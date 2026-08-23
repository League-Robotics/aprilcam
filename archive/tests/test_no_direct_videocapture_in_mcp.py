"""Tests verifying that the MCP detection path uses daemon RPCs, never
direct cv2.VideoCapture(device_index) or in-process vision machinery.

Ticket 014-006: Remove direct VideoCapture in MCP server and vision/objects.py.
Ticket 015-003: Remove in-process detection; rewire MCP tag tools to daemon RPCs.

The daemon's CameraPipeline is the sole camera opener.  The MCP server must
never construct cv2.VideoCapture(device) or instantiate DetectionLoop/AprilCam.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_frame() -> np.ndarray:
    """Return a tiny black BGR frame for mock responses."""
    return np.zeros((10, 10, 3), dtype=np.uint8)


def _make_daemon_client():
    """Build a mock DaemonControl."""
    dc = MagicMock()
    mock_consumer = MagicMock()
    mock_consumer.__iter__ = MagicMock(return_value=iter([]))
    dc.get_tag_stream.return_value = mock_consumer
    dc.capture_frame.return_value = _fake_frame()
    return dc


# ---------------------------------------------------------------------------
# Tests: _handle_start_detection uses daemon GetTagStream (not DetectionLoop)
# ---------------------------------------------------------------------------


def test_start_detection_uses_daemon_stream(monkeypatch) -> None:
    """start_detection subscribes to the daemon's tag stream; no DetectionLoop opened."""
    from aprilcam.server import mcp_server

    dc = _make_daemon_client()
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    # Register a sentinel (daemon-owned) camera
    cam_id = "test-cam-001"
    mcp_server.registry.open(None, handle=cam_id)
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    # cv2.VideoCapture must not be called
    with patch("cv2.VideoCapture") as mock_vc:
        with patch("aprilcam.server.mcp_server.threading") as mock_threading:
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread
            result = mcp_server._handle_start_detection(cam_id)

    mock_vc.assert_not_called()
    assert result.get("status") == "started", result
    # Thread was started
    mock_thread.start.assert_called_once()

    # Entry is a DaemonStreamEntry, not the old DetectionEntry
    entry = mcp_server.detection_registry.get(cam_id)
    assert entry is not None
    assert hasattr(entry, "consumer")
    assert hasattr(entry, "history")
    assert not hasattr(entry, "loop")  # no DetectionLoop

    # Cleanup
    mcp_server.detection_registry.pop(cam_id, None)
    mcp_server.registry.close(cam_id)
    mcp_server._cam_info.pop(cam_id, None)


def test_start_detection_no_videocapture_on_cam_handle(monkeypatch) -> None:
    """cam_<N> handles never trigger VideoCapture opens — all via daemon RPC."""
    from aprilcam.server import mcp_server

    dc = _make_daemon_client()
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    cam_id = "cam_7"
    mcp_server.registry.open(None, handle=cam_id)
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    with patch("cv2.VideoCapture") as mock_vc:
        with patch("aprilcam.server.mcp_server.threading") as mock_threading:
            mock_threading.Thread.return_value = MagicMock()
            result = mcp_server._handle_start_detection(cam_id)

    mock_vc.assert_not_called()
    assert "error" not in result, result

    # Cleanup
    mcp_server.detection_registry.pop(cam_id, None)
    mcp_server.registry.close(cam_id)
    mcp_server._cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# Tests: _handle_stop_detection closes consumer (not DetectionLoop.stop)
# ---------------------------------------------------------------------------


def test_stop_detection_no_videocapture(monkeypatch) -> None:
    """stop_detection closes the TagStreamConsumer; no VideoCapture re-open."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import DaemonStreamEntry

    cam_id = "stop-cam-002"
    mock_consumer = MagicMock()
    entry = DaemonStreamEntry(
        source_id=cam_id,
        consumer=mock_consumer,
        _done_flag=[False],
    )
    entry._camera_id = cam_id
    mcp_server.detection_registry[cam_id] = entry

    with patch("cv2.VideoCapture") as mock_vc:
        result = mcp_server._handle_stop_detection(cam_id)

    mock_vc.assert_not_called()
    mock_consumer.close.assert_called_once()
    assert result == {"source_id": cam_id, "status": "stopped"}


# ---------------------------------------------------------------------------
# Tests: _capture_jpeg_bytes uses gRPC (no AF_UNIX socket)
# ---------------------------------------------------------------------------


def test_capture_jpeg_bytes_uses_grpc(monkeypatch) -> None:
    """_capture_jpeg_bytes calls client.capture_frame_jpeg(), not a socket."""
    from aprilcam.server import mcp_server

    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # minimal JPEG-like bytes
    dc = MagicMock()
    dc.capture_frame_jpeg.return_value = jpeg
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    cam_id = "grpc-cam-003"
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    import socket as _socket
    with patch.object(_socket, "socket") as mock_sock:
        result = mcp_server._capture_jpeg_bytes(cam_id)

    # AF_UNIX socket must NOT have been opened
    mock_sock.assert_not_called()
    # Bytes came from gRPC
    dc.capture_frame_jpeg.assert_called_once_with(cam_id)
    assert result == jpeg

    mcp_server._cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# Tests: _frames_from_daemon uses gRPC (no AF_UNIX)
# ---------------------------------------------------------------------------


def test_frames_from_daemon_uses_grpc(monkeypatch) -> None:
    """_frames_from_daemon iterates via gRPC CaptureFrame, not AF_UNIX socket."""
    from aprilcam.server import mcp_server

    frame = _fake_frame()
    dc = _make_daemon_client()
    dc.capture_frame.return_value = frame
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    cam_id = "grpc-cam-004"
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    import socket as _socket
    with patch.object(_socket, "socket") as mock_sock:
        frames = list(mcp_server._frames_from_daemon(cam_id, 3))

    mock_sock.assert_not_called()
    assert len(frames) == 3
    for f in frames:
        assert np.array_equal(f, frame)

    mcp_server._cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# Regression: cv2.VideoCapture raised → detection still starts via daemon
# ---------------------------------------------------------------------------


def test_detection_survives_videocapture_ban(monkeypatch) -> None:
    """If cv2.VideoCapture is banned (raises), start_detection still works
    because all opens go through the daemon's GetTagStream RPC.

    This is the regression gate: if anyone reintroduces a direct VideoCapture
    call in the MCP detection path, this test will fail.
    """
    import cv2 as _cv2
    from aprilcam.server import mcp_server

    # Monkeypatch VideoCapture to raise unconditionally
    def _forbidden(*args, **kwargs):
        raise RuntimeError(
            "cv2.VideoCapture() called in MCP server — "
            "this violates the daemon-only invariant (014-006)"
        )

    monkeypatch.setattr(_cv2, "VideoCapture", _forbidden)

    dc = _make_daemon_client()
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    cam_id = "ban-cam-005"
    mcp_server.registry.open(None, handle=cam_id)
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    # Must not raise despite VideoCapture being banned
    with patch("aprilcam.server.mcp_server.threading") as mock_threading:
        mock_threading.Thread.return_value = MagicMock()
        result = mcp_server._handle_start_detection(cam_id)

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result.get("status") == "started"

    # Cleanup
    mcp_server.detection_registry.pop(cam_id, None)
    mcp_server.registry.close(cam_id)
    mcp_server._cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# Tests: get_objects delegates to daemon RPC (no ColorCameraThread)
# ---------------------------------------------------------------------------


def test_get_objects_delegates_to_daemon_rpc(monkeypatch) -> None:
    """_handle_get_objects calls client.get_objects() RPC, not ColorCameraThread.

    ColorCameraThread is DAEMON-ONLY and must never be instantiated from the
    MCP path.
    """
    from aprilcam.server import mcp_server

    cam_id = "obj-cam-006"

    mock_resp = MagicMock()
    mock_resp.objects = []

    dc = MagicMock()
    dc.get_objects.return_value = mock_resp
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    from aprilcam.vision import objects as _objects_mod
    instantiated = {"n": 0}
    original_init = _objects_mod.ColorCameraThread.__init__

    def _patched_init(self, *args, **kwargs):
        instantiated["n"] += 1
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(_objects_mod.ColorCameraThread, "__init__", _patched_init)

    result = mcp_server._handle_get_objects(cam_id)

    assert "error" not in result, f"Unexpected error: {result}"
    dc.get_objects.assert_called_once()
    assert instantiated["n"] == 0, (
        "ColorCameraThread was instantiated from _handle_get_objects — "
        "it is DAEMON-ONLY and must not be reached from the MCP path"
    )
