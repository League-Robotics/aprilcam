"""Tests for ``aprilcam.daemon.service`` and ``aprilcam.daemon.rpc``
(ticket 020-004; ``get_tags``/``get_tag``/``get_tag_history`` and the
``get_frame`` tag-embed added by ticket 021-004).

Fully headless: a :class:`FakeBackend` (reused from
``tests.aprilcam.camera.test_camera``, matching ``test_discovery.py``'s
established cross-module-fixture convention) stands in for real camera
hardware. The RPC round-trip tests bind a real ``grpc.Server`` to a
``tmp_path`` unix socket and drive it with a real generated stub, so
they exercise genuine (de)serialization through ``daemon/rpc.py`` and
the compiled ``proto/aprilcam_v2.proto`` messages, not just
``DaemonService``'s Python-level method bodies.

The tag-detection tests below use a substitutable ``TagDetector``-shaped
double (``_CountingDetector``/``_ScriptedDetector``), injected via
``DetectionEngine(detector_factory=...)`` and ``DaemonService(...,
detection=...)`` -- the CPU-budget "exactly one burst per call" pins and
the explicit-gap history test need an exact, controllable call count and
tag sequence, not real pixel detection (``daemon/detection.py``'s own
tests, ``test_detection.py``, cover real end-to-end synthetic-image
detection instead).
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import grpc
import numpy as np
import pytest

from aprilcam.camera import identity as identity_module
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.config.directories import Directories
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.daemon.lifecycle import DaemonLifecycle
from aprilcam.daemon.rpc import build_server
from aprilcam.daemon.service import VALID_ENCODINGS, DaemonService
from aprilcam.errors import CameraNotPresent, NotCalibrated, UnknownCamera
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import PixelPoint, TagFamily, TagId
from aprilcam.vision.detector import Detection
from tests.aprilcam.camera.test_camera import FakeBackend


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


def _identity(hardware_key: str, device_name: str) -> CameraIdentity:
    return CameraIdentity(
        slug=identity_module.device_name_slug(device_name),
        device_name=device_name,
        hardware_key=hardware_key,
        source="vid_pid_location",
        is_fallback=False,
    )


@pytest.fixture
def directories(tmp_path: Path) -> Directories:
    return Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")


@pytest.fixture
def registry(tmp_path: Path) -> CameraRegistry:
    return CameraRegistry(tmp_path / "state" / "cameras")


@pytest.fixture
def held_camera(registry: CameraRegistry):
    """A registered camera already held open (as ``get_frame`` leaves
    one) with two frames queued -- the "fake in-process camera fixture"
    the ticket's Testing section asks for."""
    identity = _identity("hw:fake", "Fake Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    cam = Camera(slug, FakeBackend(read_queue=[_frame(), _frame(), _frame()]), number=number)
    cam.open()
    return identity, slug, cam


@pytest.fixture
def service(directories, registry, held_camera):
    identity, slug, cam = held_camera
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    return DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        # Headless-safe: without this, DaemonService's own
        # identity_source defaults to real hardware enumeration
        # (aprilcam.camera.identity.resolve_all). Only the held camera
        # is "present" as far as _camera_for's _index_for lookup is
        # concerned -- anything else registered but not in this dict
        # is a registered-but-absent camera (CameraNotPresent tests
        # below).
        identity_source=lambda: {0: identity},
    )


@pytest.fixture
def absent_camera(registry: CameraRegistry):
    """A camera the registry remembers (assigned a slug and a number)
    but that ``service``'s identity source does not currently
    enumerate -- simulates an unplugged camera (UC-25), never held open
    in ``service``'s ``cameras`` cache."""
    identity = _identity("hw:absent", "Absent Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    return slug, number


# ---------------------------------------------------------------------------
# DaemonService -- direct method tests
# ---------------------------------------------------------------------------


def test_list_cameras_reports_the_held_open_camera(service, held_camera):
    _, slug, _ = held_camera

    states = service.list_cameras()

    assert len(states) == 1
    assert states[0].name == slug
    assert states[0].present is True
    assert states[0].usable is True


def test_active_slugs_reflects_the_held_camera_cache(service, held_camera):
    _, slug, _ = held_camera
    assert service.active_slugs() == {slug}


def test_probe_cameras_skips_the_held_open_camera(service, held_camera):
    """UC-25.S-7, wired for real: probe_cameras() must not touch a
    camera get_frame already holds open."""
    _, slug, _ = held_camera

    report = service.probe_cameras()

    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug


def test_get_frame_jpeg_default(service, held_camera):
    _, slug, _ = held_camera

    frame = service.get_frame(slug)

    assert frame.encoding == "jpeg"
    assert frame.width == 4 and frame.height == 4
    assert frame.data[:2] == b"\xff\xd8"  # JPEG SOI marker
    assert frame.deskewed is False


def test_get_frame_png_lossless(service, held_camera):
    _, slug, _ = held_camera

    frame = service.get_frame(slug, encoding="png")

    assert frame.encoding == "png"
    assert frame.data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature


def test_get_frame_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera) as excinfo:
        service.get_frame("no-such-camera")
    assert excinfo.value.requested == "no-such-camera"


# ---------------------------------------------------------------------------
# Camera addressing by persistent number (ticket 021-002, SUC-005)
# ---------------------------------------------------------------------------


def test_get_frame_by_number_and_by_slug_return_frames_from_the_same_camera(
    service, held_camera, registry
):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    by_slug = service.get_frame(slug)
    by_number = service.get_frame(str(number))

    assert by_slug.data == by_number.data
    assert by_slug.width == by_number.width and by_slug.height == by_number.height


def test_get_frame_unknown_number_raises_unknown_camera_with_number_slug_available(
    service, held_camera, registry
):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    with pytest.raises(UnknownCamera) as excinfo:
        service.get_frame("99")

    assert excinfo.value.requested == "99"
    assert excinfo.value.available == (f"{number} {slug}",)


def test_get_frame_absent_registered_camera_by_slug_raises_camera_not_present(
    service, absent_camera
):
    slug, _number = absent_camera

    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_frame(slug)

    assert excinfo.value.slug == slug


def test_get_frame_absent_registered_camera_by_number_raises_camera_not_present(
    service, absent_camera
):
    slug, number = absent_camera

    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_frame(str(number))

    assert excinfo.value.slug == slug


def test_get_frame_deskewed_raises_not_calibrated(service, held_camera):
    _, slug, _ = held_camera
    with pytest.raises(NotCalibrated):
        service.get_frame(slug, deskewed=True)


def test_get_frame_unsupported_encoding_raises_value_error(service, held_camera):
    _, slug, _ = held_camera
    with pytest.raises(ValueError):
        service.get_frame(slug, encoding="bmp")


def test_status_reports_running_cameras_and_directories(service, directories, held_camera):
    _, slug, _ = held_camera

    status = service.status()

    assert status.running is True
    assert [c.name for c in status.cameras] == [slug]
    assert status.config_dir == str(directories.config_dir)
    assert status.state_dir == str(directories.state_dir)


def test_close_closes_every_held_camera(service, held_camera):
    _, _, cam = held_camera

    service.close()

    assert cam.state.usable is False
    assert service.active_slugs() == frozenset()


def test_valid_encodings_matches_daemon_get_frame_contract():
    assert VALID_ENCODINGS == ("jpeg", "png")


# ---------------------------------------------------------------------------
# daemon/rpc.py -- round trips over a real gRPC unix-socket server
# ---------------------------------------------------------------------------


@pytest.fixture
def stub(service):
    # A short, top-level /tmp dir -- not pytest's tmp_path, whose deeply
    # nested per-test path (test name and all) routinely exceeds unix
    # domain sockets' ~103-character path limit on macOS/Linux.
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    try:
        listener = build_server(service, socket_path)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            yield aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_rpc_list_cameras_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.ListCameras(aprilcam_v2_pb2.ListCamerasRequest())

    assert len(response.cameras) == 1
    assert response.cameras[0].name == slug
    assert response.cameras[0].present is True
    assert response.cameras[0].HasField("number")


def test_rpc_probe_cameras_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.ProbeCameras(aprilcam_v2_pb2.ProbeCamerasRequest())

    assert len(response.skipped) == 1
    assert response.skipped[0].backend == slug


def test_rpc_get_frame_jpeg_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert response.encoding == "jpeg"
    assert response.width == 4
    assert response.height == 4
    assert response.data[:2] == b"\xff\xd8"


def test_rpc_get_frame_png_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug, encoding="png"))

    assert response.encoding == "png"
    assert response.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_rpc_get_status_round_trip(stub, directories, held_camera):
    _, slug, _ = held_camera

    response = stub.GetStatus(aprilcam_v2_pb2.GetStatusRequest())

    assert response.running is True
    assert [c.name for c in response.cameras] == [slug]
    assert response.config_dir == str(directories.config_dir)
    assert response.state_dir == str(directories.state_dir)


def test_rpc_get_frame_unknown_camera_maps_to_not_found_with_typed_metadata(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera="no-such-camera"))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownCamera"
    assert metadata["aprilcam-requested"] == "no-such-camera"


def test_rpc_get_frame_deskewed_maps_to_failed_precondition(stub, held_camera):
    _, slug, _ = held_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug, deskewed=True))

    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "NotCalibrated"


def test_rpc_get_frame_by_number_round_trip(stub, held_camera, registry):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=str(number)))

    assert response.encoding == "jpeg"
    assert response.data[:2] == b"\xff\xd8"


def test_rpc_get_frame_camera_not_present_maps_to_failed_precondition_with_slug(
    stub, absent_camera
):
    slug, _number = absent_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "CameraNotPresent"
    assert metadata["aprilcam-slug"] == slug


def test_rpc_get_frame_camera_not_present_by_number_maps_to_failed_precondition(
    stub, absent_camera
):
    slug, number = absent_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=str(number)))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "CameraNotPresent"
    assert metadata["aprilcam-slug"] == slug


# ---------------------------------------------------------------------------
# Full stack, zero cameras (AC1 / UC-01.S-2) -- proves the whole
# lifecycle+service+discovery assembly (not just the bare lock/listener
# mechanics test_lifecycle.py covers) succeeds and reports ready when
# nothing is enumerated -- headless, no hardware.
# ---------------------------------------------------------------------------


def test_full_stack_start_with_zero_cameras_and_zero_config_succeeds(tmp_path, directories):
    registry = CameraRegistry(directories.state_dir / "cameras")
    cameras: Dict[str, Camera] = {}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {},  # nothing enumerated -- no hardware at all
    )
    service = DaemonService(registry=registry, discovery=discovery, directories=directories, cameras=cameras)

    socket_dir = Path(tempfile.mkdtemp(prefix="ac-zero-", dir="/tmp"))
    try:
        lifecycle = DaemonLifecycle(
            directories=directories,
            build_listener=lambda: build_server(service, socket_dir / "control.sock"),
            status_source=service.status,
            watch=discovery.watch,
            stop_watch=discovery.stop_watch,
            on_stop=service.close,
            lock_path=socket_dir / "daemon.lock",
            socket_path=socket_dir / "control.sock",
        )

        lifecycle.start()
        try:
            status = lifecycle.status()
            assert status.running is True
            assert status.cameras == ()
            assert status.config_dir == str(directories.config_dir)
            assert status.state_dir == str(directories.state_dir)
        finally:
            lifecycle.stop()
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# get_tags / get_tag / get_tag_history (ticket 021-004)
# ---------------------------------------------------------------------------


class _CountingDetector:
    """A ``TagDetector``-shaped double: counts ``detect()`` calls and
    returns a fixed (empty, by default) set of hits every time. The
    CPU-budget "exactly one burst per call" pins below need an accurate
    call count, not real pixel detection."""

    def __init__(self, hits: Optional[List[Detection]] = None) -> None:
        self.calls = 0
        self._hits = hits if hits is not None else []

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> List[Detection]:
        self.calls += 1
        return list(self._hits)


class _ScriptedDetector:
    """A ``TagDetector``-shaped double returning a different, prescripted
    set of hits on each successive call -- builds an exact
    present/absent-tag sequence across bursts, for the explicit-gap
    history test (UC-26.S-4). Calls past the end of the script return no
    hits."""

    def __init__(self, script: List[List[Detection]]) -> None:
        self._script = [list(hits) for hits in script]
        self.calls = 0

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> List[Detection]:
        result = self._script[self.calls] if self.calls < len(self._script) else []
        self.calls += 1
        return result


def _detection(number: int, family: TagFamily = TagFamily.APRILTAG) -> Detection:
    corners = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]], dtype=np.float32)
    return Detection(family=family, number=number, corners=corners, center=PixelPoint(x=5.0, y=5.0))


def _build_tag_service(directories, registry, *, detector, frame_count: int = 10):
    """Assemble a headless ``DaemonService`` wired to *detector* via a
    fresh ``DetectionEngine`` -- the shared setup every burst-counting /
    scripted-detection test below needs, factored out so each test states
    only what is different about it (the detector's script and how many
    times it queries). Returns ``(service, slug)``."""
    identity = _identity("hw:fake-tags", "Fake Tag Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    cam = Camera(
        slug, FakeBackend(read_queue=[_frame() for _ in range(frame_count)]), number=number
    )
    cam.open()
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    detection = DetectionEngine(detector_factory=lambda: detector)
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
        detection=detection,
    )
    return service, slug


@pytest.fixture
def counting_detector():
    return _CountingDetector()


@pytest.fixture
def tag_service(directories, registry, counting_detector):
    return _build_tag_service(directories, registry, detector=counting_detector)


def test_get_tags_triggers_exactly_one_detection_burst_per_call(tag_service, counting_detector):
    service, slug = tag_service

    service.get_tags(slug)
    assert counting_detector.calls == 1

    service.get_tags(slug)
    assert counting_detector.calls == 2


def test_get_tags_called_twice_with_nothing_in_view_triggers_exactly_two_bursts(
    tag_service, counting_detector
):
    """SUC-002's CPU-budget pin: never zero, never more than two."""
    service, slug = tag_service

    first = service.get_tags(slug)
    second = service.get_tags(slug)

    assert first.tags == () and second.tags == ()
    assert counting_detector.calls == 2


def test_get_tag_triggers_exactly_one_detection_burst_per_call(tag_service, counting_detector):
    service, slug = tag_service
    tag = TagId(family=TagFamily.APRILTAG, number=7)

    service.get_tag(slug, tag)
    assert counting_detector.calls == 1

    service.get_tag(slug, tag)
    assert counting_detector.calls == 2


def test_get_tag_returns_none_for_an_absent_tag_never_a_zero_valued_record(tag_service):
    service, slug = tag_service

    result = service.get_tag(slug, TagId(family=TagFamily.APRILTAG, number=99))

    assert result is None


def test_get_tags_on_an_empty_field_returns_an_empty_tag_frame_not_an_error(tag_service):
    service, slug = tag_service

    frame = service.get_tags(slug)

    assert frame.tags == ()
    assert frame.calibration_stale is False


def test_get_tag_history_with_no_prior_queries_triggers_zero_bursts(tag_service, counting_detector):
    service, slug = tag_service

    history = service.get_tag_history(slug)

    assert history == ()
    assert counting_detector.calls == 0


def test_get_tag_history_reads_the_buffer_without_triggering_new_bursts(
    tag_service, counting_detector
):
    service, slug = tag_service

    service.get_tags(slug)
    service.get_tags(slug)
    assert counting_detector.calls == 2

    history = service.get_tag_history(slug)

    assert len(history) == 2
    assert counting_detector.calls == 2  # unchanged -- a pure read


def test_get_tag_history_returns_everything_when_fewer_than_requested(tag_service):
    service, slug = tag_service

    service.get_tags(slug)
    service.get_tags(slug)

    history = service.get_tag_history(slug, frames=100)

    assert len(history) == 2  # not an error, not padded/truncated to 100


def test_get_tag_history_by_frames_returns_the_most_recent_n(tag_service):
    service, slug = tag_service

    frames = [service.get_tags(slug) for _ in range(5)]

    history = service.get_tag_history(slug, frames=2)

    assert history == tuple(frames[-2:])


def test_idle_daemon_triggers_zero_detections_over_a_simulated_window(
    tag_service, counting_detector
):
    """No ``get_tags``/``get_tag``/``get_tag_history`` call at all --
    trivially zero detections, since there is no background loop to idle
    (sprint.md Decision 1). Pinned as a named regression test per
    dev-plan Stage 8 rather than left as an unstated assumption; a brief
    sleep stands in for "the daemon sat idle" without slowing the suite
    down to the full 10 s window the acceptance criterion names -- the
    assertion holds at any duration, since nothing but an explicit call
    increments the counter."""
    service, _slug = tag_service
    time.sleep(0.05)
    assert counting_detector.calls == 0


def test_get_tags_every_record_is_honestly_uncalibrated(directories, registry):
    """The Description's field-by-field uncalibrated contract, exercised
    at the ``DaemonService`` layer (``test_detection.py`` covers the same
    contract one layer down, against a real detector)."""
    detector = _ScriptedDetector([[_detection(7)]])
    service, slug = _build_tag_service(directories, registry, detector=detector, frame_count=1)

    tag_frame = service.get_tags(slug)

    assert len(tag_frame.tags) == 1
    record = tag_frame.tags[0]
    assert record.calibrated is False
    assert record.world is None
    assert record.heading_rad is None
    assert record.world_velocity is None
    assert record.speed is None
    assert record.yaw_rad is None
    assert record.world_corners is None
    assert record.pixel is not None
    assert record.pixel_corners is not None
    assert len(record.pixel_corners) == 4


def test_get_tag_history_preserves_explicit_gaps_when_filtered_by_tag(directories, registry):
    """UC-26.S-4: a tag absent from the middle of the requested window
    shows an explicit gap -- the frame is retained, with its real
    timestamp/frame_index, and an empty ``.tags`` -- never interpolated
    and never silently dropped."""
    target = TagId(family=TagFamily.APRILTAG, number=7)
    script = [[_detection(7)], [_detection(9)], [_detection(7)]]
    detector = _ScriptedDetector(script)
    service, slug = _build_tag_service(directories, registry, detector=detector, frame_count=3)

    service.get_tags(slug)
    service.get_tags(slug)
    service.get_tags(slug)

    history = service.get_tag_history(slug, tag=target)

    assert len(history) == 3  # no frame dropped, even though frame 1 lacks the tag
    assert [f.frame_index for f in history] == [0, 1, 2]
    assert len(history[0].tags) == 1 and history[0].tags[0].tag == target
    assert history[1].tags == ()  # the explicit gap -- frame retained, .tags empty
    assert len(history[2].tags) == 1 and history[2].tags[0].tag == target
    assert history[0].timestamp <= history[1].timestamp <= history[2].timestamp


def test_get_tags_by_number_and_by_slug_share_the_same_camera_buffer(
    tag_service, registry, counting_detector
):
    """SUC-005: ``_camera_for``'s number-or-slug resolution is reused,
    unmodified, by the new tag-query methods -- both forms land in the
    same per-camera ring buffer."""
    service, slug = tag_service
    number = registry.get(slug).number

    service.get_tags(slug)
    service.get_tags(str(number))

    history = service.get_tag_history(slug)
    assert len(history) == 2
    assert counting_detector.calls == 2


def test_get_tags_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera) as excinfo:
        service.get_tags("no-such-camera")
    assert excinfo.value.requested == "no-such-camera"


def test_get_tag_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera):
        service.get_tag("no-such-camera", TagId(family=TagFamily.APRILTAG, number=1))


def test_get_tag_history_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera):
        service.get_tag_history("no-such-camera")


def test_get_tags_absent_registered_camera_raises_camera_not_present(service, absent_camera):
    slug, _number = absent_camera
    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_tags(slug)
    assert excinfo.value.slug == slug


def test_get_tag_history_absent_registered_camera_raises_camera_not_present(
    service, absent_camera
):
    slug, _number = absent_camera
    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_tag_history(slug)
    assert excinfo.value.slug == slug


# ---------------------------------------------------------------------------
# get_frame's tag embed (ticket 021-004, sprint.md Decision 4)
# ---------------------------------------------------------------------------


def test_get_frame_tags_is_none_when_the_ring_buffer_is_empty(tag_service):
    service, slug = tag_service

    frame = service.get_frame(slug)

    assert frame.tags is None


def test_get_frame_embeds_the_latest_ring_buffer_entry_without_triggering_a_new_burst(
    tag_service, counting_detector
):
    service, slug = tag_service

    tag_frame = service.get_tags(slug)
    assert counting_detector.calls == 1

    frame = service.get_frame(slug)

    assert frame.tags == tag_frame
    assert counting_detector.calls == 1  # get_frame itself triggered no new burst


def test_get_frame_deskewed_behavior_is_unchanged_by_the_tag_embed(tag_service):
    """``get_frame``'s existing ``deskewed=True`` -> ``NotCalibrated``
    behavior must survive the ``tags=`` extension unchanged."""
    service, slug = tag_service
    with pytest.raises(NotCalibrated):
        service.get_frame(slug, deskewed=True)
