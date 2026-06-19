"""Tests verifying that the MCP detection path uses DaemonCapture, never
direct cv2.VideoCapture(device_index).

Ticket 014-006: Remove direct VideoCapture in MCP server and vision/objects.py.

The daemon's CameraPipeline is the sole camera opener.  The MCP server must
never construct cv2.VideoCapture(device) — it fetches frames from the daemon
via gRPC (DaemonCapture / CaptureFrame RPC).
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


def _make_daemon_client(frame: np.ndarray | None = None):
    """Build a mock DaemonControl that returns *frame* from capture_frame."""
    dc = MagicMock()
    dc.capture_frame.return_value = frame if frame is not None else _fake_frame()
    return dc


# ---------------------------------------------------------------------------
# Tests: _handle_start_detection uses DaemonCapture (not VideoCapture)
# ---------------------------------------------------------------------------


def test_start_detection_uses_daemon_capture(monkeypatch) -> None:
    """start_detection wraps the camera in DaemonCapture; no VideoCapture opened."""
    from aprilcam.server import mcp_server

    dc = _make_daemon_client()
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    # Register a sentinel (daemon-owned) camera
    cam_id = "test-cam-001"
    mcp_server.registry.open(None, handle=cam_id)
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    # Monkeypatch DetectionLoop to not actually run
    started = {"n": 0}
    stopped = {"n": 0}

    class FakeLoop:
        def __init__(self, source, aprilcam, ring_buffer, coord_transform=None):
            # Verify source is a DaemonCapture, not a real VideoCapture
            assert isinstance(source, mcp_server.DaemonCapture), (
                f"Expected DaemonCapture, got {type(source)}"
            )
            started["source"] = source

        def start(self):
            started["n"] += 1

        def stop(self, timeout=5.0):
            stopped["n"] += 1

        is_running = True
        frame_count = 0
        error = None
        last_frame = None

    monkeypatch.setattr(mcp_server, "DetectionLoop", FakeLoop)

    # cv2.VideoCapture must not be called with any positional device index
    with patch("cv2.VideoCapture") as mock_vc:
        result = mcp_server._handle_start_detection(cam_id)

    mock_vc.assert_not_called()
    assert result.get("status") == "started", result
    assert started["n"] == 1

    # Cleanup
    mcp_server.detection_registry.pop(cam_id, None)
    mcp_server.registry.close(cam_id)
    mcp_server._cam_info.pop(cam_id, None)


def test_start_detection_no_videocapture_on_cam_handle(monkeypatch) -> None:
    """cam_<N> handles no longer trigger exclusive VideoCapture opens.

    Before 014-006 the server did cv2.VideoCapture(camera_index) for cam_N
    handles.  After the refactor, all opens go through DaemonCapture.
    """
    from aprilcam.server import mcp_server

    dc = _make_daemon_client()
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    cam_id = "cam_7"
    mcp_server.registry.open(None, handle=cam_id)
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    class FakeLoop:
        def __init__(self, source, aprilcam, ring_buffer, coord_transform=None):
            self.source_type = type(source).__name__
        def start(self): pass
        def stop(self, timeout=5.0): pass
        is_running = True
        frame_count = 0
        error = None
        last_frame = None

    monkeypatch.setattr(mcp_server, "DetectionLoop", FakeLoop)

    with patch("cv2.VideoCapture") as mock_vc:
        result = mcp_server._handle_start_detection(cam_id)

    mock_vc.assert_not_called()
    assert "error" not in result, result

    # Cleanup
    mcp_server.detection_registry.pop(cam_id, None)
    mcp_server.registry.close(cam_id)
    mcp_server._cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# Tests: _handle_stop_detection no longer re-opens VideoCapture
# ---------------------------------------------------------------------------


def test_stop_detection_no_videocapture(monkeypatch) -> None:
    """stop_detection does not re-open a VideoCapture to 'restore' the camera."""
    from aprilcam.server import mcp_server
    from aprilcam.core.detection import RingBuffer

    cam_id = "stop-cam-002"

    # Inject a fake DetectionEntry directly (bypass start)
    loop = MagicMock()
    loop.stop = MagicMock()
    buf = RingBuffer(maxlen=10)

    entry = mcp_server.DetectionEntry(
        source_id=cam_id,
        loop=loop,
        ring_buffer=buf,
        aprilcam=MagicMock(),
    )
    entry._camera_id = cam_id  # type: ignore[attr-defined]
    mcp_server.detection_registry[cam_id] = entry

    with patch("cv2.VideoCapture") as mock_vc:
        result = mcp_server._handle_stop_detection(cam_id)

    mock_vc.assert_not_called()
    loop.stop.assert_called_once()
    assert result == {"source_id": cam_id, "status": "stopped"}


# ---------------------------------------------------------------------------
# Tests: _read_one_frame uses gRPC (no AF_UNIX socket)
# ---------------------------------------------------------------------------


def test_read_one_frame_uses_grpc(monkeypatch) -> None:
    """_read_one_frame calls client.capture_frame(), not AF_UNIX socket."""
    from aprilcam.server import mcp_server

    frame = _fake_frame()
    dc = _make_daemon_client(frame)
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    cam_id = "grpc-cam-003"
    mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    import socket as _socket
    with patch.object(_socket, "socket") as mock_sock:
        result = mcp_server._read_one_frame(cam_id)

    # AF_UNIX socket must NOT have been opened
    mock_sock.assert_not_called()
    # Frame came from gRPC
    dc.capture_frame.assert_called_once_with(cam_id)
    assert np.array_equal(result, frame)

    mcp_server._cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# Tests: _frames_from_daemon uses gRPC (no AF_UNIX)
# ---------------------------------------------------------------------------


def test_frames_from_daemon_uses_grpc(monkeypatch) -> None:
    """_frames_from_daemon iterates via gRPC CaptureFrame, not AF_UNIX socket."""
    from aprilcam.server import mcp_server

    frame = _fake_frame()
    dc = _make_daemon_client(frame)
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
    because all opens go through DaemonCapture/gRPC.

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

    class FakeLoop:
        def __init__(self, source, aprilcam, ring_buffer, coord_transform=None):
            pass
        def start(self): pass
        def stop(self, timeout=5.0): pass
        is_running = True
        frame_count = 0
        error = None
        last_frame = None

    monkeypatch.setattr(mcp_server, "DetectionLoop", FakeLoop)

    # Must not raise despite VideoCapture being banned
    result = mcp_server._handle_start_detection(cam_id)

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result.get("status") == "started"

    # Cleanup
    mcp_server.detection_registry.pop(cam_id, None)
    mcp_server.registry.close(cam_id)
    mcp_server._cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# Tests: vision/objects.py ColorCameraThread is NOT reachable from get_objects
# ---------------------------------------------------------------------------


def test_get_objects_does_not_instantiate_color_camera_thread(monkeypatch) -> None:
    """_handle_get_objects reads from det_entry.loop.last_frame, not from
    ColorCameraThread.  ColorCameraThread is DAEMON-ONLY and must never be
    instantiated from the MCP path.
    """
    from aprilcam.server import mcp_server
    from aprilcam.vision import objects as _objects_mod
    from aprilcam.core.detection import RingBuffer

    cam_id = "obj-cam-006"

    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    loop = MagicMock()
    loop.last_frame = frame
    buf = RingBuffer(maxlen=10)

    entry = mcp_server.DetectionEntry(
        source_id=cam_id,
        loop=loop,
        ring_buffer=buf,
        aprilcam=MagicMock(),
    )
    entry._camera_id = cam_id  # type: ignore[attr-defined]
    mcp_server.detection_registry[cam_id] = entry

    instantiated = {"n": 0}
    original_init = _objects_mod.ColorCameraThread.__init__

    def _patched_init(self, *args, **kwargs):
        instantiated["n"] += 1
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(_objects_mod.ColorCameraThread, "__init__", _patched_init)

    mcp_server._handle_get_objects(cam_id)

    assert instantiated["n"] == 0, (
        "ColorCameraThread was instantiated from _handle_get_objects — "
        "it is DAEMON-ONLY and must not be reached from the MCP path"
    )

    # Cleanup
    mcp_server.detection_registry.pop(cam_id, None)
