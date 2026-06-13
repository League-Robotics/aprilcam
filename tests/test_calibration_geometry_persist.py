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
