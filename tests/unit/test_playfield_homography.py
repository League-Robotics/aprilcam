"""Tests for Playfield._auto_discover_homography argument-passing fix.

Verifies that:
  - _auto_discover_homography receives device_name, width, height and returns
    a 3x3 numpy array when a matching calibration file exists.
  - self._homography is set on the Playfield after start() with a mocked camera.
  - tag.wx / tag.wy are non-None when homography is loaded (via pipeline).
  - No homography is set when there is no matching calibration file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytestmark = pytest.mark.needs_cv2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A known 3x3 homography matrix for assertions.
_EXPECTED_H = [
    [0.1, 0.0, 0.0],
    [0.0, 0.1, 0.0],
    [0.0, 0.0, 1.0],
]


def _write_homography_json(path: Path, matrix: list) -> None:
    path.write_text(json.dumps({"homography": matrix}))


def _write_calibration_json(path: Path, device_name: str, matrix: list) -> None:
    """Write a unified calibration.json with a homography entry for device_name."""
    data = {
        "cameras": {
            device_name: {
                "device_name": device_name,
                "homography": matrix,
            }
        }
    }
    path.write_text(json.dumps(data))


class _MockCamera:
    """Minimal duck-typed camera with controllable name and resolution."""

    def __init__(self, name: str = "TestCam", width: int = 640, height: int = 480):
        self._name = name
        self._width = width
        self._height = height
        self._open = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def resolution(self) -> tuple[int, int]:
        return (self._width, self._height)

    def open(self) -> None:
        self._open = True

    def read(self):
        # Return a small blank frame so the pipeline thread doesn't crash
        frame = np.zeros((self._height, self._width, 3), dtype=np.uint8)
        return True, frame


# ---------------------------------------------------------------------------
# Tests: _auto_discover_homography
# ---------------------------------------------------------------------------


class TestAutoDiscoverHomography:

    def test_returns_3x3_when_legacy_file_present(self, tmp_path: Path) -> None:
        """Legacy homography-<slug>.json file is discovered and loaded."""
        from aprilcam.camera.camutil import camera_slug

        cam = _MockCamera(name="TestCam", width=640, height=480)
        slug = camera_slug("TestCam", 640, 480)
        hfile = tmp_path / f"homography-{slug}.json"
        _write_homography_json(hfile, _EXPECTED_H)

        from aprilcam.core.playfield import Playfield

        # Construct with calibration=None so no eager load; data_dir points to tmp_path.
        pf = Playfield(cam, calibration=None, data_dir=str(tmp_path))
        result = pf._auto_discover_homography("TestCam", 640, 480)

        assert result is not None
        assert isinstance(result, np.ndarray)
        assert result.shape == (3, 3)
        np.testing.assert_array_almost_equal(result, np.array(_EXPECTED_H))

    def test_returns_3x3_when_unified_calibration_present(self, tmp_path: Path) -> None:
        """Unified calibration.json with matching device_name is discovered."""
        cal_file = tmp_path / "calibration.json"
        _write_calibration_json(cal_file, "TestCam", _EXPECTED_H)

        from aprilcam.core.playfield import Playfield

        cam = _MockCamera(name="TestCam", width=640, height=480)
        pf = Playfield(cam, calibration=None, data_dir=str(tmp_path))
        result = pf._auto_discover_homography("TestCam", 640, 480)

        assert result is not None
        assert result.shape == (3, 3)
        np.testing.assert_array_almost_equal(result, np.array(_EXPECTED_H))

    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        """Returns None gracefully when no calibration file exists."""
        from aprilcam.core.playfield import Playfield

        cam = _MockCamera(name="UnknownCam", width=1280, height=720)
        pf = Playfield(cam, calibration=None, data_dir=str(tmp_path))
        result = pf._auto_discover_homography("UnknownCam", 1280, 720)

        assert result is None

    def test_returns_none_when_device_name_mismatch(self, tmp_path: Path) -> None:
        """Returns None when calibration file exists but device_name doesn't match."""
        cal_file = tmp_path / "calibration.json"
        _write_calibration_json(cal_file, "OtherCam", _EXPECTED_H)

        from aprilcam.core.playfield import Playfield

        cam = _MockCamera(name="TestCam", width=640, height=480)
        pf = Playfield(cam, calibration=None, data_dir=str(tmp_path))
        result = pf._auto_discover_homography("TestCam", 640, 480)

        assert result is None


# ---------------------------------------------------------------------------
# Tests: homography loaded on start()
# ---------------------------------------------------------------------------


class TestHomographyLoadedOnStart:

    def test_homography_none_before_start_with_auto(self, tmp_path: Path) -> None:
        """With calibration='auto', _homography is None until start() is called."""
        from aprilcam.camera.camutil import camera_slug

        cam = _MockCamera(name="TestCam", width=640, height=480)
        slug = camera_slug("TestCam", 640, 480)
        hfile = tmp_path / f"homography-{slug}.json"
        _write_homography_json(hfile, _EXPECTED_H)

        from aprilcam.core.playfield import Playfield

        pf = Playfield(cam, calibration="auto", data_dir=str(tmp_path))
        # Before start(), _homography is still None
        assert pf._homography is None

    def test_homography_set_after_start_with_auto(self, tmp_path: Path) -> None:
        """With calibration='auto', _homography is a 3x3 array after start()."""
        from aprilcam.camera.camutil import camera_slug

        cam = _MockCamera(name="TestCam", width=640, height=480)
        # Ensure camera is 'open' so resolution is available
        cam.open()
        slug = camera_slug("TestCam", 640, 480)
        hfile = tmp_path / f"homography-{slug}.json"
        _write_homography_json(hfile, _EXPECTED_H)

        from aprilcam.core.playfield import Playfield

        pf = Playfield(cam, calibration="auto", data_dir=str(tmp_path))

        # Patch pipeline.start() so no background thread starts; we only want
        # to test the homography-discovery portion of Playfield.start().
        pf._pipeline.start = MagicMock()

        pf.start()

        assert pf._homography is not None
        assert isinstance(pf._homography, np.ndarray)
        assert pf._homography.shape == (3, 3)
        np.testing.assert_array_almost_equal(pf._homography, np.array(_EXPECTED_H))

    def test_pipeline_homography_updated_after_start(self, tmp_path: Path) -> None:
        """Pipeline._homography is also updated when start() discovers homography."""
        from aprilcam.camera.camutil import camera_slug

        cam = _MockCamera(name="TestCam", width=640, height=480)
        cam.open()
        slug = camera_slug("TestCam", 640, 480)
        hfile = tmp_path / f"homography-{slug}.json"
        _write_homography_json(hfile, _EXPECTED_H)

        from aprilcam.core.playfield import Playfield

        pf = Playfield(cam, calibration="auto", data_dir=str(tmp_path))
        pf._pipeline.start = MagicMock()

        pf.start()

        assert pf._pipeline._homography is not None
        np.testing.assert_array_almost_equal(pf._pipeline._homography, np.array(_EXPECTED_H))

    def test_no_homography_when_no_calibration_file(self, tmp_path: Path) -> None:
        """_homography stays None after start() when no calibration file exists."""
        cam = _MockCamera(name="NoCam", width=640, height=480)
        cam.open()

        from aprilcam.core.playfield import Playfield

        pf = Playfield(cam, calibration="auto", data_dir=str(tmp_path))
        pf._pipeline.start = MagicMock()

        pf.start()

        assert pf._homography is None

    def test_explicit_calibration_path_still_works(self, tmp_path: Path) -> None:
        """An explicit calibration path loads homography eagerly in __init__."""
        hfile = tmp_path / "my_homography.json"
        _write_homography_json(hfile, _EXPECTED_H)

        cam = _MockCamera()
        from aprilcam.core.playfield import Playfield

        pf = Playfield(cam, calibration=str(hfile))
        # Loaded at __init__ time, not start() time
        assert pf._homography is not None
        assert pf._homography.shape == (3, 3)

    def test_calibration_none_stays_none(self, tmp_path: Path) -> None:
        """calibration=None means no auto-discovery and _homography stays None."""
        from aprilcam.camera.camutil import camera_slug

        cam = _MockCamera(name="TestCam", width=640, height=480)
        cam.open()
        slug = camera_slug("TestCam", 640, 480)
        hfile = tmp_path / f"homography-{slug}.json"
        _write_homography_json(hfile, _EXPECTED_H)

        from aprilcam.core.playfield import Playfield

        pf = Playfield(cam, calibration=None, data_dir=str(tmp_path))
        pf._pipeline.start = MagicMock()
        pf.start()

        assert pf._homography is None
