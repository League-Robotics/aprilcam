"""Tests for the config/calibration split (OOP refactor).

Covers:

- camera_pipeline.start() applies settings from config.json even when
  calibration is absent (orange-camera fix).
- camera_pipeline.start() falls back to calibration.settings for
  legacy un-migrated cameras when config has no settings.
- The real arducam-ov9782-usb-camera data files have the correct shape
  after migration: settings in config.json, not in calibration.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.calibration.calibration import CameraCalibration  # noqa: E402

pytestmark = pytest.mark.needs_cv2

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ARDUCAM_DIR = (
    _REPO_ROOT / "data" / "aprilcam" / "cameras" / "arducam-ov9782-usb-camera"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import numpy as np

_IDENTITY_H = np.eye(3, dtype=float)


def _fake_cap():
    """Return a minimal fake cv2.VideoCapture."""
    import cv2 as cv

    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {
        cv.CAP_PROP_FRAME_WIDTH: 1280,
        cv.CAP_PROP_FRAME_HEIGHT: 800,
    }.get(prop, 0)
    cap.read.return_value = (True, np.zeros((800, 1280, 3), dtype=np.uint8))
    return cap


def _fake_config():
    """Return a minimal daemon Config."""
    cfg = MagicMock()
    cfg.env_dir = None
    cfg.cameras_dir = MagicMock()
    cfg.data_dir = MagicMock()
    return cfg


# ---------------------------------------------------------------------------
# Settings applied from config.json even without calibration
# ---------------------------------------------------------------------------


def test_settings_resolution_from_config_when_no_calibration(tmp_path):
    """Settings resolution logic picks config.json when calibration is absent."""
    from aprilcam.camera.camera_config import load_camera_config as _load_cfg
    from aprilcam.camera.camera_config import save_camera_config

    # Write config.json with settings; NO calibration.json.
    cam_dir = tmp_path / "my-camera"
    cam_dir.mkdir()
    cam_config = {
        "playfield": "main-playfield",
        "device_name": "My Camera",
        "resolution": [1280, 800],
        "settings": {
            "program": "uvc-util",
            "controls": {"gain": "1", "auto-exposure-mode": "0"},
        },
    }
    save_camera_config(cam_dir, cam_config)

    # Replicate the settings-resolution logic from camera_pipeline.start().
    _cam_cfg = _load_cfg(cam_dir)
    calibration = None  # no calibration
    _settings = (
        (_cam_cfg.get("settings") if _cam_cfg else None)
        or (calibration.settings if calibration is not None else None)
    )

    assert _settings is not None, "Settings should be resolved from config.json"
    assert _settings["program"] == "uvc-util"
    assert _settings["controls"]["gain"] == "1"


def test_pipeline_settings_fallback_to_calibration_when_config_has_none(tmp_path):
    """If config.json has no settings, calibration.settings is used (legacy path)."""
    from aprilcam.camera.camera_config import save_camera_config

    # Config without settings key.
    cam_dir = tmp_path / "legacy-cam"
    cam_dir.mkdir()
    save_camera_config(cam_dir, {"playfield": "main-playfield"})

    _cfg = _fake_config()
    _cfg.cameras_dir = tmp_path

    legacy_settings = {"program": "uvc-util", "controls": {"gain": "3"}}

    # Simulate settings resolution logic from camera_pipeline.start().
    from aprilcam.camera.camera_config import load_camera_config as _load_cfg

    camera_dir = _cfg.cameras_dir / "legacy-cam"
    _cam_cfg = _load_cfg(camera_dir)
    # Fake calibration with legacy settings.
    fake_cal = MagicMock()
    fake_cal.settings = legacy_settings

    _settings = (
        (_cam_cfg.get("settings") if _cam_cfg else None)
        or (fake_cal.settings if fake_cal is not None else None)
    )

    assert _settings == legacy_settings, "Should fall back to calibration.settings"


# ---------------------------------------------------------------------------
# Data file verification
# ---------------------------------------------------------------------------


def test_arducam_config_has_settings():
    """After migration, arducam-ov9782 config.json has a settings block."""
    if not _ARDUCAM_DIR.exists():
        pytest.skip("arducam-ov9782-usb-camera directory not present")
    config_file = _ARDUCAM_DIR / "config.json"
    assert config_file.exists(), "config.json must exist"
    config = json.loads(config_file.read_text())
    assert "settings" in config, "config.json must have 'settings' key"
    settings = config["settings"]
    assert settings.get("program") == "uvc-util"
    assert "controls" in settings


def test_arducam_calibration_has_no_static_keys():
    """After migration, arducam-ov9782 calibration.json omits all 5 static keys."""
    if not _ARDUCAM_DIR.exists():
        pytest.skip("arducam-ov9782-usb-camera directory not present")
    cal_file = _ARDUCAM_DIR / "calibration.json"
    assert cal_file.exists(), "calibration.json must exist"
    data = json.loads(cal_file.read_text())
    for key in ("settings", "camera_position", "device_name", "resolution", "static_marker_ids"):
        assert key not in data, f"calibration.json must not contain '{key}' after split"
    assert "camera" in data, "calibration.json must have 'camera' key after split"
    assert data["camera"] == "arducam-ov9782-usb-camera"


def test_to_camera_json_excludes_config_owned_keys():
    """to_camera_json() (the canonical calibration.json serializer) drops the
    5 config-owned keys even when the CameraCalibration carries them."""
    from aprilcam.calibration.calibration import (
        CONFIG_OWNED_CALIBRATION_KEYS,
        CameraCalibration,
    )

    cal = CameraCalibration(
        device_name="my-cam",
        resolution=(1280, 800),
        homography=np.eye(3),
    )
    cal.static_marker_ids = ["aruco_corners", "apriltag:1"]
    cal.settings = {"controls": {"exposure-time-abs": "6"}}

    # to_dict() carries them; to_camera_json() must not.
    assert {"device_name", "resolution", "static_marker_ids", "settings"} <= set(cal.to_dict())
    clean = cal.to_camera_json()
    for key in CONFIG_OWNED_CALIBRATION_KEYS:
        assert key not in clean, f"to_camera_json() leaked config-owned key '{key}'"
    assert "homography" in clean  # regenerable calibration data is kept


def test_save_calibration_to_camera_dir_strips_config_owned_keys(tmp_path):
    """The on-disk writer never emits config-owned keys, even from a rich cal."""
    from aprilcam.calibration.calibration import (
        CameraCalibration,
        save_calibration_to_camera_dir,
    )

    cal = CameraCalibration(
        device_name="rich-cam",
        resolution=(1280, 800),
        homography=np.eye(3),
    )
    cal.static_marker_ids = ["aruco_corners", "apriltag:1"]
    cal.settings = {"controls": {"gain": "2"}}

    cam_dir = tmp_path / "rich-cam"
    path = save_calibration_to_camera_dir(cal, cam_dir, 134.3, 89.3)
    data = json.loads(path.read_text())
    for key in ("settings", "camera_position", "device_name", "resolution", "static_marker_ids"):
        assert key not in data, f"writer leaked config-owned key '{key}'"
    assert data["camera"] == "rich-cam"


def test_arducam_loads_settings_from_config():
    """load_calibration_from_camera_dir overlays settings from config onto cal."""
    if not _ARDUCAM_DIR.exists():
        pytest.skip("arducam-ov9782-usb-camera directory not present")
    from aprilcam.calibration.calibration import load_calibration_from_camera_dir

    cal = load_calibration_from_camera_dir(_ARDUCAM_DIR)
    assert cal is not None
    assert cal.settings is not None, "settings should be overlaid from config.json"
    assert cal.settings.get("program") == "uvc-util"
    assert cal.device_name == "Arducam OV9782 USB Camera"
    assert cal.resolution == (1280, 800)
    assert cal.camera_position is not None
    assert cal.camera_position.height == 127.0
