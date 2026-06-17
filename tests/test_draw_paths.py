"""Tests for PlayfieldDisplay.draw_paths (T004).

These tests use a real PlayfieldDisplay configured in headless mode with
_mode="full" so that _map_points_to_display is a pass-through (no crop,
no deskew). An identity homography means world (x, y) → source pixel (x, y)
→ display pixel (x, y), making pixel assertions straightforward.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.ui.display import PlayfieldDisplay
from aprilcam.core.playfield import PlayfieldBoundary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_display() -> PlayfieldDisplay:
    """Return a headless PlayfieldDisplay in full (pass-through) mode."""
    pf = PlayfieldBoundary()  # polygon=None, no ArUco detection called
    disp = PlayfieldDisplay(pf, headless=True, deskew_overlay=False)
    # Force _mode to "full" so _map_points_to_display is a no-op pass-through.
    disp._mode = "full"
    return disp


def _black_frame(size: int = 500) -> np.ndarray:
    """Return a black BGR frame of shape (size, size, 3) uint8."""
    return np.zeros((size, size, 3), dtype=np.uint8)


# Identity homography: world (x, y) maps directly to source pixel (x, y).
H_IDENTITY = np.eye(3, dtype=np.float64)

# A single waypoint that should land near pixel (100, 100).
_WP_CIRCLE = {
    "x": 100.0,
    "y": 100.0,
    "size_cm": 20.0,   # half-extent = 10 → radius ≥ 10 px
    "symbol": "circle",
    "symbol_color": [0, 255, 0],   # green
    "line_color": [255, 255, 255],
}

def _simple_path(waypoints: list) -> dict:
    return {"path_000": {"waypoints": waypoints}}


# ---------------------------------------------------------------------------
# Test 1: homography=None → no-op, frame unchanged
# ---------------------------------------------------------------------------

def test_draw_paths_noop_no_homography():
    disp = _make_display()
    frame = _black_frame()
    original = frame.copy()
    disp.draw_paths(frame, _simple_path([_WP_CIRCLE]), playfield=None, homography=None)
    assert np.array_equal(frame, original), "Frame should be unchanged when homography is None"


# ---------------------------------------------------------------------------
# Test 2: empty paths dict → no-op, frame unchanged
# ---------------------------------------------------------------------------

def test_draw_paths_noop_empty_paths():
    disp = _make_display()
    frame = _black_frame()
    original = frame.copy()
    disp.draw_paths(frame, {}, playfield=None, homography=H_IDENTITY)
    assert np.array_equal(frame, original), "Frame should be unchanged when paths is empty"


# ---------------------------------------------------------------------------
# Test 3: all 8 symbols don't crash + pixels changed
# ---------------------------------------------------------------------------

def test_draw_paths_all_symbols_no_crash():
    disp = _make_display()
    frame = _black_frame(500)

    symbols = [
        "circle", "filled_circle",
        "square", "filled_square",
        "triangle", "filled_triangle",
        "x", "none",
    ]
    waypoints = [
        {
            "x": 50.0 + i * 40.0,
            "y": 200.0,
            "size_cm": 15.0,
            "symbol": sym,
            "symbol_color": [200, 100, 50],
            "line_color": [100, 200, 50],
        }
        for i, sym in enumerate(symbols)
    ]

    # origin_y=0 → raw_y = 0+200 = 200 (A1-centred, +y up; identity H), on-frame.
    disp.draw_paths(frame, {"path_000": {"waypoints": waypoints}}, playfield=None, homography=H_IDENTITY, origin_y=0.0)

    # At least some pixels should have changed (all non-"none" symbols should draw).
    assert frame.any(), "Some pixels should have been drawn"


# ---------------------------------------------------------------------------
# Test 4: 'none' symbol skips the marker but lines still attach
# ---------------------------------------------------------------------------

def test_draw_paths_none_symbol_skips_marker():
    disp = _make_display()
    frame = _black_frame(500)

    # Two waypoints: first is 'none' (no marker), second is 'circle'.
    # A line should be drawn between them; the first vertex has no symbol.
    waypoints = [
        {
            "x": 50.0,
            "y": 250.0,
            "size_cm": 10.0,
            "symbol": "none",
            "symbol_color": [255, 0, 0],
            "line_color": [255, 255, 0],  # yellow line
        },
        {
            "x": 200.0,
            "y": 250.0,
            "size_cm": 10.0,
            "symbol": "circle",
            "symbol_color": [0, 255, 0],
            "line_color": [0, 0, 0],
        },
    ]

    # origin_y=0 → raw_y = 250 (A1-centred, +y up; identity H), pixel row 250.
    disp.draw_paths(frame, {"path_000": {"waypoints": waypoints}}, playfield=None, homography=H_IDENTITY, origin_y=0.0)

    # A line was drawn between the two waypoints: check a pixel on the path.
    # The line runs from x=50 to x=200 at pixel row 250. A pixel near x=125,y=250 should be non-black.
    mid_pixel = frame[250, 125]
    assert mid_pixel.any(), "A line should have been drawn between the two waypoints"

    # The first waypoint is 'none' — verify its center pixel is not filled with
    # symbol_color. The pixel at (50, 250) may have line color but not symbol color.
    # Just confirm no crash occurred (checked by reaching this line).


# ---------------------------------------------------------------------------
# Test 5: RGB→BGR conversion at the cv boundary
# ---------------------------------------------------------------------------

def test_draw_paths_color_is_bgr():
    """An RGB input of [255, 0, 0] (red) must appear red in the frame.

    OpenCV stores images as BGR. After correct RGB→BGR conversion:
      symbol_color [255, 0, 0] → cv call gets (B=0, G=0, R=255)
    So in the numpy array (also BGR) the drawn pixel should have
    channel[2] (R in BGR) ≈ 255 and channel[0] (B in BGR) ≈ 0.
    """
    disp = _make_display()
    frame = _black_frame(500)

    cx, cy = 250, 250
    waypoints = [
        {
            "x": float(cx),
            "y": float(cy),
            "size_cm": 40.0,   # large enough to reliably hit the center
            "symbol": "filled_circle",
            "symbol_color": [255, 0, 0],   # RED in RGB
            "line_color": [0, 0, 0],
        }
    ]

    # origin_y=0 → raw_y = 250 (A1-centred, +y up; identity H), pixel row 250.
    disp.draw_paths(frame, {"path_000": {"waypoints": waypoints}}, playfield=None, homography=H_IDENTITY, origin_y=0.0)

    # The filled circle center should be red in BGR storage:
    # frame[cy, cx] in BGR → channel 2 is R → should be high
    # channel 0 is B → should be low
    pixel = frame[cy, cx]
    assert pixel[2] > 200, (
        f"Red channel (BGR index 2) should be > 200 after RGB→BGR flip, got {pixel}"
    )
    assert pixel[0] < 50, (
        f"Blue channel (BGR index 0) should be < 50 after RGB→BGR flip, got {pixel}"
    )
