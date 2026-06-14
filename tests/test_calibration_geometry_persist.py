"""Tests for persisting reference geometry into the calibration record.

Sprint 011, ticket 004.  Covers:

- ``to_dict``/``from_dict`` round-trip of the ``playfield`` block
  (real-world cm), ``corner_pixels``, ``static_markers``, and
  ``static_marker_ids``.
- backward compatibility: a record lacking the new fields loads with
  defaults and no error.
- ``calibrate_single`` populating the new fields from data it already
  computes (detection mocked).
- the committed ``global-shutter-camera/calibration.json`` playfield
  block reading back into ``playfield_width_cm`` / ``playfield_height_cm``.

Sprint 012, ticket 004 additions.  Covers:

- ``calibrated_playfield`` / ``calibrated_camera`` provenance fields
  round-trip via ``to_dict`` / ``from_dict``.
- ``load_calibration_from_camera_dir`` mismatch detection setting
  ``calibration_stale`` on the returned object.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.calibration.calibration import (  # noqa: E402
    DEFAULT_STATIC_MARKER_IDS,
    CameraCalibration,
    calibrate_single,
    load_calibration_from_camera_dir,
    save_calibration_to_camera_dir,
)

pytestmark = pytest.mark.needs_cv2


_IDENTITY_H = np.eye(3, dtype=float)

# Repo root → committed example calibration with a playfield block.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_GLOBAL_SHUTTER_DIR = (
    _REPO_ROOT / "data" / "aprilcam" / "cameras" / "global-shutter-camera"
)


def _geometry_cal() -> CameraCalibration:
    """A CameraCalibration carrying full reference geometry."""
    return CameraCalibration(
        device_name="TestCam",
        resolution=(1920, 1080),
        homography=_IDENTITY_H.copy(),
        tags_used=5,
        rms_error=0.01,
        playfield_width_cm=109.0,
        playfield_height_cm=79.5,
        corner_pixels=[[10.0, 20.0], [1900.0, 22.0], [1890.0, 1060.0], [12.0, 1058.0]],
        static_markers={
            "corner:UL": {"pixel": [10.0, 20.0], "world": [0.0, 0.0]},
            "corner:UR": {"pixel": [1900.0, 22.0], "world": [109.0, 0.0]},
            "corner:LR": {"pixel": [1890.0, 1060.0], "world": [109.0, 79.5]},
            "corner:LL": {"pixel": [12.0, 1058.0], "world": [0.0, 79.5]},
            "apriltag:1": {"pixel": [950.0, 540.0], "world": [54.5, 39.7]},
        },
        static_marker_ids=list(DEFAULT_STATIC_MARKER_IDS),
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_to_dict_emits_geometry_fields():
    d = _geometry_cal().to_dict()
    assert d["playfield"] == {"width": 109.0, "height": 79.5}
    assert d["corner_pixels"] == [
        [10.0, 20.0], [1900.0, 22.0], [1890.0, 1060.0], [12.0, 1058.0]
    ]
    assert d["static_markers"]["apriltag:1"] == {
        "pixel": [950.0, 540.0], "world": [54.5, 39.7]
    }
    assert d["static_marker_ids"] == ["aruco_corners", "apriltag:1"]


def test_round_trip_geometry():
    original = _geometry_cal()
    restored = CameraCalibration.from_dict(original.to_dict())

    # playfield W×H round-trip as real-world centimetres.
    assert restored.playfield_width_cm == 109.0
    assert restored.playfield_height_cm == 79.5

    assert restored.corner_pixels == original.corner_pixels
    assert restored.static_markers == original.static_markers
    assert restored.static_marker_ids == original.static_marker_ids


def test_round_trip_through_json():
    """Geometry survives a JSON serialize/parse boundary."""
    original = _geometry_cal()
    parsed = json.loads(json.dumps(original.to_dict()))
    restored = CameraCalibration.from_dict(parsed)
    assert restored.playfield_width_cm == 109.0
    assert restored.playfield_height_cm == 79.5
    assert restored.corner_pixels == original.corner_pixels
    assert restored.static_markers == original.static_markers


def test_playfield_block_is_real_world_cm():
    """The persisted playfield dims are cm, not pixels."""
    cal = _geometry_cal()
    d = cal.to_dict()
    # cm values, clearly not pixel-scale (resolution is 1920x1080).
    assert d["playfield"]["width"] == pytest.approx(109.0)
    assert d["playfield"]["height"] == pytest.approx(79.5)
    assert d["playfield"]["width"] < d["resolution"][0]


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_legacy_record_without_geometry_loads():
    """A record lacking the new fields loads with defaults, no error."""
    legacy = {
        "device_name": "OldCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "tags_used": 4,
        "rms_error": 0.5,
    }
    cal = CameraCalibration.from_dict(legacy)
    assert cal.playfield_width_cm == 0.0
    assert cal.playfield_height_cm == 0.0
    assert cal.corner_pixels is None
    assert cal.static_markers is None
    assert cal.static_marker_ids is None


def test_legacy_field_cm_keys_fall_back_into_playfield():
    """Old top-level field_*_cm keys populate playfield dims on load."""
    legacy = {
        "device_name": "OldCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "field_width_cm": 101.0,
        "field_height_cm": 89.0,
    }
    cal = CameraCalibration.from_dict(legacy)
    assert cal.playfield_width_cm == 101.0
    assert cal.playfield_height_cm == 89.0


def test_committed_global_shutter_playfield_loads():
    """The committed example's playfield block reads back into the dims."""
    if not (_GLOBAL_SHUTTER_DIR / "calibration.json").exists():
        pytest.skip("global-shutter-camera/calibration.json not present")
    cal = load_calibration_from_camera_dir(_GLOBAL_SHUTTER_DIR)
    assert cal is not None
    assert cal.playfield_width_cm == 109.0
    assert cal.playfield_height_cm == 79.5


# ---------------------------------------------------------------------------
# calibrate_single populates geometry (detection mocked)
# ---------------------------------------------------------------------------


class _FakeCap:
    """Minimal VideoCapture stand-in returning a fixed resolution."""

    def __init__(self, w: int = 1920, h: int = 1080):
        self._w, self._h = w, h

    def get(self, prop):  # noqa: ANN001
        import cv2 as cv

        if prop == cv.CAP_PROP_FRAME_WIDTH:
            return self._w
        if prop == cv.CAP_PROP_FRAME_HEIGHT:
            return self._h
        return 0


def test_calibrate_single_populates_geometry(monkeypatch):
    import aprilcam.calibration.homography as homography_mod
    import aprilcam.camera.camutil as camutil_mod

    # Four ArUco corners (negative ids) forming the canonical 4-corner layout,
    # plus AprilTag 1 (static) and AprilTag 2 (dynamic).
    fake_tags = {
        -1: np.array([10.0, 20.0]),     # UL  -> (0, 0)
        -2: np.array([1900.0, 22.0]),   # UR  -> (W, 0)
        -3: np.array([12.0, 1058.0]),   # LL  -> (0, H)
        -4: np.array([1890.0, 1060.0]), # LR  -> (W, H)
        1: np.array([950.0, 540.0]),    # static AprilTag 1
        2: np.array([300.0, 300.0]),    # dynamic AprilTag 2
    }

    monkeypatch.setattr(
        homography_mod, "detect_all_tags", lambda cap, n: dict(fake_tags)
    )
    monkeypatch.setattr(camutil_mod, "get_device_name", lambda idx: "TestCam")

    cal = calibrate_single(
        _FakeCap(),
        field_width_cm=109.0,
        field_height_cm=79.5,
        correct_distortion=False,
        camera_index=0,
    )

    # Playfield dims captured as the real-world cm passed in.
    assert cal.playfield_width_cm == 109.0
    assert cal.playfield_height_cm == 79.5

    # Four corner pixels recorded.
    assert cal.corner_pixels is not None
    assert len(cal.corner_pixels) == 4

    # Static set defaults to aruco_corners + apriltag:1 and is stored.
    assert cal.static_marker_ids == ["aruco_corners", "apriltag:1"]

    # Static markers: four corners + AprilTag 1, but NOT dynamic AprilTag 2.
    assert cal.static_markers is not None
    assert "apriltag:1" in cal.static_markers
    assert "apriltag:2" not in cal.static_markers
    corner_keys = {k for k in cal.static_markers if k.startswith("corner:")}
    assert len(corner_keys) == 4

    # AprilTag 1's recorded pixel is its measured detection pixel.
    assert cal.static_markers["apriltag:1"]["pixel"] == [950.0, 540.0]


def test_calibrate_single_geometry_round_trips(monkeypatch):
    """Geometry captured by calibrate_single survives to_dict/from_dict."""
    import aprilcam.calibration.homography as homography_mod
    import aprilcam.camera.camutil as camutil_mod

    fake_tags = {
        -1: np.array([10.0, 20.0]),
        -2: np.array([1900.0, 22.0]),
        -3: np.array([12.0, 1058.0]),
        -4: np.array([1890.0, 1060.0]),
        1: np.array([950.0, 540.0]),
    }
    monkeypatch.setattr(
        homography_mod, "detect_all_tags", lambda cap, n: dict(fake_tags)
    )
    monkeypatch.setattr(camutil_mod, "get_device_name", lambda idx: "TestCam")

    cal = calibrate_single(
        _FakeCap(), field_width_cm=109.0, field_height_cm=79.5,
        correct_distortion=False,
    )
    restored = CameraCalibration.from_dict(cal.to_dict())
    assert restored.playfield_width_cm == 109.0
    assert restored.playfield_height_cm == 79.5
    assert restored.corner_pixels == cal.corner_pixels
    assert restored.static_markers == cal.static_markers
    assert restored.static_marker_ids == cal.static_marker_ids


# ---------------------------------------------------------------------------
# Sprint 012 — provenance fields and mismatch detection
# ---------------------------------------------------------------------------


def _minimal_cal(
    *,
    playfield_slug: str | None = None,
    camera_slug: str | None = None,
    width_cm: float = 134.3,
    height_cm: float = 89.3,
) -> CameraCalibration:
    """Build a minimal CameraCalibration with optional provenance fields."""
    return CameraCalibration(
        device_name="TestCam",
        resolution=(1920, 1080),
        homography=_IDENTITY_H.copy(),
        playfield_width_cm=width_cm,
        playfield_height_cm=height_cm,
        calibrated_playfield=playfield_slug,
        calibrated_camera=camera_slug,
    )


class _FakePlayfieldDef:
    """Minimal stand-in for PlayfieldDefinition for mismatch tests."""

    def __init__(self, width_cm: float = 134.3, height_cm: float = 89.3) -> None:
        self.width_cm = width_cm
        self.height_cm = height_cm


def test_provenance_fields_round_trip(tmp_path: Path) -> None:
    """calibrated_playfield / calibrated_camera survive to_dict/from_dict."""
    cal = _minimal_cal(playfield_slug="main-playfield", camera_slug="test-cam")

    # to_dict includes them.
    d = cal.to_dict()
    assert d["calibrated_playfield"] == "main-playfield"
    assert d["calibrated_camera"] == "test-cam"

    # from_dict restores them.
    restored = CameraCalibration.from_dict(d)
    assert restored.calibrated_playfield == "main-playfield"
    assert restored.calibrated_camera == "test-cam"

    # Round-trip through save/load.
    save_calibration_to_camera_dir(cal, tmp_path, 134.3, 89.3)
    reloaded = load_calibration_from_camera_dir(tmp_path)
    assert reloaded is not None
    assert reloaded.calibrated_playfield == "main-playfield"
    assert reloaded.calibrated_camera == "test-cam"


def test_provenance_none_when_absent() -> None:
    """Legacy dict without provenance keys → both fields are None."""
    legacy = {
        "device_name": "OldCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
    }
    cal = CameraCalibration.from_dict(legacy)
    assert cal.calibrated_playfield is None
    assert cal.calibrated_camera is None


def test_mismatch_sets_stale(tmp_path: Path) -> None:
    """Mismatched playfield slug sets calibration_stale=True on load."""
    # Save a calibration that references "old-field".
    cal = _minimal_cal(playfield_slug="old-field", camera_slug="test-cam")
    save_calibration_to_camera_dir(cal, tmp_path, 134.3, 89.3)

    # Load with a config pointing to "main-playfield".
    camera_config = {"playfield": "main-playfield"}
    pf_def = _FakePlayfieldDef(width_cm=134.3, height_cm=89.3)
    loaded = load_calibration_from_camera_dir(tmp_path, camera_config, pf_def)

    assert loaded is not None
    assert getattr(loaded, "calibration_stale", False) is True


def test_legacy_record_sets_stale(tmp_path: Path) -> None:
    """A legacy record (no calibrated_playfield) is marked stale when a def is known."""
    # Save a calibration without provenance.
    cal = _minimal_cal(playfield_slug=None)
    save_calibration_to_camera_dir(cal, tmp_path, 134.3, 89.3)

    camera_config = {"playfield": "main-playfield"}
    pf_def = _FakePlayfieldDef(width_cm=134.3, height_cm=89.3)
    loaded = load_calibration_from_camera_dir(tmp_path, camera_config, pf_def)

    assert loaded is not None
    assert getattr(loaded, "calibration_stale", False) is True


def test_matching_provenance_not_stale(tmp_path: Path) -> None:
    """Matching slug + dimensions → calibration_stale is not set."""
    cal = _minimal_cal(
        playfield_slug="main-playfield",
        camera_slug="test-cam",
        width_cm=134.3,
        height_cm=89.3,
    )
    save_calibration_to_camera_dir(cal, tmp_path, 134.3, 89.3)

    camera_config = {"playfield": "main-playfield"}
    pf_def = _FakePlayfieldDef(width_cm=134.3, height_cm=89.3)
    loaded = load_calibration_from_camera_dir(tmp_path, camera_config, pf_def)

    assert loaded is not None
    assert not getattr(loaded, "calibration_stale", False)


def test_dimension_mismatch_sets_stale(tmp_path: Path) -> None:
    """Calibration stored with different dims than current def → stale."""
    # Calibration was done with old field dimensions.
    cal = _minimal_cal(
        playfield_slug="main-playfield",
        camera_slug="test-cam",
        width_cm=101.0,   # old dimension
        height_cm=89.0,
    )
    save_calibration_to_camera_dir(cal, tmp_path, 101.0, 89.0)

    camera_config = {"playfield": "main-playfield"}
    # Def now reports different dimensions.
    pf_def = _FakePlayfieldDef(width_cm=134.3, height_cm=89.3)
    loaded = load_calibration_from_camera_dir(tmp_path, camera_config, pf_def)

    assert loaded is not None
    assert getattr(loaded, "calibration_stale", False) is True


def test_mismatch_detection_ignores_when_no_params(tmp_path: Path) -> None:
    """When camera_config or playfield_def is None, stale is never set."""
    cal = _minimal_cal(playfield_slug=None)  # legacy record
    save_calibration_to_camera_dir(cal, tmp_path, 134.3, 89.3)

    # No optional params: old callers are unaffected.
    loaded = load_calibration_from_camera_dir(tmp_path)
    assert loaded is not None
    assert not getattr(loaded, "calibration_stale", False)

    # Only one param: still no mismatch detection (both required).
    loaded2 = load_calibration_from_camera_dir(
        tmp_path, camera_config={"playfield": "main-playfield"}
    )
    assert not getattr(loaded2, "calibration_stale", False)


# ---------------------------------------------------------------------------
# Config/calibration split — OOP refactor
# ---------------------------------------------------------------------------


def _write_cal_without_static(tmp_path: Path, *, slug: str = "test-cam") -> None:
    """Write a minimal calibration.json with no static fields (post-split format)."""
    data = {
        "camera": slug,
        "homography": _IDENTITY_H.tolist(),
        "tags_used": 5,
        "rms_error": 0.01,
        "playfield": {"width": 109.0, "height": 79.5},
        "detection_fps": 10,
        "calibrated_playfield": "main-playfield",
        "calibrated_camera": slug,
    }
    (tmp_path / "calibration.json").write_text(json.dumps(data))


def test_config_overlay_populates_static_fields(tmp_path: Path) -> None:
    """config.json supplies static fields when calibration.json omits them."""
    _write_cal_without_static(tmp_path)
    config = {
        "playfield": "main-playfield",
        "device_name": "My Camera",
        "resolution": [1280, 800],
        "settings": {"program": "uvc-util", "controls": {"gain": "1"}},
        "camera_position": {"x_offset": 1.0, "y_offset": 2.0, "height": 100.0},
        "static_marker_ids": ["aruco_corners", "apriltag:1"],
    }
    (tmp_path / "config.json").write_text(json.dumps(config))

    cal = load_calibration_from_camera_dir(tmp_path)

    assert cal is not None
    assert cal.device_name == "My Camera"
    assert cal.resolution == (1280, 800)
    assert cal.settings == {"program": "uvc-util", "controls": {"gain": "1"}}
    assert cal.camera_position is not None
    assert cal.camera_position.x_offset == 1.0
    assert cal.camera_position.y_offset == 2.0
    assert cal.camera_position.height == 100.0
    assert cal.static_marker_ids == ["aruco_corners", "apriltag:1"]


def test_saved_calibration_omits_static_fields(tmp_path: Path) -> None:
    """save_calibration_to_camera_dir does NOT write the 5 static fields."""
    cal = CameraCalibration(
        device_name="TestCam",
        resolution=(1280, 800),
        homography=_IDENTITY_H.copy(),
        settings={"program": "uvc-util", "controls": {"gain": "1"}},
        camera_position=None,
        static_marker_ids=["aruco_corners"],
    )
    save_calibration_to_camera_dir(cal, tmp_path, 109.0, 79.5)
    data = json.loads((tmp_path / "calibration.json").read_text())

    for key in ("device_name", "resolution", "settings", "camera_position", "static_marker_ids"):
        assert key not in data, f"calibration.json must not contain '{key}' after split"

    # camera slug is present
    assert data.get("camera") == tmp_path.name


def test_saved_calibration_has_camera_key(tmp_path: Path) -> None:
    """calibration.json written by save_calibration_to_camera_dir has a 'camera' key."""
    cal = _minimal_cal(playfield_slug="main-playfield", camera_slug="test-cam")
    save_calibration_to_camera_dir(cal, tmp_path, 134.3, 89.3)
    data = json.loads((tmp_path / "calibration.json").read_text())
    assert data.get("camera") == tmp_path.name


def test_legacy_calibration_with_static_fields_loads(tmp_path: Path) -> None:
    """Legacy calibration.json with static fields still loads them (fallback)."""
    legacy_data = {
        "device_name": "LegacyCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "settings": {"program": "uvc-util", "controls": {"gain": "2"}},
        "camera_position": {"x_offset": 0.0, "y_offset": 0.0, "height": 50.0},
        "static_marker_ids": ["aruco_corners"],
        "tags_used": 4,
        "rms_error": 0.1,
    }
    (tmp_path / "calibration.json").write_text(json.dumps(legacy_data))
    # No config.json — the loader falls back to calibration.json values.

    cal = load_calibration_from_camera_dir(tmp_path)

    assert cal is not None
    assert cal.device_name == "LegacyCam"
    assert cal.resolution == (640, 480)
    assert cal.settings == {"program": "uvc-util", "controls": {"gain": "2"}}
    assert cal.camera_position is not None
    assert cal.camera_position.height == 50.0
    assert cal.static_marker_ids == ["aruco_corners"]


def test_config_wins_over_legacy_calibration_values(tmp_path: Path) -> None:
    """When both calibration.json and config.json have a key, config.json wins."""
    legacy_data = {
        "device_name": "LegacyCam",
        "resolution": [640, 480],
        "homography": _IDENTITY_H.tolist(),
        "settings": {"program": "uvc-util", "controls": {"gain": "1"}},
    }
    (tmp_path / "calibration.json").write_text(json.dumps(legacy_data))
    config = {
        "device_name": "NewCam",
        "resolution": [1920, 1080],
        "settings": {"program": "uvc-util", "controls": {"gain": "5"}},
    }
    (tmp_path / "config.json").write_text(json.dumps(config))

    cal = load_calibration_from_camera_dir(tmp_path)

    assert cal is not None
    assert cal.device_name == "NewCam"      # config wins
    assert cal.resolution == (1920, 1080)   # config wins
    assert cal.settings["controls"]["gain"] == "5"  # config wins
