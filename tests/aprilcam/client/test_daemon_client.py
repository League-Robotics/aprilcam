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
from aprilcam.errors import ConnectionFailed, NotCalibrated, UnknownCamera
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.types import CameraState, ImageFrame, ProbeReport
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


@pytest.fixture
def client(service) -> Iterator[GrpcDaemon]:
    # A short, top-level /tmp dir -- not pytest's tmp_path, whose deeply
    # nested per-test path routinely exceeds unix domain sockets'
    # ~103-character path limit on macOS/Linux (same workaround as
    # test_service.py's ``stub`` fixture).
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


def test_get_frame_unknown_camera_raises_unknown_camera_with_fields_intact(client, held_camera):
    _, slug, _ = held_camera

    with pytest.raises(UnknownCamera) as excinfo:
        client.get_frame("no-such-camera")

    # The typed fields survive the round trip intact -- not just the
    # message text: the registry's known slugs, not an empty tuple.
    assert excinfo.value.requested == "no-such-camera"
    assert excinfo.value.available == (slug,)


def test_get_frame_deskewed_raises_not_calibrated(client, held_camera):
    _, slug, _ = held_camera

    with pytest.raises(NotCalibrated):
        client.get_frame(slug, deskewed=True)


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
