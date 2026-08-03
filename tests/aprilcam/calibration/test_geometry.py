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

import math

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration import geometry
from aprilcam.types import MountParameters, PixelPoint, WorldPoint


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


# ---------------------------------------------------------------------------
# project_tag_to_field_plane (ticket 025-003, UC-12): parallax projection
# ---------------------------------------------------------------------------


def test_project_tag_to_field_plane_zero_mount_height_is_a_no_op():
    """Identity case: no elevation, nothing to correct."""
    tag_world = WorldPoint(x=12.0, y=-4.0)

    corrected = geometry.project_tag_to_field_plane(
        tag_world,
        mount_z_cm=0.0,
        camera_ground_position=WorldPoint(x=0.0, y=0.0),
        camera_height_cm=200.0,
    )

    assert corrected == tag_world


def test_project_tag_to_field_plane_zero_camera_height_is_a_no_op():
    """No known camera height means nothing to correct against -- the
    guard that keeps this a safe no-op rather than a division by zero."""
    tag_world = WorldPoint(x=12.0, y=-4.0)

    corrected = geometry.project_tag_to_field_plane(
        tag_world,
        mount_z_cm=20.0,
        camera_ground_position=WorldPoint(x=0.0, y=0.0),
        camera_height_cm=0.0,
    )

    assert corrected == tag_world


def test_project_tag_to_field_plane_hand_computed_worked_example():
    """The worked example from the docstring, by hand:

    Camera sits at ground position (0, 0), 100 cm up. A tag's true ground
    footprint is (40, 30), but it is mounted 20 cm above the field, so a
    homography (which assumes points lie on z=0) reports it at an
    *apparent* position extrapolated further from the camera:

        t = camera_height / (camera_height - mount_z) = 100 / 80 = 1.25
        apparent = camera + t * (true_footprint - camera)
                 = (0, 0) + 1.25 * (40, 30) = (50, 37.5)

    ``project_tag_to_field_plane`` must invert this and recover exactly
    (40, 30) given the same camera geometry and mount height, using its
    own (equivalent) ratio = mount_z / camera_height = 0.2.
    """
    apparent = WorldPoint(x=50.0, y=37.5)
    camera_ground_position = WorldPoint(x=0.0, y=0.0)

    corrected = geometry.project_tag_to_field_plane(
        apparent,
        mount_z_cm=20.0,
        camera_ground_position=camera_ground_position,
        camera_height_cm=100.0,
    )

    assert corrected.x == pytest.approx(40.0)
    assert corrected.y == pytest.approx(30.0)


def test_project_tag_to_field_plane_moves_measurably_closer_to_ground_truth():
    """UC-12.S-2: with mount_z registered, the corrected position must be
    measurably closer to the true footprint than the raw/apparent one."""
    true_footprint = WorldPoint(x=40.0, y=30.0)
    apparent = WorldPoint(x=50.0, y=37.5)  # from the worked example above
    camera_ground_position = WorldPoint(x=0.0, y=0.0)

    corrected = geometry.project_tag_to_field_plane(
        apparent,
        mount_z_cm=20.0,
        camera_ground_position=camera_ground_position,
        camera_height_cm=100.0,
    )

    def _dist(a: WorldPoint, b: WorldPoint) -> float:
        return math.hypot(a.x - b.x, a.y - b.y)

    uncorrected_error = _dist(apparent, true_footprint)
    corrected_error = _dist(corrected, true_footprint)

    assert uncorrected_error > 0.0  # sanity: the raw position really is off
    assert corrected_error < uncorrected_error
    assert corrected_error == pytest.approx(0.0, abs=1e-9)


def test_project_tag_to_field_plane_changes_position_by_a_predictable_amount():
    """A known mount height moves the apparent position toward the
    camera's ground position by exactly ``ratio = mount_z / camera_height``
    of the vector from the apparent position to the camera -- checked as
    an explicit displacement, not just "some change happened"."""
    apparent = WorldPoint(x=50.0, y=37.5)
    camera_ground_position = WorldPoint(x=0.0, y=0.0)
    ratio = 20.0 / 100.0

    corrected = geometry.project_tag_to_field_plane(
        apparent,
        mount_z_cm=20.0,
        camera_ground_position=camera_ground_position,
        camera_height_cm=100.0,
    )

    expected_dx = ratio * (camera_ground_position.x - apparent.x)
    expected_dy = ratio * (camera_ground_position.y - apparent.y)
    assert corrected.x == pytest.approx(apparent.x + expected_dx)
    assert corrected.y == pytest.approx(apparent.y + expected_dy)


# ---------------------------------------------------------------------------
# apply_mount_offset (ticket 025-003, UC-12): offset + yaw composition
# ---------------------------------------------------------------------------


def test_apply_mount_offset_zero_offset_and_yaw_is_a_no_op():
    """Identity case: no offset, no mount yaw -- the reported pose is the
    observed pose, unchanged."""
    field_position = WorldPoint(x=-3.0, y=9.0)
    yaw_rad = math.radians(37.0)

    robot_position, robot_yaw_rad = geometry.apply_mount_offset(
        field_position, yaw_rad, MountParameters()
    )

    assert robot_position == field_position
    assert robot_yaw_rad == pytest.approx(yaw_rad)


def test_apply_mount_offset_hand_computed_offset_no_mount_yaw():
    """UC-12.S-3's own numbers (mount_x=8, mount_y=3), zero mount yaw, tag
    observed facing north (yaw_rad = 90 degrees).

    Facing north, "forward" is +y (north) and "left" (90 degrees CCW from
    forward) is -x (west) -- so the tag, mounted 8 cm forward and 3 cm
    left of the robot's reference point, sits at
    ``robot_position + (8 cm north) + (3 cm west)``
    ``= robot_position + (0, 8) + (-3, 0) = robot_position + (-3, 8)``.
    Given the tag's observed field position, the robot's reference point
    is therefore ``field_position - (-3, 8) = field_position + (3, -8)``.
    """
    field_position = WorldPoint(x=50.0, y=37.5)
    mount = MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=0.0)

    robot_position, robot_yaw_rad = geometry.apply_mount_offset(
        field_position, math.radians(90.0), mount
    )

    assert robot_position.x == pytest.approx(53.0)
    assert robot_position.y == pytest.approx(29.5)
    assert robot_yaw_rad == pytest.approx(math.radians(90.0))


def test_uc12_s3_rotating_in_place_keeps_the_robot_position_fixed():
    """UC-12.S-3: an off-centre, elevated tag corrects to the robot's
    reference point; as the synthetic robot rotates in place, the
    corrected position stays fixed while the raw tag position sweeps a
    circle around it.

    Simulate the robot rotating in place at a fixed reference point
    (0, 0) with mount_x=8, mount_y=3, mount_yaw_rad=0: the tag's world
    position at heading ``yaw`` is
    ``robot_position + R(yaw) @ (mount_x, mount_y)`` (the forward
    computation -- ``apply_mount_offset`` is checked here as its exact
    inverse). The raw tag position must trace a circle of radius
    ``hypot(mount_x, mount_y)`` around (0, 0); the corrected position
    must land back on (0, 0) at every heading.
    """
    robot_position = WorldPoint(x=0.0, y=0.0)
    mount = MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=0.0)
    radius = math.hypot(mount.mount_x, mount.mount_y)

    raw_tag_positions = []
    for heading_deg in (0.0, 45.0, 90.0, 180.0, 270.0):
        yaw_rad = math.radians(heading_deg)
        c, s = math.cos(yaw_rad), math.sin(yaw_rad)
        tag_world = WorldPoint(
            x=robot_position.x + (mount.mount_x * c - mount.mount_y * s),
            y=robot_position.y + (mount.mount_x * s + mount.mount_y * c),
        )
        raw_tag_positions.append(tag_world)

        corrected_position, corrected_yaw = geometry.apply_mount_offset(
            tag_world, yaw_rad, mount
        )
        assert corrected_position.x == pytest.approx(robot_position.x, abs=1e-9)
        assert corrected_position.y == pytest.approx(robot_position.y, abs=1e-9)
        assert corrected_yaw == pytest.approx(yaw_rad)

    # The raw tag position really does sweep a circle of the expected
    # radius around the (fixed) corrected position -- not just "changes".
    for tag_world in raw_tag_positions:
        assert math.hypot(
            tag_world.x - robot_position.x, tag_world.y - robot_position.y
        ) == pytest.approx(radius)


def test_uc12_s4_rotated_mount_reports_the_robots_true_heading_not_the_tags():
    """UC-12.S-4, verbatim: a tag mounted rotated 90 degrees
    (``mount_yaw_rad`` = 90 deg) on a robot driving east reports corrected
    heading 0 (east), not 90 degrees.

    The tag is rigidly rotated 90 degrees CCW from the chassis's forward
    direction, so while the robot's true world heading is 0 (east), the
    tag's own *observed* world yaw is ``0 + 90 deg = 90 deg`` (north) --
    "the 90 degrees the tag itself presents" (UC-12.S-4's own words).
    ``apply_mount_offset`` must recover the robot's true heading (0, east)
    from the observed 90 degrees and the known 90-degree mount yaw:
    ``robot_yaw = observed_yaw - mount_yaw = 90 - 90 = 0``.

    This is also the sign check for this module's own ticket text, which
    describes the yaw step as "add mount_yaw to the observed yaw_rad" --
    literally applied, that would give ``90 + 90 = 180`` (west), which is
    neither the robot's true heading nor the tag's presented one, and
    fails this exact scenario from the same use case. Subtraction is the
    only formula consistent with UC-12.S-4's stated result (see
    ``apply_mount_offset``'s docstring for the full derivation and the
    matching v1 precedent).
    """
    mount = MountParameters(mount_x=0.0, mount_y=0.0, mount_yaw_rad=math.radians(90.0))
    observed_yaw_rad = math.radians(90.0)  # "the 90 degrees the tag itself presents"
    field_position = WorldPoint(x=10.0, y=-5.0)  # position is irrelevant to this check

    _robot_position, robot_yaw_rad = geometry.apply_mount_offset(
        field_position, observed_yaw_rad, mount
    )

    assert robot_yaw_rad == pytest.approx(0.0, abs=1e-9)  # east, the robot's heading


def test_offset_rotates_by_the_robots_derived_yaw_not_the_tags_observed_yaw():
    """The seam this ticket's Definition of Done calls out explicitly:
    the mount offset must rotate with the robot's own (derived) yaw, not
    the tag's raw observed yaw -- the two only coincide when
    ``mount_yaw_rad`` is zero.

    Set up a robot at reference point (0, 0), facing due east
    (robot_yaw = 0), with a tag mounted 8 cm forward / 3 cm left of
    centre (mount_x=8, mount_y=3) AND rotated 90 degrees from the
    chassis's forward (mount_yaw_rad = 90 deg). Rotating (8, 3) by the
    robot's *own* yaw (0 degrees, i.e. no rotation) places the tag at
    world (0, 0) + (8, 3) = (8, 3); its observed yaw is
    ``robot_yaw + mount_yaw = 0 + 90 = 90`` degrees.

    Feeding that observed tag pose back through ``apply_mount_offset``
    must recover the robot's true reference point (0, 0) exactly. Had the
    implementation instead rotated the offset by the tag's *observed* yaw
    (90 degrees) rather than the derived robot yaw (0 degrees), it would
    compute offset_world = (8*cos90 - 3*sin90, 8*sin90 + 3*cos90)
    = (-3, 8), giving a wrong reference point of (8 - (-3), 3 - 8) =
    (11, -5) instead of (0, 0).
    """
    mount = MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=math.radians(90.0))
    tag_world = WorldPoint(x=8.0, y=3.0)
    observed_yaw_rad = math.radians(90.0)

    robot_position, robot_yaw_rad = geometry.apply_mount_offset(
        tag_world, observed_yaw_rad, mount
    )

    assert robot_position.x == pytest.approx(0.0, abs=1e-9)
    assert robot_position.y == pytest.approx(0.0, abs=1e-9)
    assert robot_yaw_rad == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# correct_tag_pose (ticket 025-003, UC-12): the full composed correction
# ---------------------------------------------------------------------------


def test_correct_tag_pose_identity_with_zero_mount_and_zero_height():
    """Full identity case: a tag with zero mount offset and zero mount
    height reproduces the input position (and yaw) unchanged, regardless
    of camera geometry -- there is nothing for either step to correct."""
    tag_world = WorldPoint(x=17.0, y=-6.5)
    yaw_rad = math.radians(123.0)

    robot_position, robot_yaw_rad = geometry.correct_tag_pose(
        tag_world,
        yaw_rad,
        MountParameters(),
        camera_ground_position=WorldPoint(x=999.0, y=-999.0),
        camera_height_cm=250.0,
    )

    assert robot_position == tag_world
    assert robot_yaw_rad == pytest.approx(yaw_rad)


def test_correct_tag_pose_composes_parallax_then_offset_and_yaw():
    """The two steps compose in order: parallax first (pulling the
    apparent position to the true ground footprint), then offset/yaw
    (translating that footprint to the robot's reference point and
    deriving its heading) -- hand-computed end to end.

    Camera at (0, 0), 100 cm up. A robot at reference point (0, 0) faces
    east (robot_yaw = 0), with a tag mounted 8 cm forward / 3 cm left of
    centre (mount_x=8, mount_y=3) and rotated 90 degrees from the
    chassis's forward direction (mount_yaw_rad = 90 deg), 20 cm above the
    field (mount_z = 20).

    The tag's true ground footprint, ignoring elevation, is the mount
    offset rotated by the robot's own 0-degree yaw and added to the
    robot's position: ``(0, 0) + (8, 3) = (8, 3)``. Because it actually
    sits 20 cm up, a homography (which assumes z=0) reports it further
    from the camera by the parallax ratio ``t = 100 / (100 - 20) = 1.25``:
    ``apparent = (0, 0) + 1.25 * (8, 3) = (10, 3.75)``. Its observed
    world yaw is ``robot_yaw + mount_yaw = 0 + 90 = 90`` degrees.

    Feeding that apparent position (10, 3.75) and the 90-degree observed
    yaw through ``correct_tag_pose`` must recover the robot's true
    reference point (0, 0) and true heading (0, east): parallax first
    pulls (10, 3.75) back to the true footprint (8, 3), then offset/yaw
    composition subtracts the mount offset (rotated by the derived
    robot_yaw = 0) to land back on (0, 0).
    """
    camera_ground_position = WorldPoint(x=0.0, y=0.0)
    apparent = WorldPoint(x=10.0, y=3.75)
    mount = MountParameters(
        mount_x=8.0, mount_y=3.0, mount_z=20.0, mount_yaw_rad=math.radians(90.0)
    )
    observed_yaw_rad = math.radians(90.0)

    robot_position, robot_yaw_rad = geometry.correct_tag_pose(
        apparent,
        observed_yaw_rad,
        mount,
        camera_ground_position=camera_ground_position,
        camera_height_cm=100.0,
    )

    assert robot_position.x == pytest.approx(0.0, abs=1e-9)
    assert robot_position.y == pytest.approx(0.0, abs=1e-9)
    assert robot_yaw_rad == pytest.approx(0.0, abs=1e-9)
