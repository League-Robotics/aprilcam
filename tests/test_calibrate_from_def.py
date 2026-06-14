"""Tests for calibrate_from_playfield_def — sprint 012, ticket 004.

All tests run WITHOUT a live camera; ``detect_all_tags`` is monkeypatched
to return controlled tag dictionaries.

Covers:
- ``PlayfieldConfigError`` raised when ``config.json`` is absent.
- ``PlayfieldConfigError`` raised when the playfield slug is not in the
  registry.
- Error messages include the list of available playfield names.
- Successful calibration produces correct homography and provenance fields.
- Negative-tid ArUco mapping: def IDs 1/3/5/7 → tids -2/-4/-6/-8.
- ``RuntimeError`` when fewer than 4 corner markers are found.
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
    PlayfieldConfigError,
    calibrate_from_playfield_def,
    load_calibration_from_camera_dir,
)

pytestmark = pytest.mark.needs_cv2

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_IDENTITY_H = np.eye(3, dtype=float)


class _FakePlayfieldDef:
    """Minimal PlayfieldDefinition stand-in with realistic geometry.

    Mirrors main-playfield.json:
      NW id=1  (-67,  44.65)   tid=-2
      NE id=3  ( 67,  44.65)   tid=-4
      SE id=5  ( 67, -44.65)   tid=-6
      SW id=7  (-67, -44.65)   tid=-8
    """

    width_cm: float = 134.3
    height_cm: float = 89.3

    def corner_aruco_ids(self) -> list[int]:
        return [1, 3, 5, 7]

    def corner_world_coords(self) -> list[tuple[float, float]]:
        return [(-67.0, 44.65), (67.0, 44.65), (67.0, -44.65), (-67.0, -44.65)]


class _FakeRegistry:
    """Minimal PlayfieldDefinitionRegistry stand-in."""

    def __init__(self, playfields: dict | None = None) -> None:
        self._defs = playfields or {}

    def get(self, name: str) -> object:
        return self._defs[name]  # KeyError when absent — same as real registry

    def list(self) -> list[str]:
        return sorted(self._defs.keys())


class _FakeCap:
    """Minimal VideoCapture stand-in returning a fixed resolution.

    ``read()`` returns (False, None) by default — we don't need actual frames
    because ``detect_all_tags`` is monkeypatched.
    """

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
    """Return a tags dict with the four def corner tids at known pixel positions.

    Pixel layout (arbitrary but distinct):
      tid -2  (NW, ArUco 1) → (100, 100)
      tid -4  (NE, ArUco 3) → (1820, 100)
      tid -6  (SE, ArUco 5) → (1820, 980)
      tid -8  (SW, ArUco 7) → (100, 980)
    """
    return {
        -2: np.array([100.0, 100.0]),
        -4: np.array([1820.0, 100.0]),
        -6: np.array([1820.0, 980.0]),
        -8: np.array([100.0, 980.0]),
    }


# ---------------------------------------------------------------------------
# Error-path tests: PlayfieldConfigError
# ---------------------------------------------------------------------------


def test_precondition_no_config(tmp_path: Path) -> None:
    """No config.json in camera_dir → PlayfieldConfigError with guidance."""
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    cap = _FakeCap()

    with pytest.raises(PlayfieldConfigError) as exc_info:
        calibrate_from_playfield_def(
            cap,
            camera_dir=tmp_path,
            camera_slug="test-cam",
            playfield_def_registry=registry,
        )

    msg = str(exc_info.value)
    assert "test-cam" in msg
    assert "config.json" in msg
    assert "Available playfields:" in msg
    assert "main-playfield" in msg


def test_precondition_no_config_guidance_message_exact_format(tmp_path: Path) -> None:
    """Guidance message matches the spec format including the camera slug."""
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    with pytest.raises(PlayfieldConfigError) as exc_info:
        calibrate_from_playfield_def(
            _FakeCap(),
            camera_dir=tmp_path,
            camera_slug="arducam-ov9782-usb-camera",
            playfield_def_registry=registry,
        )
    msg = str(exc_info.value)
    assert "arducam-ov9782-usb-camera" in msg
    assert "Available playfields: [main-playfield]" in msg


def test_precondition_missing_def(tmp_path: Path) -> None:
    """config.json present but named playfield not in registry → PlayfieldConfigError."""
    # Write a config.json referencing a non-existent playfield.
    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "no-such-field"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})

    with pytest.raises(PlayfieldConfigError) as exc_info:
        calibrate_from_playfield_def(
            _FakeCap(),
            camera_dir=tmp_path,
            camera_slug="test-cam",
            playfield_def_registry=registry,
        )

    msg = str(exc_info.value)
    assert "no-such-field" in msg
    assert "Available playfields:" in msg
    assert "main-playfield" in msg


def test_precondition_error_lists_all_available(tmp_path: Path) -> None:
    """Available playfield list includes all registry entries."""
    registry = _FakeRegistry({
        "alpha-field": _FakePlayfieldDef(),
        "beta-field": _FakePlayfieldDef(),
    })
    with pytest.raises(PlayfieldConfigError) as exc_info:
        calibrate_from_playfield_def(
            _FakeCap(),
            camera_dir=tmp_path,
            camera_slug="test-cam",
            playfield_def_registry=registry,
        )
    msg = str(exc_info.value)
    assert "alpha-field" in msg
    assert "beta-field" in msg


def test_precondition_empty_registry_message(tmp_path: Path) -> None:
    """When registry is empty, Available playfields message shows (none) or empty."""
    registry = _FakeRegistry({})
    with pytest.raises(PlayfieldConfigError) as exc_info:
        calibrate_from_playfield_def(
            _FakeCap(),
            camera_dir=tmp_path,
            camera_slug="test-cam",
            playfield_def_registry=registry,
        )
    msg = str(exc_info.value)
    assert "Available playfields:" in msg


# ---------------------------------------------------------------------------
# Error-path test: fewer than 4 corners detected
# ---------------------------------------------------------------------------


def test_runtime_error_when_fewer_than_4_corners(tmp_path: Path, monkeypatch) -> None:
    """RuntimeError raised when fewer than 4 corner ArUco markers are detected."""
    import aprilcam.calibration.homography as hm

    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})

    # Only 2 of the 4 expected tids present.
    partial_tags = {
        -2: np.array([100.0, 100.0]),
        -4: np.array([1820.0, 100.0]),
        1: np.array([960.0, 540.0]),  # AprilTag, not a corner
    }
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(partial_tags))

    with pytest.raises(RuntimeError) as exc_info:
        calibrate_from_playfield_def(
            _FakeCap(),
            camera_dir=tmp_path,
            camera_slug="test-cam",
            playfield_def_registry=registry,
        )

    msg = str(exc_info.value)
    assert "2 of 4" in msg
    # Message should name expected IDs
    assert "ArUco" in msg


# ---------------------------------------------------------------------------
# Successful calibration: corner IDs and homography
# ---------------------------------------------------------------------------


def test_calibrate_from_def_uses_corner_ids(tmp_path: Path, monkeypatch) -> None:
    """Mock detect_all_tags with IDs 1/3/5/7 → correct homography and provenance."""
    import aprilcam.calibration.homography as hm

    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})

    fake_tags = _make_corner_tags()
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(fake_tags))

    cal = calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=tmp_path,
        camera_slug="test-cam",
        playfield_def_registry=registry,
        correct_distortion=False,
    )

    assert isinstance(cal, CameraCalibration)
    # Provenance fields set.
    assert cal.calibrated_playfield == "main-playfield"
    assert cal.calibrated_camera == "test-cam"
    # Dimensions from def.
    assert cal.playfield_width_cm == pytest.approx(134.3)
    assert cal.playfield_height_cm == pytest.approx(89.3)
    # Calibration file written.
    assert (tmp_path / "calibration.json").exists()


def test_calibrate_from_def_homography_maps_corners(tmp_path: Path, monkeypatch) -> None:
    """The computed homography maps pixel corners to the def's world coords."""
    import aprilcam.calibration.homography as hm

    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    fake_tags = _make_corner_tags()
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(fake_tags))

    cal = calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=tmp_path,
        camera_slug="test-cam",
        playfield_def_registry=registry,
        correct_distortion=False,
    )

    H = cal.homography
    # Check that each corner pixel maps to its expected world coord (within 0.5 cm).
    corner_checks = [
        ((100.0, 100.0),   (-67.0,  44.65)),  # NW
        ((1820.0, 100.0),  ( 67.0,  44.65)),  # NE
        ((1820.0, 980.0),  ( 67.0, -44.65)),  # SE
        ((100.0, 980.0),   (-67.0, -44.65)),  # SW
    ]
    for (pu, pv), (wx, wy) in corner_checks:
        vec = H @ np.array([pu, pv, 1.0])
        got_x, got_y = float(vec[0] / vec[2]), float(vec[1] / vec[2])
        assert got_x == pytest.approx(wx, abs=0.5), f"X mismatch for pixel ({pu},{pv})"
        assert got_y == pytest.approx(wy, abs=0.5), f"Y mismatch for pixel ({pu},{pv})"


def test_calibrate_from_def_negative_tid_mapping(tmp_path: Path, monkeypatch) -> None:
    """Verify the tid mapping: ArUco IDs 1/3/5/7 → tids -2/-4/-6/-8.

    If the mapping were wrong (e.g. -1/-2/-3/-4), the function would
    report missing corners.
    """
    import aprilcam.calibration.homography as hm

    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})

    # Tags use the correct negative-tid encoding (-(id+1)).
    correct_tids = {
        -2: np.array([100.0, 100.0]),   # ArUco 1 (NW)
        -4: np.array([1820.0, 100.0]),  # ArUco 3 (NE)
        -6: np.array([1820.0, 980.0]),  # ArUco 5 (SE)
        -8: np.array([100.0, 980.0]),   # ArUco 7 (SW)
    }
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(correct_tids))

    # Should succeed with the correct tid mapping.
    cal = calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=tmp_path,
        camera_slug="test-cam",
        playfield_def_registry=registry,
        correct_distortion=False,
    )
    assert cal is not None

    # Wrong tids (e.g. the old 0-3 canonical layout) should raise RuntimeError.
    wrong_tids = {
        -1: np.array([100.0, 100.0]),   # ArUco 0 (wrong — not in def)
        -2: np.array([1820.0, 100.0]),  # ArUco 1 (matches, but only 1 of 4)
        -3: np.array([1820.0, 980.0]),  # ArUco 2 (wrong)
        -4: np.array([100.0, 980.0]),   # ArUco 3 (matches, but only 2 of 4)
    }
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(wrong_tids))
    with pytest.raises(RuntimeError):
        calibrate_from_playfield_def(
            _FakeCap(),
            camera_dir=tmp_path,
            camera_slug="test-cam",
            playfield_def_registry=registry,
            correct_distortion=False,
        )


def test_calibrate_from_def_persists_calibration(tmp_path: Path, monkeypatch) -> None:
    """calibrate_from_playfield_def writes calibration.json with provenance."""
    import aprilcam.calibration.homography as hm

    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(_make_corner_tags()))

    calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=tmp_path,
        camera_slug="test-cam",
        playfield_def_registry=registry,
        correct_distortion=False,
    )

    cal_file = tmp_path / "calibration.json"
    assert cal_file.exists()
    data = json.loads(cal_file.read_text())
    assert data["calibrated_playfield"] == "main-playfield"
    assert data["calibrated_camera"] == "test-cam"
    assert data["playfield"]["width"] == pytest.approx(134.3)
    assert data["playfield"]["height"] == pytest.approx(89.3)


def test_calibrate_from_def_with_apriltag_augmentation(
    tmp_path: Path, monkeypatch
) -> None:
    """AprilTags in the detection augment the homography point set."""
    import aprilcam.calibration.homography as hm

    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})

    # Add AprilTag 1 at a known pixel; its world position will be derived
    # from the initial homography.
    tags_with_april = dict(_make_corner_tags())
    tags_with_april[1] = np.array([960.0, 540.0])  # center-ish
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(tags_with_april))

    cal = calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=tmp_path,
        camera_slug="test-cam",
        playfield_def_registry=registry,
        correct_distortion=False,
    )
    # tags_used should include the 4 corners + the 1 AprilTag.
    assert cal.tags_used == 5
    # static_markers should include apriltag:1.
    assert cal.static_markers is not None
    assert "apriltag:1" in cal.static_markers


def test_calibrate_from_def_round_trips_through_load(
    tmp_path: Path, monkeypatch
) -> None:
    """Calibration saved by calibrate_from_playfield_def loads back correctly."""
    import aprilcam.calibration.homography as hm

    (tmp_path / "config.json").write_text(
        json.dumps({"playfield": "main-playfield"}), encoding="utf-8"
    )
    registry = _FakeRegistry({"main-playfield": _FakePlayfieldDef()})
    monkeypatch.setattr(hm, "detect_all_tags", lambda cap, n: dict(_make_corner_tags()))

    original = calibrate_from_playfield_def(
        _FakeCap(),
        camera_dir=tmp_path,
        camera_slug="test-cam",
        playfield_def_registry=registry,
        correct_distortion=False,
    )

    # Load with matching config + def → not stale.
    camera_config = {"playfield": "main-playfield"}
    pf_def = _FakePlayfieldDef()
    loaded = load_calibration_from_camera_dir(tmp_path, camera_config, pf_def)
    assert loaded is not None
    assert loaded.calibrated_playfield == "main-playfield"
    assert not getattr(loaded, "calibration_stale", False)
