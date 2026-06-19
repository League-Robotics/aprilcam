"""Tests for 015-004: web_server.py rewritten as direct daemon client.

Verifies that:
- The module imports without cv2.
- POST /api/list_cameras calls dc.enumerate_cameras() and returns JSON.
- POST /api/tags calls dc.get_tags() and returns JSON with a 'tags' key.
- POST /api/objects calls dc.get_objects() and returns JSON with an 'objects' key.
- POST /api/where calls dc.where_is() and returns JSON.
- GET /api/frame calls dc.capture_frame_jpeg() and returns image/jpeg.
- POST /api/overlay calls dc.publish_overlay() and returns JSON.
- GET / returns JSON discovery with updated endpoint list.
- WS /ws/tags error when source_id is missing.
"""

from __future__ import annotations

import builtins
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_mock_client() -> MagicMock:
    """Return a fully mocked DaemonControl."""
    dc = MagicMock()

    # enumerate_cameras → list of CameraDevice-like objects
    cam = MagicMock()
    cam.index = 0
    cam.name = "test_camera"
    cam.slug = "test_camera"
    dc.enumerate_cameras.return_value = [cam]

    # get_tags → TagFrame Pydantic model
    from aprilcam.client.models import TagFrame, TagRecord
    tag = TagRecord(
        id=5,
        center_px=(100.0, 200.0),
        corners_px=[(90.0, 190.0), (110.0, 190.0), (110.0, 210.0), (90.0, 210.0)],
        yaw=0.5,
        world_xy=(10.0, 20.0),
        in_playfield=True,
        vel_px=(1.0, 0.0),
        speed_px=1.0,
        vel_world=None,
        speed_world=None,
        heading_rad=None,
        age=0.1,
    )
    frame = TagFrame(
        frame_id=42,
        ts_mono_ns=0,
        ts_wall_ms=0,
        tags=[tag],
        homography=None,
        playfield_corners=[],
        fps=30.0,
        field_width_cm=100.0,
        field_height_cm=90.0,
        origin_x=0.0,
        origin_y=0.0,
    )
    dc.get_tags.return_value = frame

    # get_objects → proto-like response
    obj = MagicMock()
    obj.cx_px = 50.0
    obj.cy_px = 100.0
    obj.wx = 5.0
    obj.wy = 10.0
    obj.color = "red"
    obj.x_bbox = 40
    obj.y_bbox = 90
    obj.w_bbox = 20
    obj.h_bbox = 20
    obj.area_px = 400.0
    obj.object_type = "square"
    obj.confidence = 0.9
    obj_resp = MagicMock()
    obj_resp.objects = [obj]
    dc.get_objects.return_value = obj_resp

    # where_is → dict
    dc.where_is.return_value = {
        "status": "ok",
        "query": "tag 5",
        "tokens": ["tag", "5"],
        "matches": [],
    }

    # capture_frame_jpeg → raw JPEG bytes (minimal valid JPEG marker)
    dc.capture_frame_jpeg.return_value = b"\xff\xd8\xff\xe0fake_jpeg"

    # publish_overlay → True
    dc.publish_overlay.return_value = True

    return dc


def _client_with_mock(mock_dc: MagicMock | None = None):
    """Return (TestClient, mock_dc) with the daemon client pre-seeded.

    The patch must be active when TestClient enters lifespan (startup) so that
    _make_client returns our mock.  We use TestClient as a context manager so
    callers can issue requests inside a ``with`` block.
    """
    from aprilcam.server import web_server

    if mock_dc is None:
        mock_dc = _make_mock_client()

    app = web_server.create_app(daemon_host="localhost", daemon_port=5280)

    # Patch _make_client so that lifespan's startup path returns mock_dc.
    # The patch must be active for the duration of the TestClient lifespan,
    # so we return the patch context manager for callers to use.
    patcher = patch.object(web_server, "_make_client", return_value=mock_dc)
    return app, mock_dc, patcher


# ---------------------------------------------------------------------------
# Test: import without cv2
# ---------------------------------------------------------------------------


def test_import_without_cv2():
    """Importing web_server must succeed even when cv2 raises ImportError."""
    real_import = builtins.__import__

    def blocking_import(name, *a, **kw):
        if name == "cv2" or name.startswith("cv2."):
            raise ImportError("cv2 blocked for test")
        return real_import(name, *a, **kw)

    mod_key = "aprilcam.server.web_server"
    saved = sys.modules.pop(mod_key, None)
    builtins.__import__ = blocking_import
    try:
        import aprilcam.server.web_server  # noqa: F401
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules[mod_key] = saved
        elif mod_key in sys.modules:
            del sys.modules[mod_key]


# ---------------------------------------------------------------------------
# Test: GET / — discovery endpoint
# ---------------------------------------------------------------------------


def test_discovery_json():
    """GET / returns a JSON discovery document listing the new endpoints."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.get("/", headers={"accept": "application/json"})

    assert resp.status_code == 200
    data = resp.json()
    assert "endpoints" in data
    paths = [e["path"] for e in data["endpoints"]]
    # New daemon-backed paths present
    assert "/api/list_cameras" in paths
    assert "/api/tags" in paths
    assert "/api/objects" in paths
    assert "/api/where" in paths
    assert "/api/frame" in paths
    assert "/api/overlay" in paths
    # Old MCP-proxy paths absent
    assert "/api/open_camera" not in paths
    assert "/api/start_detection" not in paths
    assert "/api/get_tags" not in paths


# ---------------------------------------------------------------------------
# Test: POST /api/list_cameras
# ---------------------------------------------------------------------------


def test_list_cameras_calls_enumerate_cameras():
    """POST /api/list_cameras returns cameras from dc.enumerate_cameras()."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.post("/api/list_cameras", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert "cameras" in data
    assert len(data["cameras"]) == 1
    assert data["cameras"][0]["name"] == "test_camera"
    assert data["cameras"][0]["slug"] == "test_camera"
    assert data["cameras"][0]["index"] == 0
    mock_dc.enumerate_cameras.assert_called_once()


# ---------------------------------------------------------------------------
# Test: POST /api/tags
# ---------------------------------------------------------------------------


def test_tags_returns_tags_key():
    """POST /api/tags returns JSON with a 'tags' key from dc.get_tags()."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.post("/api/tags", json={"source_id": "test_camera"})

    assert resp.status_code == 200
    data = resp.json()
    assert "tags" in data
    assert data["source_id"] == "test_camera"
    assert data["frame"] == 42
    assert len(data["tags"]) == 1
    tag = data["tags"][0]
    assert tag["id"] == 5
    assert tag["center_px"] == [100.0, 200.0]
    assert tag["world_xy"] == [10.0, 20.0]
    assert tag["orientation_yaw"] == 0.5
    mock_dc.get_tags.assert_called_once_with("test_camera")


def test_tags_missing_source_id_returns_400():
    """POST /api/tags with no source_id returns 400."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.post("/api/tags", json={})

    assert resp.status_code == 400
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Test: POST /api/objects
# ---------------------------------------------------------------------------


def test_objects_returns_objects_key():
    """POST /api/objects returns JSON with an 'objects' key."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.post("/api/objects", json={"source_id": "test_camera"})

    assert resp.status_code == 200
    data = resp.json()
    assert "objects" in data
    assert data["source_id"] == "test_camera"
    assert len(data["objects"]) == 1
    obj = data["objects"][0]
    assert obj["center_px"] == [50.0, 100.0]
    assert obj["world_xy"] == [5.0, 10.0]
    assert obj["color"] == "red"
    mock_dc.get_objects.assert_called_once_with("test_camera")


def test_objects_zero_world_xy_returns_none():
    """POST /api/objects with wx=0,wy=0 returns world_xy=None."""
    mock_dc = _make_mock_client()
    mock_obj = mock_dc.get_objects.return_value.objects[0]
    mock_obj.wx = 0.0
    mock_obj.wy = 0.0

    app, mock_dc, patcher = _client_with_mock(mock_dc)
    with patcher:
        with TestClient(app) as client:
            resp = client.post("/api/objects", json={"source_id": "test_camera"})

    assert resp.status_code == 200
    obj = resp.json()["objects"][0]
    assert obj["world_xy"] is None


# ---------------------------------------------------------------------------
# Test: POST /api/where
# ---------------------------------------------------------------------------


def test_where_calls_where_is():
    """POST /api/where calls dc.where_is() and returns JSON."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.post("/api/where", json={"query": "tag 5", "source_id": "test_camera"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["query"] == "tag 5"
    mock_dc.where_is.assert_called_once_with(query="tag 5", cam_name="test_camera")


def test_where_missing_query_returns_400():
    """POST /api/where without query returns 400."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.post("/api/where", json={"source_id": "test_camera"})

    assert resp.status_code == 400
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Test: GET /api/frame
# ---------------------------------------------------------------------------


def test_frame_returns_jpeg_content_type():
    """GET /api/frame returns image/jpeg with JPEG bytes (no re-encode)."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.get("/api/frame", params={"source_id": "test_camera"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"\xff\xd8\xff\xe0fake_jpeg"
    mock_dc.capture_frame_jpeg.assert_called_once_with("test_camera")


def test_frame_missing_source_id_returns_400():
    """GET /api/frame without source_id returns 400."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            resp = client.get("/api/frame")

    assert resp.status_code == 400
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Test: POST /api/overlay
# ---------------------------------------------------------------------------


def test_overlay_calls_publish_overlay():
    """POST /api/overlay calls dc.publish_overlay() and returns {status: 'ok'}."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            elements = [{"type": "circle", "params": [10.0, 20.0, 5.0], "color": [255, 0, 0]}]
            resp = client.post("/api/overlay", json={
                "source_id": "test_camera",
                "elements": elements,
                "ttl": 2.0,
            })

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    mock_dc.publish_overlay.assert_called_once_with(
        cam_name="test_camera", elements=elements, ttl=2.0
    )


# ---------------------------------------------------------------------------
# Test: WS /ws/tags — missing source_id closes with error
# ---------------------------------------------------------------------------


def test_ws_tags_missing_source_id_sends_error():
    """WS /ws/tags without source_id sends error JSON and closes."""
    app, mock_dc, patcher = _client_with_mock()
    with patcher:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/tags") as ws:
                msg = ws.receive_json()
                assert "error" in msg


# ---------------------------------------------------------------------------
# Test: WS /ws/tags — streams TagFrame dicts
# ---------------------------------------------------------------------------


def test_ws_tags_streams_tag_frames():
    """WS /ws/tags receives TagFrame JSON messages from the daemon stream."""
    from aprilcam.client.models import TagFrame, TagRecord

    # Build a minimal TagFrame to stream
    tag = TagRecord(
        id=7,
        center_px=(50.0, 60.0),
        corners_px=[(45.0, 55.0), (55.0, 55.0), (55.0, 65.0), (45.0, 65.0)],
        yaw=1.0,
        world_xy=None,
        in_playfield=False,
        vel_px=None,
        speed_px=None,
        vel_world=None,
        speed_world=None,
        heading_rad=None,
        age=0.0,
    )
    tf = TagFrame(
        frame_id=1,
        ts_mono_ns=0,
        ts_wall_ms=0,
        tags=[tag],
        homography=None,
        playfield_corners=[],
        fps=30.0,
        field_width_cm=0.0,
        field_height_cm=0.0,
        origin_x=0.0,
        origin_y=0.0,
    )

    # Mock consumer: yields one TagFrame then raises EOFError
    mock_consumer = MagicMock()
    call_count = [0]

    def _read_once():
        call_count[0] += 1
        if call_count[0] == 1:
            return tf
        raise EOFError("stream done")

    mock_consumer.read.side_effect = _read_once

    mock_dc = _make_mock_client()
    mock_dc.get_tag_stream.return_value = mock_consumer

    app, mock_dc, patcher = _client_with_mock(mock_dc)
    with patcher:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/tags?source_id=test_camera") as ws:
                msg_text = ws.receive_text()
                msg = json.loads(msg_text)

    assert "tags" in msg
    assert msg["source_id"] == "test_camera"
    assert msg["frame"] == 1
    assert len(msg["tags"]) == 1
    assert msg["tags"][0]["id"] == 7

    mock_dc.get_tag_stream.assert_called_once_with("test_camera")
    mock_consumer.close.assert_called_once()
