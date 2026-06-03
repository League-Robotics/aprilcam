"""Tests for aprilcam.daemon.grpc_server — AprilCamServicer and make_grpc_server.

These tests use a mock camera registry so no real camera hardware is required.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.daemon.grpc_server import AprilCamServicer, make_grpc_server
from aprilcam.proto import aprilcam_pb2


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_servicer(cameras=None, *, tmp_path: Path) -> AprilCamServicer:
    """Build an AprilCamServicer with an optional pre-populated camera registry."""
    import tempfile

    from aprilcam.config import Config

    base = Path(tempfile.mkdtemp(prefix="ags_", dir="/tmp"))
    sock_dir = base / "s"
    data_dir = base / "d"
    sock_dir.mkdir()
    data_dir.mkdir()

    config = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )

    registry: dict = cameras if cameras is not None else {}
    lock = threading.Lock()
    shutdown = threading.Event()
    return AprilCamServicer(
        cameras=registry,
        cam_lock=lock,
        config=config,
        shutdown_event=shutdown,
    )


def _mock_context() -> MagicMock:
    """Return a MagicMock that satisfies the grpc.ServicerContext interface."""
    ctx = MagicMock(spec=grpc.ServicerContext)
    return ctx


# ── Tests: ListCameras ─────────────────────────────────────────────────────────


def test_list_cameras_empty(tmp_path: Path) -> None:
    """ListCameras returns an empty list when no cameras are open."""
    servicer = _make_servicer(tmp_path=tmp_path)
    response = servicer.ListCameras(aprilcam_pb2.Empty(), _mock_context())
    assert list(response.cameras) == []


def test_list_cameras_with_entries(tmp_path: Path) -> None:
    """ListCameras returns names of all cameras in the registry."""
    pipeline_a = MagicMock()
    pipeline_b = MagicMock()
    registry = {"cam-0": pipeline_a, "cam-1": pipeline_b}

    servicer = _make_servicer(cameras=registry, tmp_path=tmp_path)
    response = servicer.ListCameras(aprilcam_pb2.Empty(), _mock_context())
    assert sorted(response.cameras) == ["cam-0", "cam-1"]


# ── Tests: GetImageStream (idempotent) ─────────────────────────────────────────


def test_get_image_stream_creates_producer(tmp_path: Path) -> None:
    """GetImageStream creates a producer for a new camera and returns an endpoint."""
    pipeline = MagicMock()
    registry = {"cam-0": pipeline}

    servicer = _make_servicer(cameras=registry, tmp_path=tmp_path)
    request = aprilcam_pb2.StreamRequest(cam_name="cam-0", max_hz=10)

    endpoint = servicer.GetImageStream(request, _mock_context())

    # At least one of socket_path or tcp_port should be set
    assert endpoint.socket_path or endpoint.tcp_port

    # Producer should now be in the registry
    assert "cam-0" in servicer._image_producers

    # Clean up
    servicer.stop_all_producers()


def test_get_image_stream_is_idempotent(tmp_path: Path) -> None:
    """Calling GetImageStream twice for the same camera returns the same endpoint."""
    pipeline = MagicMock()
    registry = {"cam-0": pipeline}

    servicer = _make_servicer(cameras=registry, tmp_path=tmp_path)
    request = aprilcam_pb2.StreamRequest(cam_name="cam-0", max_hz=10)

    ep1 = servicer.GetImageStream(request, _mock_context())
    ep2 = servicer.GetImageStream(request, _mock_context())

    # Both calls should yield the same socket path (or port)
    assert ep1.socket_path == ep2.socket_path
    assert ep1.tcp_port == ep2.tcp_port

    # Only one producer should exist
    assert len(servicer._image_producers) == 1

    servicer.stop_all_producers()


# ── Tests: GetTagStream (idempotent) ──────────────────────────────────────────


def test_get_tag_stream_creates_producer(tmp_path: Path) -> None:
    """GetTagStream creates a TagStreamProducer for a new camera."""
    pipeline = MagicMock()
    registry = {"cam-0": pipeline}

    servicer = _make_servicer(cameras=registry, tmp_path=tmp_path)
    request = aprilcam_pb2.StreamRequest(cam_name="cam-0", max_hz=20)

    endpoint = servicer.GetTagStream(request, _mock_context())

    assert endpoint.socket_path or endpoint.tcp_port
    assert "cam-0" in servicer._tag_producers

    servicer.stop_all_producers()


def test_get_tag_stream_is_idempotent(tmp_path: Path) -> None:
    """Calling GetTagStream twice returns the same endpoint."""
    pipeline = MagicMock()
    registry = {"cam-0": pipeline}

    servicer = _make_servicer(cameras=registry, tmp_path=tmp_path)
    request = aprilcam_pb2.StreamRequest(cam_name="cam-0", max_hz=20)

    ep1 = servicer.GetTagStream(request, _mock_context())
    ep2 = servicer.GetTagStream(request, _mock_context())

    assert ep1.socket_path == ep2.socket_path
    assert ep1.tcp_port == ep2.tcp_port
    assert len(servicer._tag_producers) == 1

    servicer.stop_all_producers()


# ── Tests: not-found paths ────────────────────────────────────────────────────


def test_get_image_stream_unknown_camera(tmp_path: Path) -> None:
    """GetImageStream returns NOT_FOUND when the camera is not open."""
    servicer = _make_servicer(tmp_path=tmp_path)
    ctx = _mock_context()
    request = aprilcam_pb2.StreamRequest(cam_name="ghost", max_hz=10)

    servicer.GetImageStream(request, ctx)
    ctx.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)


def test_get_tag_stream_unknown_camera(tmp_path: Path) -> None:
    """GetTagStream returns NOT_FOUND when the camera is not open."""
    servicer = _make_servicer(tmp_path=tmp_path)
    ctx = _mock_context()
    request = aprilcam_pb2.StreamRequest(cam_name="ghost", max_hz=10)

    servicer.GetTagStream(request, ctx)
    ctx.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)


def test_capture_frame_unknown_camera(tmp_path: Path) -> None:
    """CaptureFrame returns NOT_FOUND when the camera is not open."""
    servicer = _make_servicer(tmp_path=tmp_path)
    ctx = _mock_context()

    servicer.CaptureFrame(aprilcam_pb2.CameraRequest(cam_name="ghost"), ctx)
    ctx.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)


def test_get_tags_unknown_camera(tmp_path: Path) -> None:
    """GetTags returns NOT_FOUND when the camera is not open."""
    servicer = _make_servicer(tmp_path=tmp_path)
    ctx = _mock_context()

    servicer.GetTags(aprilcam_pb2.CameraRequest(cam_name="ghost"), ctx)
    ctx.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)


# ── Tests: Shutdown ────────────────────────────────────────────────────────────


def test_shutdown_sets_event(tmp_path: Path) -> None:
    """Shutdown RPC sets the shutdown_event."""
    servicer = _make_servicer(tmp_path=tmp_path)
    assert not servicer._shutdown_event.is_set()

    servicer.Shutdown(aprilcam_pb2.Empty(), _mock_context())

    assert servicer._shutdown_event.is_set()


# ── Tests: make_grpc_server ────────────────────────────────────────────────────


def test_make_grpc_server_returns_grpc_server(tmp_path: Path) -> None:
    """make_grpc_server returns a grpc.Server instance without error."""
    servicer = _make_servicer(tmp_path=tmp_path)
    # Use an ephemeral port to avoid conflicts
    server = make_grpc_server(transports=["[::]:0"], servicer=servicer)

    try:
        assert isinstance(server, grpc.Server)
    finally:
        server.stop(grace=0)


def test_make_grpc_server_no_transports(tmp_path: Path) -> None:
    """make_grpc_server with an empty transport list still returns a valid server."""
    servicer = _make_servicer(tmp_path=tmp_path)
    server = make_grpc_server(transports=[], servicer=servicer)
    try:
        assert isinstance(server, grpc.Server)
    finally:
        server.stop(grace=0)
