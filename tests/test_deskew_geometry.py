"""Tests for homography-derived metric deskew and Playfield seeding.

Sprint 011, ticket 005.  Covers:

- ``corner_pixels_from_homography`` round-trips a known homography.
- ``metric_deskew_matrix`` maps the source polygon to the metric top-down
  rectangle corners, with deterministic output dims = ``(round(W·s),
  round(H·s))`` independent of source edge lengths.
- ``px_per_cm`` default + override scaling.
- ``PlayfieldBoundary`` seeds a non-``None`` polygon from saved geometry
  (homography + W×H) with no live frame, and the three previously-duplicated
  warp sites share the single helper.
- the committed ``global-shutter-camera/calibration.json`` regression.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

import cv2 as cv  # noqa: E402

from aprilcam.calibration.geometry import (  # noqa: E402
    DEFAULT_PX_PER_CM,
    corner_pixels_from_homography,
    metric_deskew_matrix,
)
from aprilcam.core.playfield import PlayfieldBoundary  # noqa: E402

pytestmark = pytest.mark.needs_cv2

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GLOBAL_SHUTTER = (
    _REPO_ROOT
    / "data"
    / "aprilcam"
    / "cameras"
    / "global-shutter-camera"
    / "calibration.json"
)


def _homography_from_corners(poly: np.ndarray, width_cm: float, height_cm: float) -> np.ndarray:
    """Build a pixel->world homography that maps *poly* (UL/UR/LR/LL) to the
    world corners (0,0),(W,0),(W,H),(0,H)."""
    src = np.asarray(poly, dtype=np.float32).reshape(4, 2)
    dst = np.array(
        [[0, 0], [width_cm, 0], [width_cm, height_cm], [0, height_cm]],
        dtype=np.float32,
    )
    return cv.getPerspectiveTransform(src, dst).astype(np.float64)


# A non-axis-aligned (perspective-skewed) source polygon, UL/UR/LR/LL.
_POLY = np.array(
    [[120.0, 80.0], [840.0, 110.0], [900.0, 620.0], [60.0, 560.0]],
    dtype=np.float32,
)
_W, _H = 109.0, 79.5


# ---------------------------------------------------------------------------
# corner_pixels_from_homography
# ---------------------------------------------------------------------------


def test_corner_pixels_round_trip():
    H = _homography_from_corners(_POLY, _W, _H)
    recovered = corner_pixels_from_homography(H, _W, _H)
    assert recovered.shape == (4, 2)
    assert recovered.dtype == np.float32
    np.testing.assert_allclose(recovered, _POLY, atol=1e-3)


def test_corner_pixels_order_matches_world_corners():
    # Identity-like: world corners map straight to pixel corners when H maps
    # a unit-scaled axis-aligned rectangle.
    poly = np.array([[0, 0], [100, 0], [100, 50], [0, 50]], dtype=np.float32)
    H = _homography_from_corners(poly, 100.0, 50.0)
    recovered = corner_pixels_from_homography(H, 100.0, 50.0)
    # UL=(0,0), UR=(100,0), LR=(100,50), LL=(0,50)
    np.testing.assert_allclose(recovered, poly, atol=1e-3)


# ---------------------------------------------------------------------------
# metric_deskew_matrix
# ---------------------------------------------------------------------------


def _warp_corners(M: np.ndarray, poly: np.ndarray) -> np.ndarray:
    pts = poly.reshape(-1, 1, 2).astype(np.float32)
    return cv.perspectiveTransform(pts, M).reshape(-1, 2)


def test_metric_deskew_maps_to_rectangle_corners():
    s = DEFAULT_PX_PER_CM
    M, (out_w, out_h) = metric_deskew_matrix(_POLY, _W, _H, s)
    warped = _warp_corners(M, _POLY)
    expected = np.array(
        [[0, 0], [_W * s, 0], [_W * s, _H * s], [0, _H * s]],
        dtype=np.float32,
    )
    np.testing.assert_allclose(warped, expected, atol=1e-2)


def test_metric_deskew_output_dims_deterministic():
    # Output dims are a function of W×H and px_per_cm — NOT polygon edge length.
    s = DEFAULT_PX_PER_CM
    _, size_a = metric_deskew_matrix(_POLY, _W, _H, s)
    # A much larger source polygon with the same W×H must yield the same dims.
    big = _POLY * 3.0
    _, size_b = metric_deskew_matrix(big, _W, _H, s)
    assert size_a == size_b == (round(_W * s), round(_H * s))


def test_px_per_cm_default():
    M_default, size_default = metric_deskew_matrix(_POLY, _W, _H)
    M_explicit, size_explicit = metric_deskew_matrix(_POLY, _W, _H, DEFAULT_PX_PER_CM)
    assert size_default == size_explicit
    np.testing.assert_allclose(M_default, M_explicit, atol=1e-9)


def test_px_per_cm_scaling_doubles_dims():
    _, (w1, h1) = metric_deskew_matrix(_POLY, _W, _H, 10.0)
    _, (w2, h2) = metric_deskew_matrix(_POLY, _W, _H, 20.0)
    assert (w2, h2) == (round(_W * 20.0), round(_H * 20.0))
    assert w2 == round(w1 * 2) or w2 == w1 * 2
    assert h2 == round(h1 * 2) or h2 == h1 * 2


def test_metric_deskew_degenerate_raises():
    with pytest.raises(ValueError):
        metric_deskew_matrix(_POLY, 0.0, _H, DEFAULT_PX_PER_CM)


# ---------------------------------------------------------------------------
# PlayfieldBoundary seeding from saved geometry
# ---------------------------------------------------------------------------


def test_boundary_seeds_polygon_from_homography_no_live_frame():
    H = _homography_from_corners(_POLY, _W, _H)
    b = PlayfieldBoundary(homography=H, width_cm=_W, height_cm=_H)
    poly = b.get_polygon()
    assert poly is not None
    # No update() call has been made; seeding came purely from saved geometry.
    np.testing.assert_allclose(poly, _POLY, atol=1e-2)


def test_boundary_seeds_from_explicit_polygon_arg():
    b = PlayfieldBoundary(polygon=_POLY)
    poly = b.get_polygon()
    assert poly is not None
    np.testing.assert_allclose(poly, _POLY, atol=1e-3)


def test_boundary_deskew_transform_metric_when_dims_known():
    H = _homography_from_corners(_POLY, _W, _H)
    b = PlayfieldBoundary(homography=H, width_cm=_W, height_cm=_H)
    result = b.deskew_transform()
    assert result is not None
    M, (out_w, out_h) = result
    s = DEFAULT_PX_PER_CM
    assert (out_w, out_h) == (round(_W * s), round(_H * s))
    # get_deskew_matrix must return the same matrix the helper produced.
    np.testing.assert_allclose(b.get_deskew_matrix(), M, atol=1e-9)


def test_boundary_deskew_transform_respects_px_per_cm_override():
    H = _homography_from_corners(_POLY, _W, _H)
    b = PlayfieldBoundary(homography=H, width_cm=_W, height_cm=_H, px_per_cm=5.0)
    _, (out_w, out_h) = b.deskew_transform()
    assert (out_w, out_h) == (round(_W * 5.0), round(_H * 5.0))


def test_boundary_fallback_no_homography_uses_edge_lengths():
    # No saved homography/dimensions: deskew_transform falls back to the legacy
    # polygon-edge-length pixel rectangle (live-corner path).
    b = PlayfieldBoundary(polygon=_POLY)
    assert b.width_cm == 0.0 and b.height_cm == 0.0
    result = b.deskew_transform()
    assert result is not None
    _, (out_w, out_h) = result
    UL, UR, LR, LL = _POLY
    exp_w = max(10, int(round(max(np.linalg.norm(UR - UL), np.linalg.norm(LR - LL)))))
    exp_h = max(10, int(round(max(np.linalg.norm(LL - UL), np.linalg.norm(LR - UR)))))
    assert (out_w, out_h) == (exp_w, exp_h)


def test_boundary_no_polygon_no_transform():
    b = PlayfieldBoundary()
    assert b.get_polygon() is None
    assert b.deskew_transform() is None
    assert b.get_deskew_matrix() is None


def test_deskew_warp_runs_on_synthetic_frame():
    H = _homography_from_corners(_POLY, _W, _H)
    b = PlayfieldBoundary(homography=H, width_cm=_W, height_cm=_H)
    frame = np.full((720, 1280, 3), 128, dtype=np.uint8)
    out = b.deskew(frame)
    s = DEFAULT_PX_PER_CM
    assert out.shape[1] == round(_W * s)  # width
    assert out.shape[0] == round(_H * s)  # height


# ---------------------------------------------------------------------------
# Regression: committed global-shutter calibration (homography + W×H, no
# corner_pixels) seeds + deskews to the metric size.
# ---------------------------------------------------------------------------


def test_global_shutter_regression():
    if not _GLOBAL_SHUTTER.exists():
        pytest.skip("global-shutter-camera/calibration.json not present")
    data = json.loads(_GLOBAL_SHUTTER.read_text())
    H = np.array(data["homography"], dtype=float)
    pf = data["playfield"]
    W, Hh = float(pf["width"]), float(pf["height"])
    res = data.get("resolution", [1920, 1080])

    b = PlayfieldBoundary(homography=H, width_cm=W, height_cm=Hh)
    # Polygon seeded from H⁻¹ with no live corners.
    assert b.get_polygon() is not None

    frame = np.full((int(res[1]), int(res[0]), 3), 200, dtype=np.uint8)
    out = b.deskew(frame)
    s = DEFAULT_PX_PER_CM
    assert (out.shape[1], out.shape[0]) == (round(W * s), round(Hh * s))


# ---------------------------------------------------------------------------
# display._update_deskew uses the shared helper (third warp site)
# ---------------------------------------------------------------------------


def test_display_update_deskew_uses_shared_metric_helper():
    from aprilcam.ui.display import PlayfieldDisplay

    H = _homography_from_corners(_POLY, _W, _H)
    boundary = PlayfieldBoundary(homography=H, width_cm=_W, height_cm=_H)
    disp = PlayfieldDisplay(boundary, headless=True, deskew_overlay=True)
    frame = np.full((720, 1280, 3), 128, dtype=np.uint8)
    disp._update_deskew(frame)
    s = DEFAULT_PX_PER_CM
    assert disp.deskew_size == (round(_W * s), round(_H * s))
    # Same matrix as the boundary's shared helper.
    np.testing.assert_allclose(disp.M_deskew, boundary.get_deskew_matrix(), atol=1e-9)
