"""Tests for ``aprilcam.calibration.deskew`` (ticket 022-005).

Headless, no camera hardware. ``Deskewer`` is exercised against a
hand-built, affine ``Calibration`` (the same ``_affine_homography``-style
approach ``test_geometry.py`` uses for ``pixel_to_world``/``world_to_pixel``)
rather than a real ArUco solve -- this keeps the raw-pixel <-> world
correspondence exactly known by hand, so a marker's predicted output
position can be computed independently of ``Deskewer`` itself and
compared against what the real ``cv2.warpPerspective`` warp actually
produced.

**The permanent geometry-convention suite** (dev-plan Stage 6, never
relaxed) is exercised here too, one level up from ``test_geometry.py``'s
pure-math version: real image content, warped through real OpenCV code,
still comes out north-up/east-right with the anchor invariant honoured.
"""

from __future__ import annotations

from typing import Tuple

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration import geometry
from aprilcam.calibration.calibration import Calibration
from aprilcam.calibration.deskew import Deskewer
from aprilcam.types import WorldPoint

#: A plain top-down, north-up synthetic camera mount: pixel-x increases
#: with world-x (east), pixel-y *decreases* with world-y (north sits
#: higher in the image, rows grow downward) -- fixture ground truth,
#: not production code (mirrors ``test_calibration.py``'s
#: ``standard_pixel_for_world``, independently re-derived here so this
#: module needs no cross-import).
_SCALE_PX_PER_CM = 4.0
_ORIGIN_PX = (600.0, 500.0)
_CANVAS_SIZE = (1200, 1000)  # (width, height)

_WIDTH_CM = 134.3
_HEIGHT_CM = 89.3


def _raw_pixel_for_world(x: float, y: float) -> Tuple[float, float]:
    cx, cy = _ORIGIN_PX
    return (cx + x * _SCALE_PX_PER_CM, cy - y * _SCALE_PX_PER_CM)


def _hand_built_calibration() -> Calibration:
    """A ``Calibration`` whose homography is exactly the affine mapping
    :func:`_raw_pixel_for_world` inverts -- i.e. ``to_pixel(world) ==
    _raw_pixel_for_world(world.x, world.y)`` and ``to_world`` is its
    true inverse. Built the same way ``test_geometry.py``'s
    ``_affine_homography`` is: a homogeneous matrix with a trivial
    (no-perspective) third row, so the correspondence is exact and
    hand-verifiable, not merely solved-and-trusted."""
    cx, cy = _ORIGIN_PX
    s = _SCALE_PX_PER_CM
    # world_x = (u - cx) / s ; world_y = -(v - cy) / s
    homography = (
        (1.0 / s, 0.0, -cx / s),
        (0.0, -1.0 / s, cy / s),
        (0.0, 0.0, 1.0),
    )
    return Calibration(
        stale=False,
        calibrated_at=0.0,
        playfield="main-playfield",
        homography=homography,
        matched_ids=tuple(range(1, 11)),
        reprojection_error_cm=0.0,
    )


def _blank_canvas() -> np.ndarray:
    return np.full((_CANVAS_SIZE[1], _CANVAS_SIZE[0], 3), 255, dtype=np.uint8)


def _paint_marker(canvas: np.ndarray, world: WorldPoint, *, color: Tuple[int, int, int], size: int = 20) -> None:
    """Paint a small solid *color* square centred on *world*'s raw pixel
    position (via :func:`_raw_pixel_for_world`, the fixture's own known
    ground truth -- exactly what :func:`_hand_built_calibration`'s
    homography inverts)."""
    px, py = _raw_pixel_for_world(world.x, world.y)
    half = size // 2
    x0, y0 = int(round(px - half)), int(round(py - half))
    canvas[y0 : y0 + size, x0 : x0 + size] = color


def _mean_color_near(image: np.ndarray, x: float, y: float, *, radius: int = 6) -> np.ndarray:
    height, width = image.shape[:2]
    x0, x1 = max(0, int(x) - radius), min(width, int(x) + radius + 1)
    y0, y1 = max(0, int(y) - radius), min(height, int(y) + radius + 1)
    patch = image[y0:y1, x0:x1]
    assert patch.size > 0, f"neighbourhood of ({x}, {y}) falls entirely outside a {width}x{height} image"
    return patch.reshape(-1, patch.shape[-1]).mean(axis=0)


def _color_distance(patch: np.ndarray, color: Tuple[int, int, int]) -> float:
    """Euclidean BGR distance between *patch* (a mean colour, as
    :func:`_mean_color_near` returns) and *color*. A single-channel
    comparison is not enough here: e.g. red is ``(0, 0, 255)`` in BGR
    and white background is ``(255, 255, 255)`` -- they share the same
    value (255) on the R channel, so comparing that channel alone
    cannot tell a red marker apart from bare white canvas. The full
    3-channel distance can."""
    return float(np.linalg.norm(patch.astype(np.float64) - np.array(color, dtype=np.float64)))


# ---------------------------------------------------------------------------
# Sizing / derived metadata -- UC-06.S-1, UC-19.S-1
# ---------------------------------------------------------------------------


def test_main_playfield_deskews_to_exactly_800_by_532():
    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)

    assert deskewer.output_size == (800, 532)


def test_px_per_cm_is_derived_from_the_actual_output_width_not_a_constant():
    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)

    assert deskewer.px_per_cm == pytest.approx(deskewer.output_size[0] / _WIDTH_CM)
    assert deskewer.px_per_cm == pytest.approx(800 / 134.3)


def test_px_per_cm_derivation_holds_for_a_second_differently_sized_playfield():
    """Not a coincidence of main-playfield's own numbers -- a second,
    height-constrained synthetic field derives its own px_per_cm the
    same way."""
    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=50.0, height_cm=100.0)

    assert deskewer.output_size == (400, 800)
    assert deskewer.px_per_cm == pytest.approx(deskewer.output_size[0] / 50.0)


def test_world_extent_reports_the_playfield_dimensions_verbatim():
    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)

    assert deskewer.world_extent == (_WIDTH_CM, _HEIGHT_CM)


def test_deskew_output_array_shape_matches_output_size():
    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)

    warped = deskewer.deskew(_blank_canvas())

    out_w, out_h = deskewer.output_size
    assert warped.shape[:2] == (out_h, out_w)


def test_max_dimension_override_is_honoured():
    deskewer = Deskewer(
        calibration=_hand_built_calibration(), width_cm=100.0, height_cm=100.0, max_dimension_px=400
    )

    assert deskewer.output_size == (400, 400)


# ---------------------------------------------------------------------------
# Anchor invariant + north-up/east-right, on real warped image content --
# permanent geometry-convention suite, dev-plan Stage 6, never relaxed.
# ---------------------------------------------------------------------------


def test_anchor_invariant_nw_corner_content_lands_near_the_predicted_pixel():
    """image (0, 0) <-> world (-width/2, +height/2): paint a marker a
    few cm inside the field's north-west corner (not exactly on it, so
    the whole marker stays inside the visible canvas) and confirm it
    warps to the predicted output-pixel neighbourhood -- computed via
    ``geometry.world_to_deskewed_pixel``, the same anchor-invariant
    formula ``Deskewer`` and ``client/renderer.py`` both share, so this
    also proves the two agree on where the anchor actually is."""
    half_w, half_h = _WIDTH_CM / 2.0, _HEIGHT_CM / 2.0
    near_nw = WorldPoint(x=-half_w + 5.0, y=half_h - 5.0)
    canvas = _blank_canvas()
    _paint_marker(canvas, near_nw, color=(0, 0, 255))  # red (BGR)

    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)
    warped = deskewer.deskew(canvas)

    predicted = geometry.world_to_deskewed_pixel(
        near_nw, world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm
    )
    # The prediction itself should be close to the output's (0, 0)
    # corner -- 5 cm in from the true corner, scaled by px_per_cm.
    assert predicted.x == pytest.approx(5.0 * deskewer.px_per_cm, abs=1.0)
    assert predicted.y == pytest.approx(5.0 * deskewer.px_per_cm, abs=1.0)

    marker_patch = _mean_color_near(warped, predicted.x, predicted.y)
    background_patch = _mean_color_near(
        warped, deskewer.output_size[0] - 5, deskewer.output_size[1] - 5
    )
    assert _color_distance(marker_patch, (0, 0, 255)) < _color_distance(marker_patch, (255, 255, 255))
    assert _color_distance(background_patch, (255, 255, 255)) < 20


def test_deskewed_image_is_a_clean_rectangle_not_a_trapezoid():
    """Content near each of the four field corners, once warped, should
    land close to the corresponding corner of the fixed output canvas
    (via the shared anchor formula) -- the warp maps the field
    quadrilateral to a true rectangle, not merely 'somewhere in
    frame'."""
    half_w, half_h = _WIDTH_CM / 2.0, _HEIGHT_CM / 2.0
    inset = 5.0  # cm inside each corner, so no marker straddles the canvas edge
    calibration = _hand_built_calibration()
    deskewer = Deskewer(calibration=calibration, width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)

    world_near_corners = {
        "nw": WorldPoint(x=-half_w + inset, y=half_h - inset),
        "ne": WorldPoint(x=half_w - inset, y=half_h - inset),
        "se": WorldPoint(x=half_w - inset, y=-half_h + inset),
        "sw": WorldPoint(x=-half_w + inset, y=-half_h + inset),
    }
    colors = {"nw": (0, 0, 255), "ne": (0, 255, 0), "se": (255, 0, 0), "sw": (0, 255, 255)}
    canvas = _blank_canvas()
    for name, world in world_near_corners.items():
        _paint_marker(canvas, world, color=colors[name], size=24)

    warped = deskewer.deskew(canvas)

    for name, world in world_near_corners.items():
        predicted = geometry.world_to_deskewed_pixel(
            world, world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm
        )
        patch = _mean_color_near(warped, predicted.x, predicted.y)
        expected_color = np.array(colors[name], dtype=np.float64)
        # The dominant colour near each predicted corner should be much
        # closer to that corner's own paint than to white background.
        assert np.linalg.norm(patch - expected_color) < np.linalg.norm(
            patch - np.array([255.0, 255.0, 255.0])
        ), f"corner {name!r} did not land where the anchor invariant predicts"


def test_moving_east_moves_the_marker_right_on_the_deskewed_image():
    canvas = _blank_canvas()
    _paint_marker(canvas, WorldPoint(x=-20.0, y=0.0), color=(0, 0, 255))  # west-ish, red
    _paint_marker(canvas, WorldPoint(x=20.0, y=0.0), color=(0, 255, 0))  # east-ish, green

    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)
    warped = deskewer.deskew(canvas)

    west_pixel = geometry.world_to_deskewed_pixel(
        WorldPoint(x=-20.0, y=0.0), world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm
    )
    east_pixel = geometry.world_to_deskewed_pixel(
        WorldPoint(x=20.0, y=0.0), world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm
    )
    assert east_pixel.x > west_pixel.x

    at_west = _mean_color_near(warped, west_pixel.x, west_pixel.y)
    at_east = _mean_color_near(warped, east_pixel.x, east_pixel.y)
    # The red (west) marker's content is where the west prediction says,
    # the green (east) marker's is where the east prediction says --
    # not swapped, not both smeared across both positions.
    assert _color_distance(at_west, (0, 0, 255)) < _color_distance(at_west, (0, 255, 0))
    assert _color_distance(at_east, (0, 255, 0)) < _color_distance(at_east, (0, 0, 255))


def test_moving_north_moves_the_marker_up_the_y_flip_catching_check():
    """The Definition of Done's explicit check: a tag moved north
    appears further *up* (smaller pixel-y) on the deskewed image."""
    canvas = _blank_canvas()
    _paint_marker(canvas, WorldPoint(x=0.0, y=-20.0), color=(0, 0, 255))  # south-ish, red
    _paint_marker(canvas, WorldPoint(x=0.0, y=20.0), color=(0, 255, 0))  # north-ish, green

    deskewer = Deskewer(calibration=_hand_built_calibration(), width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)
    warped = deskewer.deskew(canvas)

    south_pixel = geometry.world_to_deskewed_pixel(
        WorldPoint(x=0.0, y=-20.0), world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm
    )
    north_pixel = geometry.world_to_deskewed_pixel(
        WorldPoint(x=0.0, y=20.0), world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm
    )
    assert north_pixel.y < south_pixel.y  # north is up: smaller pixel-y

    at_north = _mean_color_near(warped, north_pixel.x, north_pixel.y)
    at_south = _mean_color_near(warped, south_pixel.x, south_pixel.y)
    # The green (north) marker's content is at the north prediction, the
    # red (south) marker's is at the south prediction -- never swapped,
    # which is exactly what a hardcoded/backwards y-flip would produce.
    assert _color_distance(at_north, (0, 255, 0)) < _color_distance(at_north, (0, 0, 255))
    assert _color_distance(at_south, (0, 0, 255)) < _color_distance(at_south, (0, 255, 0))
