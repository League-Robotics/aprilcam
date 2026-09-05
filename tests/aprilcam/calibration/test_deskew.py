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


# ---------------------------------------------------------------------------
# Lens model (2026-09-05 fix): a calibration that carries camera_matrix /
# dist_coeffs must produce a deskewed image on which tag positions computed
# from the calibration (``world_to_deskewed_pixel`` of ``to_world``) land on
# the image content -- the plain warpPerspective of the distorted frame did
# not, and was ~1 cm / 8 px off at the corners of secondary-playfield.
# ---------------------------------------------------------------------------

# A strong barrel lens for the 1200x1000 fixture canvas: short focal length
# and a large k1 so the distortion is tens of pixels near the corners.
_LENS_K = ((600.0, 0.0, 600.0), (0.0, 600.0, 500.0), (0.0, 0.0, 1.0))
_LENS_D = (-0.45, 0.08, 0.001, -0.0005, 0.0)


def _lens_calibration() -> Calibration:
    base = _hand_built_calibration()
    return Calibration(
        stale=base.stale,
        calibrated_at=base.calibrated_at,
        playfield=base.playfield,
        homography=base.homography,
        matched_ids=base.matched_ids,
        reprojection_error_cm=base.reprojection_error_cm,
        camera_matrix=_LENS_K,
        dist_coeffs=_LENS_D,
    )


def _paint_marker_at_raw(canvas: np.ndarray, px: float, py: float, *, color: Tuple[int, int, int], size: int = 16) -> None:
    half = size // 2
    x0, y0 = int(round(px - half)), int(round(py - half))
    canvas[y0 : y0 + size, x0 : x0 + size] = color


def _red_centroid(image: np.ndarray) -> Tuple[float, float]:
    b, g, r = image[..., 0].astype(int), image[..., 1].astype(int), image[..., 2].astype(int)
    mask = (r > 150) & (g < 100) & (b < 100)
    ys, xs = np.nonzero(mask)
    assert xs.size > 0, "no red marker found in the deskewed image"
    return float(xs.mean()), float(ys.mean())


def _deskewed_to_world(deskewer: Deskewer, x: float, y: float) -> WorldPoint:
    w, h = deskewer.world_extent
    return WorldPoint(x=x / deskewer.px_per_cm - w / 2.0, y=h / 2.0 - y / deskewer.px_per_cm)


def test_lens_model_deskew_puts_image_content_where_the_calibration_says_it_is():
    """The defect, stated as an acceptance test: paint a marker at the
    *distorted* raw pixel the calibration's own ``to_pixel`` gives for a
    world point near a corner, deskew, and the marker's centroid must
    land within a pixel of ``world_to_deskewed_pixel`` for that world
    point -- exactly where ``client/renderer.py`` draws an overlay for a
    tag the daemon reports there."""
    calibration = _lens_calibration()
    half_w, half_h = _WIDTH_CM / 2.0, _HEIGHT_CM / 2.0
    world = WorldPoint(x=-half_w + 12.0, y=half_h - 10.0)
    raw = calibration.to_pixel(world)
    canvas = _blank_canvas()
    size = 8
    _paint_marker_at_raw(canvas, raw.x, raw.y, color=(0, 0, 255), size=size)
    # The painted square sits on whole pixels, so predict from its actual
    # centre (pixel-centre convention: index i covers [i-0.5, i+0.5)),
    # not from the unrounded target.
    x0, y0 = int(round(raw.x - size // 2)), int(round(raw.y - size // 2))
    painted_centre = calibration.to_world(
        type(raw)(x=x0 + (size - 1) / 2.0, y=y0 + (size - 1) / 2.0)
    )

    deskewer = Deskewer(calibration=calibration, width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)
    warped = deskewer.deskew(canvas)
    predicted = geometry.world_to_deskewed_pixel(
        painted_centre, world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm
    )
    cx, cy = _red_centroid(warped)

    assert abs(cx - predicted.x) < 1.0 and abs(cy - predicted.y) < 1.0, (
        f"marker centroid ({cx:.1f}, {cy:.1f}) vs predicted ({predicted.x:.1f}, {predicted.y:.1f})"
    )


def test_without_lens_correction_the_same_scene_is_visibly_off_proving_the_test_has_teeth():
    """Warp the identical distorted scene with the pre-fix homography-
    only path (a calibration stripped of its lens model, so ``to_pixel``
    of the corners is the plain homography) and the marker lands several
    pixels from the prediction -- the radial disagreement this fix removes."""
    lens = _lens_calibration()
    half_w, half_h = _WIDTH_CM / 2.0, _HEIGHT_CM / 2.0
    world = WorldPoint(x=-half_w + 12.0, y=half_h - 10.0)
    raw = lens.to_pixel(world)  # distorted, as a real camera would show it
    canvas = _blank_canvas()
    _paint_marker_at_raw(canvas, raw.x, raw.y, color=(0, 0, 255))

    # Same corners, distorted, but warped without undistortion: build the
    # source quad the way the old path did (to_pixel through the lens) and
    # feed it to a lens-less Deskewer via a calibration whose homography
    # is that same quad's plain solve -- simplest is to call cv2 directly.
    deskewer = Deskewer(calibration=lens, width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)
    corners = geometry.world_corners(_WIDTH_CM, _HEIGHT_CM)
    src = np.array([[p.x, p.y] for p in (lens.to_pixel(w) for w in corners)], dtype=np.float32)
    out_w, out_h = deskewer.output_size
    dst = np.array([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]], dtype=np.float32)
    naive = cv.warpPerspective(canvas, cv.getPerspectiveTransform(src, dst), (out_w, out_h))

    predicted = geometry.world_to_deskewed_pixel(world, world_extent=deskewer.world_extent, px_per_cm=deskewer.px_per_cm)
    cx, cy = _red_centroid(naive)
    assert np.hypot(cx - predicted.x, cy - predicted.y) > 4.0


def test_lens_maps_match_the_calibration_to_pixel_everywhere():
    """The remap tables must agree with ``Calibration.to_pixel`` (which
    reaches the same distorted pixel by iterating ``cv2.undistortPoints``)
    to well under a pixel across the whole canvas, not just at the four
    corners -- this ties the closed-form model in ``_build_lens_maps`` to
    OpenCV's own."""
    calibration = _lens_calibration()
    deskewer = Deskewer(calibration=calibration, width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)
    maps = deskewer._build_lens_maps()
    assert maps is not None
    map_x, map_y = maps
    out_w, out_h = deskewer.output_size
    assert map_x.shape == (out_h, out_w) and map_x.dtype == np.float32

    rng = np.random.default_rng(7)
    worst = 0.0
    for _ in range(200):
        u = int(rng.integers(0, out_w))
        v = int(rng.integers(0, out_h))
        expected = calibration.to_pixel(_deskewed_to_world(deskewer, u, v))
        worst = max(worst, float(np.hypot(map_x[v, u] - expected.x, map_y[v, u] - expected.y)))
    assert worst < 0.1, f"worst map/to_pixel disagreement {worst:.3f} px"


def test_no_lens_model_takes_the_original_warp_path_unchanged():
    """A homography-only calibration must produce byte-identical output
    to a direct ``cv2.warpPerspective`` through the corner quad -- the
    pre-fix behaviour, preserved exactly for every camera without a lens
    model (Linux/Pi fleets calibrate without intrinsics by default)."""
    calibration = _hand_built_calibration()
    canvas = _blank_canvas()
    _paint_marker(canvas, WorldPoint(x=20.0, y=-10.0), color=(0, 0, 255))
    deskewer = Deskewer(calibration=calibration, width_cm=_WIDTH_CM, height_cm=_HEIGHT_CM)
    warped = deskewer.deskew(canvas)
    assert deskewer._build_lens_maps() is None

    corners = geometry.world_corners(_WIDTH_CM, _HEIGHT_CM)
    src = np.array([[p.x, p.y] for p in (calibration.to_pixel(w) for w in corners)], dtype=np.float32)
    out_w, out_h = deskewer.output_size
    dst = np.array([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]], dtype=np.float32)
    expected = cv.warpPerspective(canvas, cv.getPerspectiveTransform(src, dst), (out_w, out_h))
    assert np.array_equal(warped, expected)
