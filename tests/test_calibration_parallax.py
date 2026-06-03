"""Tests for CameraPosition, correct_world_for_height, and updated JSON I/O.

Sprint 008, ticket 001.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.calibration.calibration import (
    CameraCalibration,
    CameraPosition,
    load_calibration_from_camera_dir,
    load_field_dimensions_from_camera_dir,
    save_calibration_to_camera_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDENTITY_H = np.eye(3, dtype=float)


def _minimal_cal(**kwargs) -> CameraCalibration:
    """Return a minimal CameraCalibration suitable for round-trip tests."""
    return CameraCalibration(
        device_name="TestCam",
        resolution=(640, 480),
        homography=_IDENTITY_H.copy(),
        **kwargs,
    )


def _write_json(directory: Path, data: dict) -> None:
    (directory / "calibration.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# correct_world_for_height — identity cases
# ---------------------------------------------------------------------------


def test_correct_world_no_position():
    """camera_position=None → identity."""
    cal = _minimal_cal(camera_position=None)
    result = cal.correct_world_for_height(50.0, 50.0, 12.0)
    assert result == (50.0, 50.0)


def test_correct_world_zero_camera_height():
    """camera_position.height == 0.0 → identity."""
    cal = _minimal_cal(camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=0.0))
    result = cal.correct_world_for_height(50.0, 50.0, 12.0)
    assert result == (50.0, 50.0)


def test_correct_world_zero_tag_height():
    """tag_height_cm == 0.0 → identity regardless of camera_position.height."""
    cal = _minimal_cal(camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=180.0))
    result = cal.correct_world_for_height(50.0, 50.0, 0.0)
    assert result == (50.0, 50.0)


# ---------------------------------------------------------------------------
# correct_world_for_height — numeric correctness
# ---------------------------------------------------------------------------


def test_correct_world_formula():
    """camera (0,0,180), tag (50,50), h=12 → approx (46.667, 46.667)."""
    cal = _minimal_cal(camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=180.0))
    wx, wy = cal.correct_world_for_height(50.0, 50.0, 12.0)
    # r = 12/180 = 1/15; wx_corr = 50 + (1/15)*(0-50) = 50 - 50/15 = 50*(1 - 1/15)
    expected = 50.0 * (1 - 12.0 / 180.0)
    assert abs(wx - expected) < 0.01
    assert abs(wy - expected) < 0.01
    assert abs(wx - 46.667) < 0.01
    assert abs(wy - 46.667) < 0.01


def test_correct_world_formula_with_offset():
    """Verify formula with non-zero camera x/y offset."""
    cal = _minimal_cal(camera_position=CameraPosition(x_offset=10.0, y_offset=5.0, height=100.0))
    wx, wy = cal.correct_world_for_height(60.0, 40.0, 25.0)
    r = 25.0 / 100.0
    expected_x = 60.0 + r * (10.0 - 60.0)
    expected_y = 40.0 + r * (5.0 - 40.0)
    assert abs(wx - expected_x) < 1e-9
    assert abs(wy - expected_y) < 1e-9


# ---------------------------------------------------------------------------
# load_calibration_from_camera_dir — new format
# ---------------------------------------------------------------------------


def test_load_new_format_playfield(tmp_path):
    """Reads playfield:{width,height} into playfield_width_cm/playfield_height_cm."""
    _write_json(tmp_path, {
        "device_name": "TestCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "playfield": {"width": 101.0, "height": 89.0},
    })
    cal = load_calibration_from_camera_dir(tmp_path)
    assert cal is not None
    assert cal.playfield_width_cm == 101.0
    assert cal.playfield_height_cm == 89.0


# ---------------------------------------------------------------------------
# load_calibration_from_camera_dir — old-format fallback
# ---------------------------------------------------------------------------


def test_load_old_format_fallback(tmp_path):
    """Files with field_width_cm / field_height_cm at top level load correctly."""
    _write_json(tmp_path, {
        "device_name": "TestCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "field_width_cm": 101.0,
        "field_height_cm": 89.0,
    })
    cal = load_calibration_from_camera_dir(tmp_path)
    assert cal is not None
    assert cal.playfield_width_cm == 101.0
    assert cal.playfield_height_cm == 89.0


# ---------------------------------------------------------------------------
# load_calibration_from_camera_dir — camera_position
# ---------------------------------------------------------------------------


def test_load_camera_position(tmp_path):
    """Reads camera_position dict into CameraPosition dataclass."""
    _write_json(tmp_path, {
        "device_name": "TestCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "camera_position": {"x_offset": 0.0, "y_offset": 0.0, "height": 180.0},
    })
    cal = load_calibration_from_camera_dir(tmp_path)
    assert cal is not None
    assert isinstance(cal.camera_position, CameraPosition)
    assert cal.camera_position.x_offset == 0.0
    assert cal.camera_position.y_offset == 0.0
    assert cal.camera_position.height == 180.0


def test_load_no_camera_position(tmp_path):
    """Absent camera_position key → None."""
    _write_json(tmp_path, {
        "device_name": "TestCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
    })
    cal = load_calibration_from_camera_dir(tmp_path)
    assert cal is not None
    assert cal.camera_position is None


# ---------------------------------------------------------------------------
# load_calibration_from_camera_dir — tag_heights
# ---------------------------------------------------------------------------


def test_load_ignores_legacy_tag_heights(tmp_path):
    """tag_heights in calibration.json is silently ignored (moved to tags.json)."""
    _write_json(tmp_path, {
        "device_name": "TestCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "tag_heights": {"5": 11.8, "12": 7.5},
    })
    cal = load_calibration_from_camera_dir(tmp_path)
    assert cal is not None
    assert not hasattr(cal, "tag_heights")


# ---------------------------------------------------------------------------
# save_calibration_to_camera_dir — new format written
# ---------------------------------------------------------------------------


def test_old_keys_not_written(tmp_path):
    """Saved calibration must not contain top-level field_width_cm / field_height_cm."""
    cal = _minimal_cal()
    save_calibration_to_camera_dir(cal, tmp_path, field_width_cm=101.0, field_height_cm=89.0)
    data = json.loads((tmp_path / "calibration.json").read_text())
    assert "field_width_cm" not in data
    assert "field_height_cm" not in data
    assert data["playfield"] == {"width": 101.0, "height": 89.0}


def test_save_camera_position(tmp_path):
    """camera_position is written to JSON when present."""
    cal = _minimal_cal(camera_position=CameraPosition(x_offset=5.0, y_offset=2.0, height=150.0))
    save_calibration_to_camera_dir(cal, tmp_path, 101.0, 89.0)
    data = json.loads((tmp_path / "calibration.json").read_text())
    assert data["camera_position"] == {"x_offset": 5.0, "y_offset": 2.0, "height": 150.0}


def test_save_no_tag_heights(tmp_path):
    """tag_heights is not written to calibration.json (moved to tags.json)."""
    cal = _minimal_cal()
    save_calibration_to_camera_dir(cal, tmp_path, 101.0, 89.0)
    data = json.loads((tmp_path / "calibration.json").read_text())
    assert "tag_heights" not in data


def test_user_managed_keys_preserved(tmp_path):
    """User-managed keys in an existing calibration.json survive a save cycle."""
    # Write an existing file with a user-managed key
    (tmp_path / "calibration.json").write_text(json.dumps({
        "device_name": "TestCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "my_custom_setting": "keep_me",
        "field_width_cm": 101.0,
        "field_height_cm": 89.0,
    }))
    cal = _minimal_cal()
    save_calibration_to_camera_dir(cal, tmp_path, 101.0, 89.0)
    data = json.loads((tmp_path / "calibration.json").read_text())
    assert data.get("my_custom_setting") == "keep_me"


# ---------------------------------------------------------------------------
# load_field_dimensions_from_camera_dir
# ---------------------------------------------------------------------------


def test_load_field_dimensions_new_format(tmp_path):
    """Reads new playfield:{width,height} format."""
    _write_json(tmp_path, {
        "playfield": {"width": 120.0, "height": 90.0},
    })
    result = load_field_dimensions_from_camera_dir(tmp_path)
    assert result == (120.0, 90.0)


def test_load_field_dimensions_old_format(tmp_path):
    """Falls back to field_width_cm / field_height_cm."""
    _write_json(tmp_path, {
        "field_width_cm": 101.0,
        "field_height_cm": 89.0,
    })
    result = load_field_dimensions_from_camera_dir(tmp_path)
    assert result == (101.0, 89.0)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path):
    """Save CameraCalibration with camera_position; reload and compare."""
    pos = CameraPosition(x_offset=3.0, y_offset=-1.5, height=200.0)
    cal = _minimal_cal(
        camera_position=pos,
        playfield_width_cm=101.0,
        playfield_height_cm=89.0,
    )
    save_calibration_to_camera_dir(cal, tmp_path, field_width_cm=101.0, field_height_cm=89.0)
    loaded = load_calibration_from_camera_dir(tmp_path)

    assert loaded is not None
    assert loaded.playfield_width_cm == 101.0
    assert loaded.playfield_height_cm == 89.0
    assert loaded.camera_position is not None
    assert loaded.camera_position.x_offset == 3.0
    assert loaded.camera_position.y_offset == -1.5
    assert loaded.camera_position.height == 200.0
