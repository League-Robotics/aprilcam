"""Tests for 015-003: MCP tag tools wired to daemon RPCs (no in-process detection).

Verifies that:
- stream_tags / start_detection start a TagStreamConsumer backed by the daemon
  and do NOT instantiate DetectionLoop, AprilCam, or RingBuffer.
- get_tags reads the latest TagFrame dict from the history deque.
- get_tag_history returns the last N dicts from the deque.
- get_objects calls client.get_objects() and maps the proto response.
- stop_stream / stop_detection closes the consumer and removes the entry.
"""

from __future__ import annotations

import json
from collections import deque
from unittest.mock import MagicMock, patch, call

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tag_frame_dump(frame_id: int = 1, tag_id: int = 5) -> dict:
    """Return a dict that looks like TagFrame.model_dump()."""
    return {
        "frame_id": frame_id,
        "ts_mono_ns": 0,
        "ts_wall_ms": 0,
        "tags": [
            {
                "id": tag_id,
                "center_px": (100.0, 200.0),
                "corners_px": [(90.0, 190.0), (110.0, 190.0), (110.0, 210.0), (90.0, 210.0)],
                "yaw": 0.5,
                "world_xy": (10.0, 20.0),
                "in_playfield": True,
                "vel_px": (1.0, 0.0),
                "speed_px": 1.0,
                "vel_world": None,
                "speed_world": None,
                "heading_rad": None,
                "age": 0.1,
            }
        ],
        "homography": None,
        "playfield_corners": [],
        "fps": 30.0,
        "field_width_cm": 0.0,
        "field_height_cm": 0.0,
        "origin_x": 0.0,
        "origin_y": 0.0,
    }


# ---------------------------------------------------------------------------
# Test: stream_tags / _handle_start_detection starts a TagStreamConsumer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_tags_uses_get_tag_stream(monkeypatch):
    """stream_tags starts a TagStreamConsumer via client.get_tag_stream, not DetectionLoop."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import stream_tags

    # Ensure we start with a clean registry for this test
    mcp_server.detection_registry.pop("cam_test_stream", None)
    mcp_server.registry._cameras.setdefault("cam_test_stream", None)

    mock_consumer = MagicMock()
    mock_consumer.__iter__ = MagicMock(return_value=iter([]))  # empty stream

    mock_client = MagicMock()
    mock_client.get_tag_stream.return_value = mock_consumer

    # Stub _ensure_daemon_client
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: mock_client)
    # Stub threading to avoid real threads
    with patch("aprilcam.server.mcp_server.threading") as mock_threading:
        mock_thread = MagicMock()
        mock_threading.Thread.return_value = mock_thread

        result_list = await stream_tags("cam_test_stream")

    data = json.loads(result_list[0].text)
    assert "error" not in data, f"Unexpected error: {data}"
    assert data["status"] == "started"
    assert data["stream_id"] == "cam_test_stream"

    # Verify daemon RPC was called
    mock_client.get_tag_stream.assert_called_once()
    # Verify background thread was started
    mock_thread.start.assert_called_once()

    # The detection_registry entry is a DaemonStreamEntry
    entry = mcp_server.detection_registry.get("cam_test_stream")
    assert entry is not None
    assert entry.consumer is mock_consumer

    # Cleanup
    mcp_server.detection_registry.pop("cam_test_stream", None)
    mcp_server.registry._cameras.pop("cam_test_stream", None)


# ---------------------------------------------------------------------------
# Test: get_tags reads from history deque
# ---------------------------------------------------------------------------


def test_get_tags_reads_from_history_deque(monkeypatch):
    """_handle_get_tags returns the latest dict from entry.history."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import _handle_get_tags, DaemonStreamEntry

    dump = _make_tag_frame_dump(frame_id=42, tag_id=7)
    h: deque = deque(maxlen=300)
    h.appendleft(dump)

    mock_consumer = MagicMock()
    entry = DaemonStreamEntry(
        source_id="cam_hist",
        consumer=mock_consumer,
        history=h,
        robot_tag_id=None,
    )
    mcp_server.detection_registry["cam_hist"] = entry

    try:
        result = _handle_get_tags("cam_hist")
    finally:
        mcp_server.detection_registry.pop("cam_hist", None)

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["source_id"] == "cam_hist"
    assert result["frame"] == 42  # frame_id → frame mapping
    assert len(result["tags"]) == 1
    tag = result["tags"][0]
    assert tag["id"] == 7
    assert tag["orientation_yaw"] == 0.5  # yaw → orientation_yaw mapping
    assert tag["center_px"] == [100.0, 200.0]  # tuple → list
    assert tag["world_xy"] == [10.0, 20.0]


# ---------------------------------------------------------------------------
# Test: get_tag_history returns a slice
# ---------------------------------------------------------------------------


def test_get_tag_history_returns_slice(monkeypatch):
    """_handle_get_tag_history returns the last N dicts from the deque."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import _handle_get_tag_history, DaemonStreamEntry

    h: deque = deque(maxlen=300)
    for i in range(10):
        h.appendleft(_make_tag_frame_dump(frame_id=i))

    mock_consumer = MagicMock()
    entry = DaemonStreamEntry(
        source_id="cam_hist2",
        consumer=mock_consumer,
        history=h,
    )
    mcp_server.detection_registry["cam_hist2"] = entry

    try:
        result = _handle_get_tag_history("cam_hist2", num_frames=5)
    finally:
        mcp_server.detection_registry.pop("cam_hist2", None)

    assert "error" not in result
    assert result["source_id"] == "cam_hist2"
    assert len(result["frames"]) == 5
    # The deque is appendleft so newest is first (frame_id 9 at index 0)
    assert result["frames"][0]["frame"] == 9


# ---------------------------------------------------------------------------
# Test: get_objects calls client.get_objects() and maps the response
# ---------------------------------------------------------------------------


def test_get_objects_calls_client_rpc(monkeypatch):
    """_handle_get_objects calls client.get_objects() and maps the proto response."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import _handle_get_objects

    mock_obj = MagicMock()
    mock_obj.cx_px = 50.0
    mock_obj.cy_px = 100.0
    mock_obj.wx = 5.0
    mock_obj.wy = 10.0
    mock_obj.color = "red"
    mock_obj.x_bbox = 40
    mock_obj.y_bbox = 90
    mock_obj.w_bbox = 20
    mock_obj.h_bbox = 20
    mock_obj.area_px = 400.0
    mock_obj.object_type = "square"
    mock_obj.confidence = 0.9

    mock_resp = MagicMock()
    mock_resp.objects = [mock_obj]

    mock_client = MagicMock()
    mock_client.get_objects.return_value = mock_resp

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: mock_client)
    # _resolve_cam_name falls back to source_id when not in registries
    result = _handle_get_objects("cam_obj")

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["source_id"] == "cam_obj"
    assert len(result["objects"]) == 1
    obj = result["objects"][0]
    assert obj["center_px"] == [50.0, 100.0]
    assert obj["world_xy"] == [5.0, 10.0]
    assert obj["color"] == "red"
    assert obj["bbox"] == [40, 90, 20, 20]
    assert obj["area_px"] == 400.0
    assert obj["object_type"] == "square"
    assert obj["confidence"] == 0.9

    mock_client.get_objects.assert_called_once()


def test_get_objects_zero_world_returns_none(monkeypatch):
    """_handle_get_objects maps wx=0, wy=0 to world_xy=None."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import _handle_get_objects

    mock_obj = MagicMock()
    mock_obj.cx_px = 50.0
    mock_obj.cy_px = 100.0
    mock_obj.wx = 0.0
    mock_obj.wy = 0.0
    mock_obj.color = "blue"
    mock_obj.x_bbox = 40
    mock_obj.y_bbox = 90
    mock_obj.w_bbox = 20
    mock_obj.h_bbox = 20
    mock_obj.area_px = 400.0
    mock_obj.object_type = "square"
    mock_obj.confidence = 0.8

    mock_resp = MagicMock()
    mock_resp.objects = [mock_obj]

    mock_client = MagicMock()
    mock_client.get_objects.return_value = mock_resp

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: mock_client)
    result = _handle_get_objects("cam_obj2")

    assert result["objects"][0]["world_xy"] is None


# ---------------------------------------------------------------------------
# Test: stop_stream closes consumer and removes entry
# ---------------------------------------------------------------------------


def test_stop_stream_closes_consumer(monkeypatch):
    """_handle_stop_detection closes the consumer and removes the entry from the registry."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import _handle_stop_detection, DaemonStreamEntry

    mock_consumer = MagicMock()
    done_flag = [False]
    entry = DaemonStreamEntry(
        source_id="cam_stop",
        consumer=mock_consumer,
        _done_flag=done_flag,
    )
    mcp_server.detection_registry["cam_stop"] = entry

    result = _handle_stop_detection("cam_stop")

    assert result == {"source_id": "cam_stop", "status": "stopped"}
    mock_consumer.close.assert_called_once()
    assert done_flag[0] is True  # done flag was set
    assert "cam_stop" not in mcp_server.detection_registry


def test_stop_stream_unknown_source(monkeypatch):
    """_handle_stop_detection returns an error for unknown source_id."""
    from aprilcam.server.mcp_server import _handle_stop_detection

    result = _handle_stop_detection("does_not_exist")
    assert "error" in result


# ---------------------------------------------------------------------------
# Test: get_tags with no active stream falls back to daemon RPC
# ---------------------------------------------------------------------------


def test_get_tags_fallback_to_rpc(monkeypatch):
    """_handle_get_tags falls back to GetTags RPC when no stream is active."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import _handle_get_tags
    from aprilcam.client.models import TagFrame, TagRecord

    # Build a real TagFrame Pydantic model for the mock
    mock_tf = MagicMock(spec=TagFrame)
    mock_tf.model_dump.return_value = _make_tag_frame_dump(frame_id=99, tag_id=3)

    mock_client = MagicMock()
    mock_client.get_tags.return_value = mock_tf

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: mock_client)

    # Ensure no active stream entry for this source
    mcp_server.detection_registry.pop("cam_rpc", None)

    result = _handle_get_tags("cam_rpc")

    # Should succeed using the RPC path
    assert "error" not in result, f"Expected no error but got: {result}"
    assert result["frame"] == 99
    assert result["source_id"] == "cam_rpc"
    mock_client.get_tags.assert_called_once()
