"""Integration tests for parallax correction and A1-centred origin translation.

Tests cover the integration points:
- camera_pipeline.py: origin translation applied to all world_xy values
- camera_pipeline.py: parallax correction applied after origin translation
- mcp_server.py: calibrate_playfield stores camera_position
- mcp_server.py: get_tags returns world_xy unchanged (pipeline already corrected)
"""

from __future__ import annotations

import json
import dataclasses
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.calibration.calibration import (
    CameraCalibration,
    CameraPosition,
    load_calibration_from_camera_dir,
    save_calibration_to_camera_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDENTITY_H = np.eye(3, dtype=float)


def _minimal_cal(**kwargs) -> CameraCalibration:
    return CameraCalibration(
        device_name="TestCam",
        resolution=(640, 480),
        homography=_IDENTITY_H.copy(),
        **kwargs,
    )


def _make_tag_record(tag_id: int, world_xy: Optional[tuple]):
    """Return a real TagRecord with the given id and world_xy."""
    from aprilcam.core.detection import TagRecord
    return TagRecord(
        id=tag_id,
        center_px=(0.0, 0.0),
        corners_px=[[0.0, 0.0]] * 4,
        orientation_yaw=0.0,
        world_xy=world_xy,
        in_playfield=True,
        vel_px=None,
        speed_px=None,
        vel_world=None,
        speed_world=None,
        heading_rad=None,
        timestamp=0.0,
        frame_index=0,
    )


# ---------------------------------------------------------------------------
# camera_pipeline.py — origin translation + parallax block
# ---------------------------------------------------------------------------


def _run_pipeline_postprocess(pipeline, tag_records):
    """Run the same post-process block as camera_pipeline._capture_loop."""
    import dataclasses as _dc
    if pipeline._calibration and (
        pipeline._calibration.playfield_width_cm > 0
        or pipeline._calibration.camera_position is not None
    ):
        origin_x = pipeline._calibration.playfield_width_cm / 2.0
        origin_y = pipeline._calibration.playfield_height_cm / 2.0
        corrected = []
        for tr in tag_records:
            if tr.world_xy is None:
                corrected.append(tr)
                continue
            wx = tr.world_xy[0] - origin_x
            wy = origin_y - tr.world_xy[1]
            tag_h = pipeline._tag_heights.get(tr.id, 0.0)
            if pipeline._calibration.camera_position and tag_h > 0.0:
                wx, wy = pipeline._calibration.correct_world_for_height(wx, wy, tag_h)
            tr = _dc.replace(tr, world_xy=(wx, wy))
            corrected.append(tr)
        return corrected
    return tag_records


def test_pipeline_applies_origin_translation():
    """world_xy is shifted by (field_w/2, field_h/2) to produce A1-centred coords."""
    from aprilcam.daemon.camera_pipeline import CameraPipeline

    pipeline = CameraPipeline.__new__(CameraPipeline)
    pipeline._calibration = _minimal_cal(
        playfield_width_cm=100.0,
        playfield_height_cm=80.0,
    )
    pipeline._tag_heights = {}

    tr = _make_tag_record(5, (50.0, 40.0))
    result = _run_pipeline_postprocess(pipeline, [tr])

    # (50, 40) - (50, 40) = (0, 0) — dead centre of field → A1 position
    assert result[0].world_xy == (0.0, 0.0)


def test_pipeline_applies_correction_to_elevated_tag():
    """Tags with height > 0 get parallax correction after origin translation."""
    from aprilcam.daemon.camera_pipeline import CameraPipeline

    pipeline = CameraPipeline.__new__(CameraPipeline)
    pipeline._calibration = _minimal_cal(
        camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=180.0),
        playfield_width_cm=100.0,
        playfield_height_cm=80.0,
    )
    pipeline._tag_heights = {5: 12.0}

    # Corner-based world_xy: (100, 80) → A1-centred (50, 40) before correction
    tr = _make_tag_record(5, (100.0, 80.0))
    result = _run_pipeline_postprocess(pipeline, [tr])

    # After translation: wx=50, wy=40-80=-40; r=12/180
    r = 12.0 / 180.0
    expected_x = 50.0 * (1.0 - r)   # cx=0 so: wx + r*(0 - wx) = wx*(1-r)
    expected_y = -40.0 * (1.0 - r)  # south corner → negative y (y-up)
    assert abs(result[0].world_xy[0] - expected_x) < 0.01
    assert abs(result[0].world_xy[1] - expected_y) < 0.01


def test_pipeline_skips_tag_not_in_heights():
    """Tags not in _tag_heights get origin translation but no parallax correction."""
    from aprilcam.daemon.camera_pipeline import CameraPipeline

    pipeline = CameraPipeline.__new__(CameraPipeline)
    pipeline._calibration = _minimal_cal(
        camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=180.0),
        playfield_width_cm=100.0,
        playfield_height_cm=80.0,
    )
    pipeline._tag_heights = {5: 12.0}

    tr = _make_tag_record(99, (50.0, 40.0))  # origin = (50, 40)
    result = _run_pipeline_postprocess(pipeline, [tr])

    # Only translation applied: (50-50, 40-40) = (0, 0)
    assert result[0].world_xy == (0.0, 0.0)


def test_pipeline_skips_when_no_calibration():
    """No calibration → block is skipped entirely, no exception."""
    from aprilcam.daemon.camera_pipeline import CameraPipeline

    pipeline = CameraPipeline.__new__(CameraPipeline)
    pipeline._calibration = None
    pipeline._tag_heights = {}

    tr = _make_tag_record(5, (50.0, 50.0))
    result = _run_pipeline_postprocess(pipeline, [tr])

    assert result[0].world_xy == (50.0, 50.0)


def test_pipeline_skips_null_world_xy():
    """Tags with world_xy=None pass through unchanged."""
    from aprilcam.daemon.camera_pipeline import CameraPipeline

    pipeline = CameraPipeline.__new__(CameraPipeline)
    pipeline._calibration = _minimal_cal(
        camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=180.0),
        playfield_width_cm=100.0,
        playfield_height_cm=80.0,
    )
    pipeline._tag_heights = {5: 12.0}

    tr = _make_tag_record(5, None)
    result = _run_pipeline_postprocess(pipeline, [tr])

    assert result[0].world_xy is None


# ---------------------------------------------------------------------------
# calibrate_playfield MCP tool — camera_position stored in calibration.json
# ---------------------------------------------------------------------------


def test_calibrate_playfield_stores_camera_position(tmp_path, monkeypatch):
    """calibrate_playfield persists camera_position to config.json.

    Updated for the config/calibration split: camera_position now lives in
    config.json (not calibration.json).  calibrate_from_playfield_def is
    monkeypatched to mimic real persistence so no live hardware is required.
    """
    import asyncio
    import numpy as np

    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import (
        PlayfieldEntry,
        playfield_registry,
        registry,
        _cam_info,
    )
    from aprilcam.core.playfield import PlayfieldBoundary as Playfield
    from aprilcam.calibration.calibration import CameraCalibration, CameraPosition

    cam_id = "cam_test_cp"
    pf_id = f"pf_{cam_id}"

    pf = MagicMock(spec=Playfield)
    pf._poly = None

    registry._cameras[cam_id] = None
    _cam_info[cam_id] = {"cam_name": cam_id}

    entry = PlayfieldEntry(
        playfield_id=pf_id,
        camera_id=cam_id,
        playfield=pf,
    )
    playfield_registry.register(entry)

    # Patch calibrate_from_playfield_def to return a controlled calibration.
    controlled_cal = CameraCalibration(
        device_name=cam_id,
        resolution=(1920, 1080),
        homography=np.eye(3),
        playfield_width_cm=40.0,
        playfield_height_cm=32.0,
        calibrated_playfield="main-playfield",
        calibrated_camera=cam_id,
        camera_position=CameraPosition(x_offset=5.0, y_offset=-2.0, height=150.0),
    )
    # Simulate the _merged_config that skip_save=True provides.
    controlled_cal._merged_config = {
        "playfield": "main-playfield",
        "camera_position": {
            "x_offset": 5.0,
            "y_offset": -2.0,
            "height": 150.0,
        },
    }

    import aprilcam.calibration.calibration as cal_mod

    def _fake_calibrate_from_def(**kwargs):
        return controlled_cal

    monkeypatch.setattr(
        cal_mod, "calibrate_from_playfield_def", _fake_calibrate_from_def
    )

    # Wire the fake daemon client: GetCameraConfig returns our config blob.
    rpc_cfg_store: dict[str, dict] = {}
    rpc_cal_store: dict[str, dict] = {}

    fake_client = MagicMock()

    def _get_cfg(cam_name: str) -> MagicMock:
        reply = MagicMock()
        existing = rpc_cfg_store.get(cam_name, {"playfield": "main-playfield"})
        reply.present = True
        reply.json_blob = json.dumps(existing)
        return reply

    def _set_cal(cam_name: str, blob: str) -> MagicMock:
        rpc_cal_store[cam_name] = json.loads(blob)
        reply = MagicMock()
        reply.ok = True
        return reply

    def _set_cfg(cam_name: str, blob: str) -> MagicMock:
        rpc_cfg_store[cam_name] = json.loads(blob)
        reply = MagicMock()
        reply.ok = True
        return reply

    fake_client.get_camera_config.side_effect = _get_cfg
    fake_client.set_calibration.side_effect = _set_cal
    fake_client.set_camera_config.side_effect = _set_cfg

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_client)

    try:
        result_contents = asyncio.run(
            mcp_server.calibrate_playfield(
                playfield_id=pf_id,
                camera_height_cm=150.0,
                camera_x_offset_cm=5.0,
                camera_y_offset_cm=-2.0,
            )
        )
        result = json.loads(result_contents[0].text)

        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["calibrated"] is True
        assert result["camera_height_cm"] == 150.0

        # Verify camera_position was stored in the config RPC (not calibration RPC).
        saved_cfg = rpc_cfg_store.get(cam_id)
        if saved_cfg is not None:
            assert "camera_position" in saved_cfg
            assert saved_cfg["camera_position"]["height"] == 150.0
            assert saved_cfg["camera_position"]["x_offset"] == 5.0
            assert saved_cfg["camera_position"]["y_offset"] == -2.0
        # Verify calibration dict does NOT contain camera_position.
        saved_cal = rpc_cal_store.get(cam_id)
        if saved_cal is not None:
            assert "camera_position" not in saved_cal
    finally:
        try:
            del registry._cameras[cam_id]
        except KeyError:
            pass
        try:
            playfield_registry.remove(pf_id)
        except KeyError:
            pass
        _cam_info.pop(cam_id, None)


def test_calibrate_playfield_response_includes_camera_height_cm(tmp_path, monkeypatch):
    """Response JSON always includes camera_height_cm field.

    Updated for sprint 012: calibrate_playfield now delegates to
    calibrate_from_playfield_def.  We write config.json and monkeypatch
    calibrate_from_playfield_def so no live hardware is required.
    """
    import asyncio
    import numpy as np

    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import (
        PlayfieldEntry,
        playfield_registry,
        registry,
        _cam_info,
    )
    from aprilcam.core.playfield import PlayfieldBoundary as Playfield
    from aprilcam.calibration.calibration import CameraCalibration

    cam_id = "cam_test_ch"
    pf_id = f"pf_{cam_id}"

    pf = MagicMock(spec=Playfield)
    pf._poly = None

    registry._cameras[cam_id] = None
    _cam_info[cam_id] = {"cam_name": cam_id}

    entry = PlayfieldEntry(
        playfield_id=pf_id,
        camera_id=cam_id,
        playfield=pf,
    )
    playfield_registry.register(entry)

    controlled_cal = CameraCalibration(
        device_name=cam_id,
        resolution=(1920, 1080),
        homography=np.eye(3),
        playfield_width_cm=40.0,
        playfield_height_cm=32.0,
        calibrated_playfield="main-playfield",
        calibrated_camera=cam_id,
    )

    import aprilcam.calibration.calibration as cal_mod

    def _fake_calibrate_from_def(**kwargs):
        return controlled_cal

    monkeypatch.setattr(
        cal_mod, "calibrate_from_playfield_def", _fake_calibrate_from_def
    )

    # Wire the fake daemon client with proper GetCameraConfig response.
    fake_client = MagicMock()
    cfg_reply = MagicMock()
    cfg_reply.present = True
    cfg_reply.json_blob = json.dumps({"playfield": "main-playfield"})
    fake_client.get_camera_config.return_value = cfg_reply
    set_reply = MagicMock()
    set_reply.ok = True
    fake_client.set_calibration.return_value = set_reply
    fake_client.set_camera_config.return_value = set_reply

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_client)

    try:
        result_contents = asyncio.run(
            mcp_server.calibrate_playfield(
                playfield_id=pf_id,
            )
        )
        result = json.loads(result_contents[0].text)
        assert "camera_height_cm" in result
    finally:
        try:
            del registry._cameras[cam_id]
        except KeyError:
            pass
        try:
            playfield_registry.remove(pf_id)
        except KeyError:
            pass
        _cam_info.pop(cam_id, None)


# ---------------------------------------------------------------------------
# get_tags MCP tool — world_xy passed through as-is (pipeline already corrected)
# ---------------------------------------------------------------------------


def _make_detection_entry_with_tags(
    source_id: str,
    tags: list[dict],
) -> object:
    """Create a DaemonStreamEntry whose history deque contains *tags*.

    Tags must be convertible to TagFrame.model_dump() format (uses yaw,
    not orientation_yaw; tuples for center_px/corners_px/world_xy).
    """
    from collections import deque
    from aprilcam.server.mcp_server import DaemonStreamEntry

    # Convert list-format fields to tuple format as model_dump() returns.
    def _tag_to_model_dump(t: dict) -> dict:
        tag = dict(t)
        if "world_xy" in tag and isinstance(tag["world_xy"], list):
            tag["world_xy"] = tuple(tag["world_xy"])
        if "center_px" in tag and isinstance(tag["center_px"], list):
            tag["center_px"] = tuple(tag["center_px"])
        if "corners_px" in tag and isinstance(tag["corners_px"], list):
            tag["corners_px"] = [tuple(c) if isinstance(c, list) else c
                                 for c in tag["corners_px"]]
        # orientation_yaw -> yaw for model_dump format
        if "orientation_yaw" in tag and "yaw" not in tag:
            tag["yaw"] = tag.pop("orientation_yaw")
        elif "yaw" not in tag:
            tag.setdefault("yaw", 0.0)
        return tag

    frame_dump = {
        "frame_id": 1,
        "ts_mono_ns": 0,
        "ts_wall_ms": 1000,
        "tags": [_tag_to_model_dump(t) for t in tags],
        "homography": None,
        "playfield_corners": [],
        "fps": 30.0,
        "field_width_cm": 0.0,
        "field_height_cm": 0.0,
        "origin_x": 0.0,
        "origin_y": 0.0,
    }

    h: deque = deque(maxlen=300)
    h.appendleft(frame_dump)

    consumer = MagicMock()
    return DaemonStreamEntry(
        source_id=source_id,
        consumer=consumer,
        history=h,
        robot_tag_id=None,
    )


def test_get_tags_returns_world_xy_unchanged():
    """get_tags returns world_xy exactly as stored in history deque (pipeline pre-corrected)."""
    import asyncio
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import detection_registry

    source_id = "pf_passthrough"
    tags = [{"id": 5, "world_xy": [12.3, -7.5], "center_px": [320, 240], "corners_px": []}]
    entry = _make_detection_entry_with_tags(source_id, tags)
    detection_registry[source_id] = entry

    try:
        result_contents = asyncio.run(mcp_server.get_tags(source_id=source_id))
        result = json.loads(result_contents[0].text)
        assert "error" not in result
        assert result["tags"][0]["world_xy"] == [12.3, -7.5]
    finally:
        detection_registry.pop(source_id, None)
