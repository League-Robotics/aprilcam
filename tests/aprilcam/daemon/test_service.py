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

import json
import shutil
import tempfile
import time
from contextlib import contextmanager
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
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import ConfigLoader, PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.daemon.interfaces import Daemon
from aprilcam.daemon.lifecycle import DaemonLifecycle
from aprilcam.daemon.rpc import build_server
from aprilcam.daemon.service import VALID_ENCODINGS, DaemonService
from aprilcam.errors import (
    CameraNotPresent,
    MalformedDocument,
    NotCalibrated,
    UnknownCamera,
    UnknownPlayfield,
)
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import PixelPoint, TagFamily, TagFrame, TagId, TagRecord, WorldPoint
from aprilcam.vision.detector import Detection
from tests.aprilcam.camera.test_camera import FakeBackend


def _playfield_definition(
    name: str = "main-playfield", aruco_ids: tuple = (1, 2, 3)
) -> PlayfieldDefinition:
    """A minimal but real ``PlayfieldDefinition`` -- enough for the
    playfield/camera-config service tests below (ticket 022-001), which
    care about the daemon's storage/dispatch behaviour, not document
    parsing (``tests/aprilcam/config/test_loader.py`` covers that)."""
    return PlayfieldDefinition(
        name=name,
        width_cm=100.0,
        height_cm=80.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"u{i}", x=float(i), y=float(-i), tag_id=i)
            for i in aruco_ids
        ),
    )


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


@contextmanager
def _bind_stub(service):
    """Bind a real ``grpc.Server`` to a short top-level ``/tmp`` unix
    socket (not pytest's deeply nested ``tmp_path``, which routinely
    exceeds unix domain sockets' ~103-character path limit on
    macOS/Linux) and yield a connected, real generated stub. Factored
    out of the ``stub`` fixture below (ticket 021-005) so the
    tag-detection round-trip tests can bind a *different* service per
    test (each needs its own scripted detector) without duplicating
    the bind/connect/teardown dance."""
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


@pytest.fixture
def stub(service):
    with _bind_stub(service) as s:
        yield s


@pytest.fixture
def tag_stub_factory(directories, registry):
    """Factory fixture: bind a real ``grpc.Server`` to a fresh
    ``DaemonService`` wired to a caller-supplied scripted/counting
    detector (see ``_build_tag_service`` above), yielding
    ``(stub, slug)``. Each RPC round-trip test below needs its own
    detection script, so this is a factory rather than a single
    fixture instance."""

    @contextmanager
    def _make(detector, *, frame_count: int = 10):
        service, slug = _build_tag_service(
            directories, registry, detector=detector, frame_count=frame_count
        )
        with _bind_stub(service) as stub:
            yield stub, slug

    return _make


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


# ---------------------------------------------------------------------------
# GetTags / GetTag / GetTagHistory -- RPC round trips over a real
# gRPC unix-socket server (ticket 021-005)
# ---------------------------------------------------------------------------


class _StaticTagsDaemon(Daemon):
    """A minimal ``Daemon`` double (plain base class, derived from --
    see ``daemon.interfaces``'s module docstring) that answers
    ``get_tags`` with a pre-built ``TagFrame`` regardless of the
    camera named. Lets a test hand the wire a ``TagRecord`` carrying a
    *genuine* ``0.0``/``(0, 0)`` for fields that ``DaemonService`` never
    actually produces this sprint (every real record is honestly
    uncalibrated -- see ``test_get_tags_every_record_is_honestly_
    uncalibrated`` above) -- proving the marshalling code's *other*
    failure mode: a real zero must survive as a real zero, not collapse
    into "unset" the same way ``None`` must never coerce to ``0.0``
    (ticket 021-005's marshalling contract cuts both ways)."""

    def __init__(self, tag_frame: TagFrame) -> None:
        self._tag_frame = tag_frame

    def get_tags(self, camera: str) -> TagFrame:
        return self._tag_frame


def test_rpc_get_tags_round_trip_found_tag_is_honestly_uncalibrated(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    assert len(response.tags.tags) == 1
    record = response.tags.tags[0]
    assert record.tag.family == aprilcam_v2_pb2.APRILTAG
    assert record.tag.number == 7
    assert record.calibrated is False
    # Absence, not a 0.0/empty placeholder -- the wire contract this
    # sprint's uncalibrated records must never violate.
    assert record.HasField("world") is False
    assert record.HasField("heading_rad") is False
    assert record.HasField("world_velocity") is False
    assert record.HasField("speed") is False
    assert record.HasField("yaw_rad") is False
    assert record.world_corners == []
    assert record.HasField("pixel") is True
    assert len(record.pixel_corners) == 4


def test_rpc_get_tags_round_trip_aruco_family_and_number_survive_intact(tag_stub_factory):
    """The v1 ``-(N+1)`` internal ArUco encoding must never appear on
    the wire -- family and a non-negative number cross intact."""
    detector = _ScriptedDetector([[_detection(3, family=TagFamily.ARUCO)]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    record = response.tags.tags[0]
    assert record.tag.family == aprilcam_v2_pb2.ARUCO
    assert record.tag.number == 3


def test_rpc_get_tags_round_trip_empty_field_returns_empty_tag_frame(tag_stub_factory):
    detector = _ScriptedDetector([[]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    assert list(response.tags.tags) == []
    assert response.tags.calibration_stale is False


def test_rpc_get_tags_unknown_camera_maps_to_not_found(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, _slug):
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera="no-such-camera"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownCamera"
    assert metadata["aprilcam-requested"] == "no-such-camera"


def test_rpc_get_tag_round_trip_found(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        request = aprilcam_v2_pb2.GetTagRequest(
            camera=slug, tag=aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
        )
        response = stub.GetTag(request)

    assert response.HasField("record") is True
    assert response.record.tag.number == 7


def test_rpc_get_tag_round_trip_not_found_is_an_absent_record_not_an_error(tag_stub_factory):
    detector = _ScriptedDetector([[]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        request = aprilcam_v2_pb2.GetTagRequest(
            camera=slug, tag=aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=99)
        )
        response = stub.GetTag(request)

    assert response.HasField("record") is False


def test_rpc_get_tag_unknown_camera_maps_to_not_found(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, _slug):
        request = aprilcam_v2_pb2.GetTagRequest(
            camera="no-such-camera",
            tag=aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=1),
        )
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.GetTag(request)

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_rpc_get_tag_history_round_trip_preserves_explicit_gap(tag_stub_factory):
    target = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
    script = [[_detection(7)], [_detection(9)], [_detection(7)]]
    detector = _ScriptedDetector(script)
    with tag_stub_factory(detector, frame_count=3) as (stub, slug):
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

        response = stub.GetTagHistory(
            aprilcam_v2_pb2.GetTagHistoryRequest(camera=slug, tag=target)
        )

    history = response.history
    assert len(history) == 3
    assert [f.frame_index for f in history] == [0, 1, 2]
    assert len(history[0].tags) == 1 and history[0].tags[0].tag.number == 7
    assert list(history[1].tags) == []  # explicit gap -- retained, empty
    assert len(history[2].tags) == 1 and history[2].tags[0].tag.number == 7


def test_rpc_get_tag_history_round_trip_frames_limit(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)] for _ in range(5)])
    with tag_stub_factory(detector, frame_count=5) as (stub, slug):
        for _ in range(5):
            stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

        response = stub.GetTagHistory(
            aprilcam_v2_pb2.GetTagHistoryRequest(camera=slug, frames=2)
        )

    assert len(response.history) == 2
    assert [f.frame_index for f in response.history] == [3, 4]


def test_rpc_get_tag_history_unknown_camera_maps_to_not_found(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, _slug):
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.GetTagHistory(aprilcam_v2_pb2.GetTagHistoryRequest(camera="no-such-camera"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_rpc_get_tags_camera_not_present_maps_to_failed_precondition(stub, absent_camera):
    """Reuses the plain ``service``/``stub`` fixtures (no detection
    script needed -- the camera is never reached) to prove
    ``CameraNotPresent`` (ticket 021-002) round-trips through the new
    ``GetTags`` RPC exactly as it already does for ``GetFrame``."""
    slug, _number = absent_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "CameraNotPresent"
    assert metadata["aprilcam-slug"] == slug


def test_rpc_get_tags_round_trip_distinguishes_a_real_zero_from_absent():
    """The wire's other marshalling failure mode: a genuinely
    calibrated-looking ``0.0``/``(0, 0)`` must survive as a real value,
    never collapsing into "unset" -- the mirror image of ``None`` never
    coercing to ``0.0``. Drives ``build_server`` with a
    ``_StaticTagsDaemon`` double instead of ``DaemonService`` because no
    real detection burst this sprint ever produces a non-``None`` value
    for these fields to exercise this the other way."""
    zeroed = TagRecord(
        tag=TagId(family=TagFamily.APRILTAG, number=1),
        world=WorldPoint(x=0.0, y=0.0),
        heading_rad=0.0,
        world_velocity=WorldPoint(x=0.0, y=0.0),
        speed=0.0,
        timestamp=1.0,
        calibrated=True,
        yaw_rad=0.0,
    )
    unset = TagRecord(
        tag=TagId(family=TagFamily.APRILTAG, number=2),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=1.0,
        calibrated=False,
    )
    tag_frame = TagFrame(tags=(zeroed, unset), timestamp=1.0, frame_index=0)
    service = _StaticTagsDaemon(tag_frame)

    with _bind_stub(service) as stub:
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera="any"))

    zero_record, unset_record = response.tags.tags

    assert zero_record.HasField("heading_rad") is True
    assert zero_record.heading_rad == 0.0
    assert zero_record.HasField("world") is True
    assert zero_record.world.x == 0.0 and zero_record.world.y == 0.0
    assert zero_record.HasField("speed") is True
    assert zero_record.speed == 0.0
    assert zero_record.HasField("yaw_rad") is True

    assert unset_record.HasField("heading_rad") is False
    assert unset_record.HasField("world") is False
    assert unset_record.HasField("world_velocity") is False
    assert unset_record.HasField("speed") is False
    assert unset_record.HasField("yaw_rad") is False


# ---------------------------------------------------------------------------
# GetFrame's tag embed over the wire: absent vs. present-but-empty
# (ticket 021-005) -- the three-way distinction ImageFrame.tags
# documents must survive real (de)serialization, not just the
# same-process DaemonService assertions above.
# ---------------------------------------------------------------------------


def test_rpc_get_frame_tags_is_absent_when_no_detection_has_run(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert response.HasField("tags") is False


def test_rpc_get_frame_tags_is_present_but_empty_after_detection_saw_nothing(tag_stub_factory):
    detector = _ScriptedDetector([[]])
    with tag_stub_factory(detector, frame_count=2) as (stub, slug):
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert response.HasField("tags") is True
    assert list(response.tags.tags) == []


def test_rpc_get_frame_embeds_tags_round_trip(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_stub_factory(detector, frame_count=2) as (stub, slug):
        tags_response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        frame_response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert frame_response.HasField("tags") is True
    assert frame_response.tags.frame_index == tags_response.tags.frame_index
    assert len(frame_response.tags.tags) == 1
    assert frame_response.tags.tags[0].tag.number == 7


# ---------------------------------------------------------------------------
# Playfields / camera config -- the minimal config-service slice
# (ticket 022-001, sprint.md Architecture Decision 1 & 4)
# ---------------------------------------------------------------------------


def test_set_playfield_and_get_playfield_round_trip_reports_all_features(service):
    definition = _playfield_definition()

    service.set_playfield(definition)
    fetched = service.get_playfield("main-playfield")

    assert fetched.name == "main-playfield"
    assert fetched.definition.features() == definition.features()
    assert fetched.definition.required_aruco_ids() == (1, 2, 3)


def test_set_playfield_with_the_real_main_playfield_document_round_trips_through_the_daemon(
    service,
):
    """AC1 (UC-02.S-1), end to end through ``DaemonService`` -- not just
    the loader (``tests/aprilcam/config/test_loader.py`` covers the
    loader in isolation): push the real shipped document, fetch it
    back, and check the exact acceptance-criteria coordinates."""
    repo_root = Path(__file__).resolve().parents[3]
    main_playfield_path = repo_root / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
    definition = ConfigLoader().load_playfield(main_playfield_path)

    service.set_playfield(definition)
    fetched = service.get_playfield("main-playfield")

    features = fetched.definition.features()
    assert len(features) == 25
    by_slug = {f.slug: f for f in features}
    assert by_slug["aruco-northwest-u1"].position == WorldPoint(x=-67, y=44.65)
    assert by_slug["aruco-southeast-u5"].position == WorldPoint(x=67, y=-44.65)
    assert fetched.definition.required_aruco_ids() == tuple(range(1, 11))
    assert fetched.linked_cameras() == ()


def test_a_good_playfield_survives_a_later_rejected_push_attempt(service, tmp_path):
    """AC2's full scenario (UC-02.S-2), end to end: a definition already
    held is untouched by a later push whose document is malformed --
    ``ConfigLoader.load_playfield`` raises ``MalformedDocument`` before
    ``set_playfield`` is ever called, so the daemon's held state simply
    never had a chance to change."""
    good = _playfield_definition("main-playfield", aruco_ids=(1, 2, 3))
    service.set_playfield(good)

    bad_path = tmp_path / "bad-main-playfield.json"
    bad_path.write_text(
        json.dumps(
            {
                "name": "main-playfield",
                "playfield": {"width_cm": 100.0, "height_cm": 80.0},
                "aruco_tags": [
                    {"slug": "u3", "id": 3, "x": -1, "y": -1},
                    {"slug": "u3-dup", "id": 3, "x": 1, "y": 1},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MalformedDocument):
        definition = ConfigLoader().load_playfield(bad_path)
        service.set_playfield(definition)  # never reached

    fetched = service.get_playfield("main-playfield")
    assert fetched.definition.required_aruco_ids() == (1, 2, 3)  # unchanged


def test_list_playfields_includes_a_pushed_definition(service):
    service.set_playfield(_playfield_definition())

    playfields = service.list_playfields()

    assert [p.name for p in playfields] == ["main-playfield"]


def test_get_playfield_unknown_name_raises_unknown_playfield_listing_held_playfields(service):
    service.set_playfield(_playfield_definition("main-playfield"))

    with pytest.raises(UnknownPlayfield) as excinfo:
        service.get_playfield("no-such-field")

    assert excinfo.value.requested == "no-such-field"
    assert excinfo.value.available == ("main-playfield",)


def test_get_playfield_on_a_service_with_no_playfields_lists_none_available(service):
    with pytest.raises(UnknownPlayfield) as excinfo:
        service.get_playfield("main-playfield")
    assert excinfo.value.available == ()


def test_daemon_holds_two_differently_named_playfields_without_interference(service):
    """Sprint.md Decision 4: no single-slot cardinality limit."""
    field_a = _playfield_definition("field-a", aruco_ids=(1, 2))
    field_b = _playfield_definition("field-b", aruco_ids=(5, 6, 7))

    service.set_playfield(field_a)
    service.set_playfield(field_b)

    assert {p.name for p in service.list_playfields()} == {"field-a", "field-b"}
    assert service.get_playfield("field-a").definition.required_aruco_ids() == (1, 2)
    assert service.get_playfield("field-b").definition.required_aruco_ids() == (5, 6, 7)

    # Replacing one by name never disturbs the other.
    service.set_playfield(_playfield_definition("field-a", aruco_ids=(9,)))
    assert service.get_playfield("field-a").definition.required_aruco_ids() == (9,)
    assert service.get_playfield("field-b").definition.required_aruco_ids() == (5, 6, 7)


def test_set_camera_config_links_camera_and_get_camera_config_echoes_it_back(service, held_camera):
    _, slug, _ = held_camera
    service.set_playfield(_playfield_definition())

    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    fetched = service.get_camera_config(slug)

    assert fetched.playfield == "main-playfield"


def test_linked_playfield_reports_the_configured_camera(service, held_camera):
    _, slug, _ = held_camera
    service.set_playfield(_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    assert service.get_playfield("main-playfield").linked_cameras() == (slug,)
    assert service.list_playfields()[0].linked_cameras() == (slug,)


def test_list_cameras_reports_the_playfield_link_after_set_camera_config(service, held_camera):
    _, slug, _ = held_camera
    assert service.list_cameras()[0].playfield is None  # not configured yet

    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    assert service.list_cameras()[0].playfield == "main-playfield"


def test_camera_config_settings_round_trip_without_hardware_application(directories, registry):
    """AC6: a pushed ``settings`` dict survives the round trip through
    ``get_camera_config`` unchanged, but a spy ``set_property`` proves
    zero setting-application calls reach the backend -- ``Decision 1``'s
    "accepted and echoed back but never applied" is asserted here
    behaviourally, not just by absence of a call in the source."""
    identity = _identity("hw:spy", "Spy Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    backend = FakeBackend(read_queue=[_frame()])
    calls: List[tuple] = []
    backend.set_property = (  # type: ignore[method-assign]
        lambda name, value: calls.append((name, value)) or True
    )
    cam = Camera(slug, backend, number=number)
    cam.open()
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
    )

    service.set_camera_config(
        CameraConfig(name=slug, playfield=None, settings={"exposure": 100, "gain": 2.5})
    )
    fetched = service.get_camera_config(slug)

    assert fetched.settings == {"exposure": 100, "gain": 2.5}
    assert calls == []  # zero setting-application calls


def test_get_camera_config_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera) as excinfo:
        service.get_camera_config("never-configured")
    assert excinfo.value.requested == "never-configured"
    assert excinfo.value.available == ()


def test_reset_camera_config_raises_not_implemented_never_a_silent_no_op(service, held_camera):
    _, slug, _ = held_camera
    with pytest.raises(NotImplementedError):
        service.reset_camera_config(slug)


def test_fresh_daemon_service_instance_has_no_playfields_or_camera_links(
    directories, registry, held_camera
):
    """UC-02's ephemeral rule: a simulated restart (fresh ``DaemonService``
    over the same registry/directories) starts with zero pushed
    playfields and zero camera links -- disk, not RPC, is the source of
    record, and this ticket writes nothing to disk."""
    _, slug, cam = held_camera
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    original = DaemonService(registry=registry, discovery=discovery, directories=directories, cameras=cameras)
    original.set_playfield(_playfield_definition())
    original.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert original.list_playfields() != ()

    restarted = DaemonService(
        registry=registry, discovery=discovery, directories=directories, cameras=cameras
    )

    assert restarted.list_playfields() == ()
    assert restarted.list_cameras()[0].playfield is None
    with pytest.raises(UnknownCamera):
        restarted.get_camera_config(slug)


# ---------------------------------------------------------------------------
# Playfields / camera config -- RPC round trips (ticket 022-001)
# ---------------------------------------------------------------------------


def _playfield_definition_msg(
    name: str = "main-playfield", aruco_ids: tuple = (1, 2, 3)
) -> aprilcam_v2_pb2.PlayfieldDefinition:
    """A ``PlayfieldDefinition`` proto message, built by hand -- matches
    this file's existing convention of constructing request messages
    directly (e.g. ``aprilcam_v2_pb2.TagId(...)`` above) rather than
    importing ``daemon/rpc.py``'s private converters into a test."""
    return aprilcam_v2_pb2.PlayfieldDefinition(
        name=name,
        width_cm=100.0,
        height_cm=80.0,
        aruco_tags=[
            aprilcam_v2_pb2.PlayfieldFeatureEntry(slug=f"u{i}", x=float(i), y=float(-i), id=i)
            for i in aruco_ids
        ],
    )


def _camera_config_msg(
    name: str, *, playfield: Optional[str] = None, settings: Optional[dict] = None
) -> aprilcam_v2_pb2.CameraConfig:
    msg = aprilcam_v2_pb2.CameraConfig(name=name, settings_json=json.dumps(settings or {}))
    if playfield is not None:
        msg.playfield = playfield
    return msg


def test_rpc_set_playfield_and_get_playfield_round_trip(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))

    response = stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="main-playfield"))

    assert response.playfield.name == "main-playfield"
    definition = response.playfield.definition
    assert definition.width_cm == 100.0 and definition.height_cm == 80.0
    assert sorted(e.id for e in definition.aruco_tags) == [1, 2, 3]
    assert list(response.playfield.linked_cameras) == []


def test_rpc_list_playfields_round_trip_holds_two(stub):
    stub.SetPlayfield(
        aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg("field-a", (1,)))
    )
    stub.SetPlayfield(
        aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg("field-b", (2, 3)))
    )

    response = stub.ListPlayfields(aprilcam_v2_pb2.ListPlayfieldsRequest())

    assert {p.name for p in response.playfields} == {"field-a", "field-b"}


def test_rpc_get_playfield_unknown_name_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="no-such-field"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownPlayfield"
    assert metadata["aprilcam-requested"] == "no-such-field"


def test_rpc_get_playfield_reports_a_linked_camera(stub, held_camera):
    _, slug, _ = held_camera
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    stub.SetCameraConfig(
        aprilcam_v2_pb2.SetCameraConfigRequest(
            config=_camera_config_msg(slug, playfield="main-playfield")
        )
    )

    response = stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="main-playfield"))

    assert list(response.playfield.linked_cameras) == [slug]


def test_rpc_set_camera_config_and_get_camera_config_round_trip_settings(stub, held_camera):
    _, slug, _ = held_camera
    pushed = _camera_config_msg(slug, playfield="main-playfield", settings={"exposure": 100, "gain": 2.5})
    stub.SetCameraConfig(aprilcam_v2_pb2.SetCameraConfigRequest(config=pushed))

    response = stub.GetCameraConfig(aprilcam_v2_pb2.GetCameraConfigRequest(camera=slug))

    assert response.config.playfield == "main-playfield"
    assert json.loads(response.config.settings_json) == {"exposure": 100, "gain": 2.5}


def test_rpc_get_camera_config_unknown_camera_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetCameraConfig(aprilcam_v2_pb2.GetCameraConfigRequest(camera="never-configured"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownCamera"
