"""Tests for ``aprilcam.calibration.geometry`` (ticket 022-003).

Pure math tests: no camera, no vision, no ``Calibrator`` -- every
homography here is hand-built or solved from a small hand-chosen
correspondence set. See ``test_calibration.py`` for the fixture-level
(rendered-image) half of the geometry-convention suite this ticket's
Definition of Done also requires.

**The permanent geometry-convention suite** (dev-plan Stage 6, never
relaxed): east increases ``to_world(...).x``, north increases
``to_world(...).y``, and nothing in this module performs an
independent origin shift or y-flip of its own -- whatever a homography
encodes is exactly what ``pixel_to_world``/``world_to_pixel`` report,
proven below by feeding two *differently-mounted* synthetic cameras
(standard and intentionally inverted) through the same functions and
checking both track their own ground truth rather than a hardcoded
assumption.
"""

from __future__ import annotations

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration import geometry
from aprilcam.types import PixelPoint, WorldPoint


def _affine_homography(*, sx: float, ox: float, sy: float, oy: float) -> np.ndarray:
    """A 3x3 homogeneous matrix representing the pure affine mapping
    ``world_x = sx*pixel_x + ox``, ``world_y = sy*pixel_y + oy`` -- a
    homography with a trivial (no perspective) third row, so its
    behaviour is exactly predictable by hand."""
    return np.array(
        [
            [sx, 0.0, ox],
            [0.0, sy, oy],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


# ---------------------------------------------------------------------------
# apply_homography -- the shared primitive
# ---------------------------------------------------------------------------


def test_apply_homography_identity_is_a_no_op():
    identity = np.eye(3)
    assert geometry.apply_homography(identity, 12.5, -7.25) == (12.5, -7.25)


def test_apply_homography_matches_hand_computed_affine_result():
    # world_x = 2*u - 100, world_y = -3*v + 150
    matrix = _affine_homography(sx=2.0, ox=-100.0, sy=-3.0, oy=150.0)

    x, y = geometry.apply_homography(matrix, 10.0, 5.0)

    assert x == pytest.approx(2 * 10 - 100)
    assert y == pytest.approx(-3 * 5 + 150)


def test_apply_homography_raises_on_a_point_mapped_to_infinity():
    # A degenerate matrix whose bottom row zeroes out w for this input.
    matrix = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.0]])
    with pytest.raises(ValueError):
        geometry.apply_homography(matrix, 1.0, 0.0)


# ---------------------------------------------------------------------------
# pixel_to_world / world_to_pixel
# ---------------------------------------------------------------------------


def test_pixel_to_world_delegates_to_apply_homography():
    matrix = _affine_homography(sx=2.0, ox=-100.0, sy=-3.0, oy=150.0)

    world = geometry.pixel_to_world(matrix, PixelPoint(x=10.0, y=5.0))

    assert world == WorldPoint(x=-80.0, y=135.0)


def test_world_to_pixel_is_the_true_inverse_of_pixel_to_world():
    matrix = _affine_homography(sx=2.0, ox=-100.0, sy=-3.0, oy=150.0)

    world = geometry.pixel_to_world(matrix, PixelPoint(x=10.0, y=5.0))
    pixel = geometry.world_to_pixel(matrix, world)

    assert pixel.x == pytest.approx(10.0)
    assert pixel.y == pytest.approx(5.0)


def test_round_trip_to_pixel_of_to_world_returns_the_original_pixel():
    """``to_pixel(to_world(p)) ~= p`` -- the ticket's explicit testing
    requirement -- checked across several points and a homography
    solved (not hand-built) from four correspondences, so this also
    exercises a realistic, not merely toy, matrix."""
    pixel_pts = np.array([[100.0, 100.0], [500.0, 100.0], [500.0, 400.0], [100.0, 400.0]], dtype=np.float32)
    world_pts = np.array([[-50.0, 40.0], [50.0, 40.0], [50.0, -40.0], [-50.0, -40.0]], dtype=np.float32)
    matrix, _mask = cv.findHomography(pixel_pts, world_pts, method=0)
    assert matrix is not None

    for px, py in [(100.0, 100.0), (300.0, 250.0), (420.0, 180.0), (150.0, 390.0)]:
        world = geometry.pixel_to_world(matrix, PixelPoint(x=px, y=py))
        pixel = geometry.world_to_pixel(matrix, world)
        assert pixel.x == pytest.approx(px, abs=1e-6)
        assert pixel.y == pytest.approx(py, abs=1e-6)


# ---------------------------------------------------------------------------
# Geometry convention suite (permanent, never relaxed): east -> larger x,
# north -> larger y, and no hardcoded y-flip anywhere in this module.
# ---------------------------------------------------------------------------


def test_east_in_pixel_space_produces_a_larger_world_x():
    """A homography where increasing pixel-x is "further east" --
    moving east must increase ``to_world(...).x``."""
    matrix = _affine_homography(sx=1.0, ox=0.0, sy=-1.0, oy=0.0)

    near = geometry.pixel_to_world(matrix, PixelPoint(x=10.0, y=0.0))
    far_east = geometry.pixel_to_world(matrix, PixelPoint(x=40.0, y=0.0))

    assert far_east.x > near.x


def test_north_up_camera_produces_a_larger_world_y_for_a_smaller_pixel_y():
    """A standard top-down, north-up mount: image rows grow downward,
    so a point further north (larger world y) sits at a *smaller*
    pixel-y. Moving north (in world terms) must still increase
    ``to_world(...).y`` -- the mirror-catching check."""
    matrix = _affine_homography(sx=1.0, ox=0.0, sy=-1.0, oy=0.0)

    south_ish = geometry.pixel_to_world(matrix, PixelPoint(x=0.0, y=40.0))
    north_ish = geometry.pixel_to_world(matrix, PixelPoint(x=0.0, y=10.0))

    assert north_ish.y > south_ish.y


def test_to_world_tracks_whatever_the_homography_encodes_not_a_hardcoded_flip():
    """The load-bearing regression: construct a second, deliberately
    *inverted*-mount homography (pixel-y increasing WITH world-y,
    the opposite sign relationship from the standard mount above). If
    ``pixel_to_world``/``apply_homography`` ever grew a hardcoded
    ``y = -y`` step, this test would fail even though the "normal
    mount" tests above kept passing -- exactly the silent-mirroring
    failure mode ``design.md`` Sec.6 warns is the single most expensive
    bug class in this project. Both mounts must independently satisfy
    "further north (world) -> larger world y" using *their own*
    pixel<->world relationship, never a shared assumption baked into
    this module.
    """
    inverted_matrix = _affine_homography(sx=1.0, ox=0.0, sy=1.0, oy=0.0)

    near = geometry.pixel_to_world(inverted_matrix, PixelPoint(x=0.0, y=10.0))
    far_north = geometry.pixel_to_world(inverted_matrix, PixelPoint(x=0.0, y=40.0))

    assert far_north.y > near.y


def test_origin_pixel_maps_to_world_origin_with_no_extra_shift():
    """The origin-shift invariant, directly: whatever pixel a
    homography says maps to world (0, 0) must map to *exactly* (0, 0)
    -- geometry.py performs no additional subtraction or flip of its
    own beyond the matrix multiply (see the module docstring)."""
    matrix = _affine_homography(sx=1.0, ox=-25.0, sy=-1.0, oy=60.0)
    # This matrix's own algebra says pixel (25, 60) is world (0, 0).
    origin_pixel = PixelPoint(x=25.0, y=60.0)

    world = geometry.pixel_to_world(matrix, origin_pixel)

    assert world.x == pytest.approx(0.0, abs=1e-9)
    assert world.y == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# deskewed_output_size (ticket 022-005): the fixed-bounding-box sizing rule
# ---------------------------------------------------------------------------


def test_deskewed_output_size_main_playfield_is_exactly_800_by_532():
    """The ticket's own worked example: 134.3 x 89.3 cm into an 800x800
    box, aspect-preserved, width-constrained."""
    width_px, height_px = geometry.deskewed_output_size(134.3, 89.3)

    assert (width_px, height_px) == (800, 532)


def test_deskewed_output_size_is_aspect_preserved_and_width_or_height_constrained():
    """A field taller than it is wide is height-constrained instead --
    proving the sizing rule picks whichever dimension is larger, not
    always width."""
    width_px, height_px = geometry.deskewed_output_size(50.0, 100.0, max_dimension_px=800)

    assert height_px == 800  # the larger physical dimension hits the box exactly
    assert width_px == pytest.approx(400, abs=1)  # half the height, aspect-preserved


def test_deskewed_output_size_honours_a_max_dimension_override():
    width_px, height_px = geometry.deskewed_output_size(100.0, 100.0, max_dimension_px=400)

    assert (width_px, height_px) == (400, 400)


def test_deskewed_output_size_rejects_non_positive_dimensions():
    with pytest.raises(ValueError):
        geometry.deskewed_output_size(0.0, 89.3)
    with pytest.raises(ValueError):
        geometry.deskewed_output_size(134.3, -1.0)


# ---------------------------------------------------------------------------
# world_to_deskewed_pixel (ticket 022-005): the anchor invariant. Permanent
# geometry-convention suite, dev-plan Stage 6 -- never relaxed.
# ---------------------------------------------------------------------------


def test_anchor_invariant_field_nw_corner_maps_to_pixel_origin():
    """``types.ImageFrame``'s own contract, checked directly: the
    field's north-west corner, world (-width/2, +height/2), is image
    (0, 0) -- main-playfield's own numbers (67.15, 44.65)."""
    px_per_cm = 800 / 134.3

    pixel = geometry.world_to_deskewed_pixel(
        WorldPoint(x=-67.15, y=44.65), world_extent=(134.3, 89.3), px_per_cm=px_per_cm
    )

    assert pixel.x == pytest.approx(0.0, abs=1e-6)
    assert pixel.y == pytest.approx(0.0, abs=1e-6)


def test_anchor_invariant_field_se_corner_maps_to_the_output_canvas_corner():
    px_per_cm = 800 / 134.3

    pixel = geometry.world_to_deskewed_pixel(
        WorldPoint(x=67.15, y=-44.65), world_extent=(134.3, 89.3), px_per_cm=px_per_cm
    )

    assert pixel.x == pytest.approx(800.0, abs=1e-6)
    # Height is not width-constrained, so this lands close to, but not
    # necessarily bit-exact at, 532 -- deskewed_output_size's own
    # docstring explains why a non-square field's shorter dimension can
    # be a sub-pixel off the exact scale.
    assert pixel.y == pytest.approx(89.3 * px_per_cm, abs=1e-6)


def test_anchor_invariant_field_centre_maps_to_the_output_centre():
    width_cm, height_cm = 134.3, 89.3
    px_per_cm = 800 / width_cm

    pixel = geometry.world_to_deskewed_pixel(
        WorldPoint(x=0.0, y=0.0), world_extent=(width_cm, height_cm), px_per_cm=px_per_cm
    )

    # Field centre (world origin) sits at exactly half the output's
    # width/height, in pixels -- computed from the same px_per_cm, not
    # a hand-rounded literal (800/2 == 400 is exact; the height half is
    # not, since only width is guaranteed to hit the box exactly).
    assert pixel.x == pytest.approx(800 / 2, abs=1e-6)
    assert pixel.y == pytest.approx(height_cm / 2 * px_per_cm, abs=1e-6)


def test_moving_east_increases_deskewed_pixel_x_the_mirror_catching_check():
    """North is up, east is right (design.md Sec.6): a tag moved east
    must appear further right on the deskewed image."""
    extent = (134.3, 89.3)
    px_per_cm = 800 / 134.3

    west_ish = geometry.world_to_deskewed_pixel(
        WorldPoint(x=-10.0, y=0.0), world_extent=extent, px_per_cm=px_per_cm
    )
    east_ish = geometry.world_to_deskewed_pixel(
        WorldPoint(x=10.0, y=0.0), world_extent=extent, px_per_cm=px_per_cm
    )

    assert east_ish.x > west_ish.x


def test_moving_north_decreases_deskewed_pixel_y_the_y_flip_catching_check():
    """The single sanctioned y-flip, exercised directly: a tag moved
    north (larger world y) must appear *higher* on the image -- a
    *smaller* pixel-y, since image rows grow downward. Getting this
    backwards is the silent north/south mirror this project fears most
    (design.md Sec.6)."""
    extent = (134.3, 89.3)
    px_per_cm = 800 / 134.3

    south_ish = geometry.world_to_deskewed_pixel(
        WorldPoint(x=0.0, y=-10.0), world_extent=extent, px_per_cm=px_per_cm
    )
    north_ish = geometry.world_to_deskewed_pixel(
        WorldPoint(x=0.0, y=10.0), world_extent=extent, px_per_cm=px_per_cm
    )

    assert north_ish.y < south_ish.y
