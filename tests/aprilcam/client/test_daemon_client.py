"""Tests for ``aprilcam.client.daemon_client`` and
``aprilcam.client.models`` (ticket 020-005).

Fully headless: reuses ticket 004's fake-camera daemon fixture
construction (``tests/aprilcam/daemon/test_service.py``'s
``held_camera``/``service`` pattern -- a registered camera held open
via :class:`FakeBackend`, no real hardware) and binds a real
``grpc.Server`` to a ``tmp_path`` unix socket, exactly like
``test_service.py``'s own ``stub`` fixture. The difference is what
drives the RPCs: here it is ``client.daemon_client.GrpcDaemon`` (the
client-side wrapper this ticket adds), not the bare generated stub --
so these tests exercise the full client-side proto boundary
(``client/models.py``'s ``from_proto`` conversions returning genuine
``aprilcam.types`` dataclasses) and typed-error reconstruction from
gRPC trailing metadata, on top of the wire format ticket 004 already
covers.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

import grpc
import numpy as np
import pytest

from aprilcam.camera import identity as identity_module
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.config.directories import Directories
from aprilcam.daemon.rpc import build_server
from aprilcam.daemon.service import DaemonService
from aprilcam.errors import CameraNotPresent, ConnectionFailed, NotCalibrated, UnknownCamera
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.types import CameraState, ImageFrame, ProbeReport, TagFamily, TagFrame, TagId, TagRecord
from tests.aprilcam.camera.test_camera import FakeBackend
from tests.aprilcam.daemon.test_service import (
    _build_tag_service,
    _CountingDetector,
    _detection,
    _ScriptedDetector,
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
        # Headless-safe: see test_service.py's identical note -- without
        # this, _camera_for's _index_for falls through to real hardware
        # enumeration for any registered-but-uncached camera.
        identity_source=lambda: {0: identity},
    )


@pytest.fixture
def absent_camera(registry: CameraRegistry):
    """A registered-but-not-currently-enumerated camera -- see
    test_service.py's identical fixture for the full rationale."""
    identity = _identity("hw:absent", "Absent Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    return slug, number


@contextmanager
def _bind_client(service) -> Iterator[GrpcDaemon]:
    # A short, top-level /tmp dir -- not pytest's tmp_path, whose deeply
    # nested per-test path routinely exceeds unix domain sockets'
    # ~103-character path limit on macOS/Linux (same workaround as
    # test_service.py's ``stub`` fixture). Factored out of the
    # ``client`` fixture below (ticket 021-005) so the tag-detection
    # round-trip tests can bind a *different* service per test (each
    # needs its own scripted detector).
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-dc-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    try:
        listener = build_server(service, socket_path)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            yield GrpcDaemon(stub, target="localhost")
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def client(service) -> Iterator[GrpcDaemon]:
    with _bind_client(service) as c:
        yield c


@pytest.fixture
def tag_client_factory(directories, registry):
    """Factory fixture: build a ``GrpcDaemon`` connected to a fresh
    ``DaemonService`` wired to a caller-supplied scripted/counting
    detector (reusing ``test_service.py``'s ``_build_tag_service`` --
    the same cross-module-fixture convention ``test_service.py``
    itself follows for ``FakeBackend``), yielding ``(client, slug)``.
    """

    @contextmanager
    def _make(detector, *, frame_count: int = 10):
        service, slug = _build_tag_service(
            directories, registry, detector=detector, frame_count=frame_count
        )
        with _bind_client(service) as client:
            yield client, slug

    return _make


# ---------------------------------------------------------------------------
# Round trips -- proto boundary: real aprilcam.types dataclasses out
# ---------------------------------------------------------------------------


def test_list_cameras_round_trip_returns_camera_state_dataclasses(client, held_camera):
    _, slug, _ = held_camera

    states = client.list_cameras()

    assert len(states) == 1
    assert isinstance(states[0], CameraState)
    assert states[0].name == slug
    assert states[0].present is True
    assert states[0].usable is True
    assert states[0].number is not None


def test_probe_cameras_round_trip_returns_probe_report(client, held_camera):
    _, slug, _ = held_camera

    report = client.probe_cameras()

    assert isinstance(report, ProbeReport)
    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug


def test_get_frame_jpeg_round_trip(client, held_camera):
    _, slug, _ = held_camera

    frame = client.get_frame(slug)

    assert isinstance(frame, ImageFrame)
    assert frame.encoding == "jpeg"
    assert frame.width == 4 and frame.height == 4
    assert frame.data[:2] == b"\xff\xd8"  # JPEG SOI marker
    assert frame.deskewed is False


def test_get_frame_png_round_trip(client, held_camera):
    _, slug, _ = held_camera

    frame = client.get_frame(slug, encoding="png")

    assert frame.encoding == "png"
    assert frame.data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature


def test_status_round_trip(client, directories, held_camera):
    _, slug, _ = held_camera

    status = client.status()

    assert status.running is True
    assert [c.name for c in status.cameras] == [slug]
    assert status.config_dir == str(directories.config_dir)
    assert status.state_dir == str(directories.state_dir)


# ---------------------------------------------------------------------------
# Typed errors survive the round trip as the right exception classes
# ---------------------------------------------------------------------------


def test_get_frame_unknown_camera_raises_unknown_camera_with_fields_intact(
    client, held_camera, registry
):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    with pytest.raises(UnknownCamera) as excinfo:
        client.get_frame("no-such-camera")

    # The typed fields survive the round trip intact -- not just the
    # message text: the registry's known cameras as "{number} {slug}"
    # pairs (ticket 021-002), not an empty tuple and not bare slugs.
    assert excinfo.value.requested == "no-such-camera"
    assert excinfo.value.available == (f"{number} {slug}",)


def test_get_frame_deskewed_raises_not_calibrated(client, held_camera):
    _, slug, _ = held_camera

    with pytest.raises(NotCalibrated):
        client.get_frame(slug, deskewed=True)


def test_get_frame_by_number_round_trip(client, held_camera, registry):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    frame = client.get_frame(str(number))

    assert isinstance(frame, ImageFrame)
    assert frame.data[:2] == b"\xff\xd8"


def test_get_frame_camera_not_present_raises_camera_not_present_with_slug_intact(
    client, absent_camera
):
    slug, _number = absent_camera

    with pytest.raises(CameraNotPresent) as excinfo:
        client.get_frame(slug)

    assert excinfo.value.slug == slug


def test_rpc_failure_after_server_shutdown_raises_connection_failed(service):
    """An unclassified RPC failure (no ``aprilcam-error-type`` metadata
    -- here, the server going away mid-session) is reported as
    ConnectionFailed naming the target, not a bare grpc.RpcError."""
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-dc-down-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    try:
        listener = build_server(service, socket_path)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
        client = GrpcDaemon(stub, target="localhost")

        listener.stop(0)

        with pytest.raises(ConnectionFailed) as excinfo:
            client.status()
        assert excinfo.value.host == "localhost"

        channel.close()
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# get_tags / get_tag / get_tag_history -- client-side round trips
# (ticket 021-005). Exercises the full proto boundary: real
# aprilcam.types dataclasses out, with the None-not-0.0 discipline
# intact after crossing client/models.py's from_proto conversions, not
# just HasField() at the raw protobuf level (test_service.py covers
# that layer).
# ---------------------------------------------------------------------------


def test_get_tags_round_trip_returns_a_tag_frame_of_honestly_uncalibrated_records(
    tag_client_factory,
):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        tag_frame = client.get_tags(slug)

    assert isinstance(tag_frame, TagFrame)
    assert len(tag_frame.tags) == 1
    record = tag_frame.tags[0]
    assert isinstance(record, TagRecord)
    assert record.tag == TagId(family=TagFamily.APRILTAG, number=7)
    assert record.calibrated is False
    # None, never 0.0/empty -- the client-side dataclass must not lose
    # this distinction after crossing client/models.py.
    assert record.world is None
    assert record.heading_rad is None
    assert record.world_velocity is None
    assert record.speed is None
    assert record.yaw_rad is None
    assert record.world_corners is None
    assert record.pixel is not None
    assert record.pixel_corners is not None
    assert len(record.pixel_corners) == 4


def test_get_tags_round_trip_empty_field_returns_empty_tag_frame(tag_client_factory):
    detector = _ScriptedDetector([[]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        tag_frame = client.get_tags(slug)

    assert tag_frame.tags == ()
    assert tag_frame.calibration_stale is False


def test_get_tags_unknown_camera_raises_unknown_camera(client):
    with pytest.raises(UnknownCamera) as excinfo:
        client.get_tags("no-such-camera")
    assert excinfo.value.requested == "no-such-camera"


def test_get_tag_round_trip_found_returns_a_tag_record(tag_client_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        record = client.get_tag(slug, TagId(family=TagFamily.APRILTAG, number=7))

    assert record is not None
    assert isinstance(record, TagRecord)
    assert record.tag == TagId(family=TagFamily.APRILTAG, number=7)


def test_get_tag_round_trip_not_found_returns_none_never_a_zero_valued_record(
    tag_client_factory,
):
    detector = _ScriptedDetector([[]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        record = client.get_tag(slug, TagId(family=TagFamily.APRILTAG, number=99))

    assert record is None


def test_get_tag_unknown_camera_raises_unknown_camera(client):
    with pytest.raises(UnknownCamera):
        client.get_tag("no-such-camera", TagId(family=TagFamily.APRILTAG, number=1))


def test_get_tag_history_round_trip_preserves_explicit_gap(tag_client_factory):
    target = TagId(family=TagFamily.APRILTAG, number=7)
    script = [[_detection(7)], [_detection(9)], [_detection(7)]]
    detector = _ScriptedDetector(script)
    with tag_client_factory(detector, frame_count=3) as (client, slug):
        client.get_tags(slug)
        client.get_tags(slug)
        client.get_tags(slug)

        history = client.get_tag_history(slug, tag=target)

    assert isinstance(history, tuple)
    assert len(history) == 3
    assert all(isinstance(f, TagFrame) for f in history)
    assert len(history[0].tags) == 1 and history[0].tags[0].tag == target
    assert history[1].tags == ()  # explicit gap -- retained, empty
    assert len(history[2].tags) == 1 and history[2].tags[0].tag == target


def test_get_tag_history_round_trip_with_no_prior_queries_returns_empty_tuple(
    tag_client_factory,
):
    detector = _CountingDetector()
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        history = client.get_tag_history(slug)

    assert history == ()


def test_get_tag_history_unknown_camera_raises_unknown_camera(client):
    with pytest.raises(UnknownCamera):
        client.get_tag_history("no-such-camera")


def test_get_frame_embeds_tags_after_a_prior_get_tags_call(tag_client_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_client_factory(detector, frame_count=2) as (client, slug):
        tag_frame = client.get_tags(slug)
        frame = client.get_frame(slug)

    assert frame.tags == tag_frame


def test_get_frame_tags_is_none_when_no_detection_has_run(tag_client_factory):
    detector = _CountingDetector()
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        frame = client.get_frame(slug)

    assert frame.tags is None
