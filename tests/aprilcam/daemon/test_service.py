"""Tests for ``aprilcam.daemon.service`` and ``aprilcam.daemon.rpc``
(ticket 020-004).

Fully headless: a :class:`FakeBackend` (reused from
``tests.aprilcam.camera.test_camera``, matching ``test_discovery.py``'s
established cross-module-fixture convention) stands in for real camera
hardware. The RPC round-trip tests bind a real ``grpc.Server`` to a
``tmp_path`` unix socket and drive it with a real generated stub, so
they exercise genuine (de)serialization through ``daemon/rpc.py`` and
the compiled ``proto/aprilcam_v2.proto`` messages, not just
``DaemonService``'s Python-level method bodies.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Dict

import grpc
import numpy as np
import pytest

from aprilcam.camera import identity as identity_module
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import DaemonLifecycle
from aprilcam.daemon.rpc import build_server
from aprilcam.daemon.service import VALID_ENCODINGS, DaemonService
from aprilcam.errors import NotCalibrated, UnknownCamera
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
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
    return DaemonService(registry=registry, discovery=discovery, directories=directories, cameras=cameras)


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
