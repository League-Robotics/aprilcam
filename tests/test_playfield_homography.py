"""Regression tests for the homography fix from Sprint 004 ticket 001.

Verifies two key behaviours:
  1. With a calibration file present, Playfield._homography is a 3x3 ndarray
     after start().
  2. With no calibration file, Playfield._homography is None after start().

These tests exercise the fix that ensures calibration='auto' does NOT call
discover_homography() with no args at construction time; instead,
_auto_discover_homography(device_name, width, height) is called at start()
and correctly forwards all three arguments.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.needs_cv2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_H = [
    [0.1, 0.0, 0.0],
    [0.0, 0.1, 0.0],
    [0.0, 0.0, 1.0],
]


class _MockCamera:
    """Minimal duck-typed camera with controllable name and resolution."""

    def __init__(self, name: str = "TestCam", width: int = 640, height: int = 480):
        self._name = name
        self._width = width
        self._height = height
        self._open = True  # start open so resolution is available

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
        frame = np.zeros((self._height, self._width, 3), dtype=np.uint8)
        return True, frame


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_auto_discover_homography_loads_calibration(tmp_path: Path) -> None:
    """With a mock calibration file present, _homography is a 3x3 ndarray after start()."""
    from aprilcam.camera.camutil import camera_slug
    from aprilcam.core.playfield import Playfield

    cam = _MockCamera(name="TestCam", width=640, height=480)
    slug = camera_slug("TestCam", 640, 480)
    hfile = tmp_path / f"homography-{slug}.json"
    hfile.write_text(json.dumps({"homography": _EXPECTED_H}))

    pf = Playfield(cam, calibration="auto", data_dir=str(tmp_path))

    # Before start(), _homography is None — no eager discovery at construction
    assert pf._homography is None

    # Patch pipeline.start() to prevent a real background thread
    pf._pipeline.start = MagicMock()
    pf.start()

    assert pf._homography is not None
    assert isinstance(pf._homography, np.ndarray)
    assert pf._homography.shape == (3, 3)
    np.testing.assert_array_almost_equal(pf._homography, np.array(_EXPECTED_H))


def test_auto_discover_homography_returns_none_when_no_file(tmp_path: Path) -> None:
    """With no calibration file, _homography is None after start()."""
    from aprilcam.core.playfield import Playfield

    cam = _MockCamera(name="NoCam", width=640, height=480)

    pf = Playfield(cam, calibration="auto", data_dir=str(tmp_path))
    pf._pipeline.start = MagicMock()
    pf.start()

    assert pf._homography is None
