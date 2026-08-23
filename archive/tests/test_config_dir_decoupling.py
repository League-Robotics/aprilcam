"""Tests for ticket 019-002 — decoupling config reads from calibration reads.

Covers the new optional ``config_dir`` parameters added to:

- ``calibration.load_calibration_from_camera_dir`` — config.json overlay may
  resolve against a different root than calibration.json.
- ``calibration.calibrate_from_playfield_def`` — daemon-local write branch
  may write config.json and calibration.json to two different roots.
- ``core.playfield.Playfield._load_static_marker_ids`` — config.json lookup
  may resolve against an explicit config root instead of a sibling of the
  calibration file.

All new parameters are additive and optional; omitting them must reproduce
today's exact sibling-read/sibling-write behavior (backward-compat guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

import cv2 as cv  # noqa: E402

from aprilcam.calibration.calibration import (  # noqa: E402
    CameraCalibration,
    calibrate_from_playfield_def,
    load_calibration_from_camera_dir,
)
from aprilcam.camera.camera_config import save_camera_config  # noqa: E402

pytestmark = pytest.mark.needs_cv2


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_calibrate_from_def.py fixtures)
# ---------------------------------------------------------------------------

_IDENTITY_H = np.eye(3, dtype=float)


class _FakePlayfieldDef:
    width_cm: float = 134.3
    height_cm: float = 89.3

    def corner_aruco_ids(self) -> list[int]:
        return [1, 3, 5, 7]

    def corner_world_coords(self) -> list[tuple[float, float]]:
        return [(-67.0, 44.65), (67.0, 44.65), (67.0, -44.65), (-67.0, -44.65)]


class _FakeRegistry:
    def __init__(self, playfields: dict | None = None) -> None:
        self._defs = playfields or {}

    def get(self, name: str) -> object:
        return self._defs[name]

    def list(self) -> list[str]:
        return sorted(self._defs.keys())


class _FakeCap:
    def __init__(self, w: int = 1920, h: int = 1080) -> None:
        self._w, self._h = w, h

    def get(self, prop: int) -> float:
        if prop == cv.CAP_PROP_FRAME_WIDTH:
            return float(self._w)
        if prop == cv.CAP_PROP_FRAME_HEIGHT:
            return float(self._h)
        return 0.0

    def read(self):  # noqa: ANN201
        return False, None


def _make_corner_tags() -> Dict[int, np.ndarray]:
    return {
        -2: np.array([100.0, 100.0]),
        -4: np.array([1820.0, 100.0]),
        -6: np.array([1820.0, 980.0]),
        -8: np.array([100.0, 980.0]),
    }


def _write_calibration(camera_dir: Path, **extra) -> None:
    from aprilcam.calibration.calibration import save_calibration_to_camera_dir

    cal = CameraCalibration(
        device_name="ignored-from-calibration",
        resolution=(999, 999),
        homography=_IDENTITY_H,
    )
    for k, v in extra.items():
        setattr(cal, k, v)
    save_calibration_to_camera_dir(cal, camera_dir, 134.3, 89.3)


# ---------------------------------------------------------------------------
# load_calibration_from_camera_dir(config_dir=...)
# ---------------------------------------------------------------------------


def test_loader_reads_config_from_separate_config_dir(tmp_path: Path) -> None:
    """config.json overlay comes from config_dir, calibration geometry from camera_dir."""
    state_root = tmp_path / "state"
    config_root = tmp_path / "config"
    camera_dir = state_root / "my-cam"
    camera_dir.mkdir(parents=True)

    _write_calibration(camera_dir)

    save_camera_config(
        config_root / "my-cam",
        {
            "device_name": "Config-Root Camera",
            "resolution": [1280, 800],
            "static_marker_ids": ["aruco_corners", "apriltag:1"],
        },
    )

    cal = load_calibration_from_camera_dir(camera_dir, config_dir=config_root)
    assert cal is not None
    assert cal.device_name == "Config-Root Camera"
    assert cal.resolution == (1280, 800)
    assert cal.static_marker_ids == ["aruco_corners", "apriltag:1"]


def test_loader_config_dir_none_is_unchanged_sibling_behavior(tmp_path: Path) -> None:
    """config_dir=None (default) reproduces today's sibling-read behavior exactly."""
    camera_dir = tmp_path / "shared-cam"
    camera_dir.mkdir()
    _write_calibration(camera_dir)
    save_camera_config(camera_dir, {"device_name": "Shared Dir Camera", "resolution": [640, 480]})

    cal_default = load_calibration_from_camera_dir(camera_dir)
    cal_explicit_none = load_calibration_from_camera_dir(camera_dir, config_dir=None)

    assert cal_default is not None
    assert cal_explicit_none is not None
    assert cal_default.device_name == cal_explicit_none.device_name == "Shared Dir Camera"
    assert cal_default.resolution == cal_explicit_none.resolution == (640, 480)


def test_loader_state_dir_config_json_absent_but_present_under_config_dir(
    tmp_path: Path,
) -> None:
    """calibration.json in state dir, NO config.json alongside it, but a
    config.json under config_dir/<slug>/ still yields a fully-populated
    calibration when config_dir is passed."""
    state_root = tmp_path / "state"
    config_root = tmp_path / "config"
    camera_dir = state_root / "cam-x"
    camera_dir.mkdir(parents=True)

    _write_calibration(camera_dir)
    assert not (camera_dir / "config.json").exists()

    save_camera_config(
        config_root / "cam-x",
        {
            "device_name": "Cam X",
            "resolution": [800, 600],
            "settings": {"program": "uvc-util"},
            "static_marker_ids": ["aruco_corners"],
            "camera_position": {"x_offset": 1.0, "y_offset": 2.0, "height": 100.0},
        },
    )

    cal = load_calibration_from_camera_dir(camera_dir, config_dir=config_root)
    assert cal is not None
    assert cal.device_name == "Cam X"
    assert cal.resolution == (800, 600)
    assert cal.settings == {"program": "uvc-util"}
    assert cal.static_marker_ids == ["aruco_corners"]
    assert cal.camera_position is not None
    assert cal.camera_position.height == 100.0


def test_loader_camera_config_dict_bypasses_config_dir_entirely(tmp_path: Path) -> None:
    """When camera_config dict is passed, config_dir has no effect at all."""
    state_root = tmp_path / "state"
    config_root = tmp_path / "config"
    camera_dir = state_root / "cam-y"
    camera_dir.mkdir(parents=True)
    _write_calibration(camera_dir)

    # config_root holds a DIFFERENT config.json — must be ignored.
    save_camera_config(config_root / "cam-y", {"device_name": "Should Not Be Used"})

    pre_loaded = {"device_name": "Pre-Loaded Dict Camera", "resolution": [1920, 1080]}
    cal = load_calibration_from_camera_dir(
        camera_dir, camera_config=pre_loaded, config_dir=config_root
    )
    assert cal is not None
    assert cal.device_name == "Pre-Loaded Dict Camera"
    assert cal.resolution == (1920, 1080)


# ---------------------------------------------------------------------------
# calibrate_from_playfield_def(config_dir=...) — daemon-local write branch
# ---------------------------------------------------------------------------


def test_calibrate_from_def_writes_state_and_config_to_separate_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    import aprilcam.calibration.homography as hm

    state_dir = tmp_path / "state" / "cam-z"
    config_dir = tmp_path / "config" / "cam-z"
    state_dir.mkdir(parents=True)

    (state_dir / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(_make_corner_tags()))

    calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=state_dir,
        camera_slug="cam-z",
        playfield_def_registry=registry,
        correct_distortion=False,
        config_dir=config_dir,
    )

    assert (state_dir / "calibration.json").exists()
    assert (config_dir / "config.json").exists()
    # config.json only written under config_dir, not re-written into state_dir
    # (the pre-existing state_dir/config.json from setup is untouched content-wise,
    # but the *new* merged config must land in config_dir).
    written_config = json.loads((config_dir / "config.json").read_text())
    assert written_config["playfield"] == "main-playfield"
    assert written_config["resolution"] == [1920, 1080]


def test_calibrate_from_def_config_dir_defaults_to_camera_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """Omitting config_dir writes both files to camera_dir (backward-compat)."""
    import aprilcam.calibration.homography as hm

    camera_dir = tmp_path / "shared"
    camera_dir.mkdir()
    (camera_dir / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(_make_corner_tags()))

    calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=camera_dir,
        camera_slug="shared-cam",
        playfield_def_registry=registry,
        correct_distortion=False,
    )

    assert (camera_dir / "calibration.json").exists()
    assert (camera_dir / "config.json").exists()


def test_calibrate_from_def_skip_save_writes_neither_file(tmp_path: Path, monkeypatch) -> None:
    import aprilcam.calibration.homography as hm

    state_dir = tmp_path / "state" / "cam-w"
    config_dir = tmp_path / "config" / "cam-w"
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(_make_corner_tags()))

    calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=None,
        camera_slug="cam-w",
        playfield_def_registry=registry,
        correct_distortion=False,
        camera_config={"playfield": "main-playfield"},
        skip_save=True,
        config_dir=config_dir,
    )

    assert not state_dir.exists()
    assert not config_dir.exists()


# ---------------------------------------------------------------------------
# Playfield._load_static_marker_ids(config_dir=...)
# ---------------------------------------------------------------------------


def _make_playfield_stub():
    """Build a bare Playfield instance without running __init__ (avoids
    needing a real camera / detector stack)."""
    from aprilcam.core.playfield import Playfield

    return object.__new__(Playfield)


def test_load_static_marker_ids_explicit_config_dir(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    config_root = tmp_path / "config"
    cal_dir = state_root / "cam-a"
    cal_dir.mkdir(parents=True)
    cal_path = cal_dir / "calibration.json"
    cal_path.write_text(json.dumps({"static_marker_ids": ["from-calibration"]}), encoding="utf-8")

    save_camera_config(config_root / "cam-a", {"static_marker_ids": ["from-config-dir"]})

    pf = _make_playfield_stub()
    ids = pf._load_static_marker_ids(str(cal_path), config_dir=config_root)
    assert ids == ["from-config-dir"]


def test_load_static_marker_ids_config_dir_none_is_sibling_regression(tmp_path: Path) -> None:
    cal_dir = tmp_path / "cam-b"
    cal_dir.mkdir()
    cal_path = cal_dir / "calibration.json"
    cal_path.write_text(json.dumps({"static_marker_ids": ["from-calibration"]}), encoding="utf-8")
    save_camera_config(cal_dir, {"static_marker_ids": ["from-sibling-config"]})

    pf = _make_playfield_stub()
    ids_default = pf._load_static_marker_ids(str(cal_path))
    ids_explicit_none = pf._load_static_marker_ids(str(cal_path), config_dir=None)

    assert ids_default == ["from-sibling-config"]
    assert ids_explicit_none == ["from-sibling-config"]


def test_load_static_marker_ids_config_dir_wins_over_sibling_and_calibration(
    tmp_path: Path,
) -> None:
    """When both a sibling config.json AND a config_dir config.json exist
    with different static_marker_ids, the config_dir version wins."""
    state_root = tmp_path / "state"
    config_root = tmp_path / "config"
    cal_dir = state_root / "cam-c"
    cal_dir.mkdir(parents=True)
    cal_path = cal_dir / "calibration.json"
    cal_path.write_text(json.dumps({"static_marker_ids": ["from-calibration"]}), encoding="utf-8")

    # Sibling config.json (legacy location) — should be ignored when
    # config_dir is provided and has its own config.json.
    save_camera_config(cal_dir, {"static_marker_ids": ["from-sibling"]})
    save_camera_config(config_root / "cam-c", {"static_marker_ids": ["from-config-dir"]})

    pf = _make_playfield_stub()
    ids = pf._load_static_marker_ids(str(cal_path), config_dir=config_root)
    assert ids == ["from-config-dir"]


def test_load_static_marker_ids_config_dir_falls_back_to_calibration_when_absent(
    tmp_path: Path,
) -> None:
    """config_dir given but no config.json under it at all -> falls back to
    the calibration file's own static_marker_ids (not the sibling)."""
    state_root = tmp_path / "state"
    config_root = tmp_path / "config"
    cal_dir = state_root / "cam-d"
    cal_dir.mkdir(parents=True)
    config_root.mkdir(parents=True)
    cal_path = cal_dir / "calibration.json"
    cal_path.write_text(json.dumps({"static_marker_ids": ["from-calibration"]}), encoding="utf-8")

    pf = _make_playfield_stub()
    ids = pf._load_static_marker_ids(str(cal_path), config_dir=config_root)
    assert ids == ["from-calibration"]
