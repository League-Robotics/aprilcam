"""Pure pixel <-> world conversion math backing ``Calibration.to_world``/
``to_pixel`` (ticket 022-003).

No I/O, no cv2 detector objects, no camera access, no homography
*solving* -- only NumPy linear algebra applied to an already-solved
homography. Solving the homography, matching detected ids to surveyed
positions, and constructing ``CalibrationResult``/``Calibration`` are
``calibration.py``'s job; the fixed-bounding-box deskew warp is ticket
005's ``deskew.py``. This module is the "point-conversion half" ticket
003's own scope note asks for.

A homography here always maps pixel ``(u, v, 1)`` to world
``(x, y, w)`` in homogeneous coordinates::

    [x, y, w]^T = H @ [u, v, 1]^T

with ``(x/w, y/w)`` the world point, in cm.

**No origin-shift code lives here, deliberately.** ``design.md`` §6 /
``specification.md`` §7 are explicit that the world frame's origin shift
must be a pure subtraction on both axes, never a y-flip -- getting that
wrong is called out as the single most expensive class of bug in this
project (a silent north/south mirror that still produces
plausible-looking numbers). This module sidesteps the hazard entirely
rather than getting it right by careful arithmetic: the homography
``calibration.py`` solves maps pixels directly onto a playfield
definition's real, already A1-centred surveyed positions
(``config.loader.PlayfieldDefinition.aruco_position``) -- there is no
intermediate corner-relative coordinate system here to subtract from or
flip. The correspondences fed into the solve already encode "east is
positive x, north is positive y, origin at AprilTag 1" because that is
literally what the surveyed JSON says; this module's functions apply
that already-correct mapping and invent no new coordinate system of
their own. This is also what keeps the module free of v1's
upper-left-origin corner table, which ``specification.md`` §6 names
explicitly as the foot-gun superseded by playfield-definition-driven
calibration.

Also home to the deskew-geometry functions (ticket 022-005,
``deskewed_output_size``/``world_to_deskewed_pixel``) and the UC-12
mount-correction functions (ticket 025-003,
``project_tag_to_field_plane``/``apply_mount_offset``/
``correct_tag_pose``) -- same "pure function, no daemon state, no I/O"
shape, just a different pair of conversions (deskewed-pixel<->world,
tag-pose->robot-pose) than the pixel<->world pair above.

**Lens distortion (sprint 030 ticket 001):** ``undistort_point``/
``distort_point`` below are the point-level half of Design Rationale
Decision 1 -- v2 undistorts/redistorts single pixels at the two places
that need it (``calibration.calibration.Calibration.to_world``/
``to_pixel``, and the homography solve inside ``Calibrator.calibrate``),
never a whole captured frame the way v1 did
(``archive/src/aprilcam/stream.py:433``). This keeps every line of
distortion-aware code inside this module and ``calibration.py`` --
``vision/``, ``daemon/detection.py``, and ``calibration/deskew.py`` need
no changes, since they only ever call the still-pure ``to_world``/
``to_pixel`` methods, whose *internals* now optionally undistort/
redistort but whose signatures are unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence, Tuple

import cv2 as cv
import numpy as np

from aprilcam.types import MountParameters, PixelPoint, WorldPoint

__all__ = [
    "apply_homography",
    "invert_homography",
    "pixel_to_world",
    "world_to_pixel",
    "undistort_point",
    "distort_point",
    "deskewed_output_size",
    "world_to_deskewed_pixel",
    "project_tag_to_field_plane",
    "apply_mount_offset",
    "correct_tag_pose",
    "ReferenceObservation",
    "CameraGeometrySolution",
    "solve_camera_geometry",
]


def apply_homography(matrix: np.ndarray, x: float, y: float) -> Tuple[float, float]:
    """Map one 2D point through a 3x3 homogeneous transform *matrix*.

    ``[x', y', w]^T = matrix @ [x, y, 1]^T``, returned as ``(x'/w,
    y'/w)``. Generic over direction -- pixel->world with a solved
    homography, or world->pixel with its inverse (:func:`invert_homography`)
    -- the caller decides which matrix to pass; this function does not
    know or care which space *x*/*y* are in.
    """
    vec = matrix @ np.array([x, y, 1.0], dtype=np.float64)
    w = float(vec[2])
    if w == 0.0:
        raise ValueError(
            "homogeneous transform produced w=0 (point mapped to infinity)"
        )
    return (float(vec[0] / w), float(vec[1] / w))


def invert_homography(matrix: np.ndarray) -> np.ndarray:
    """The inverse of a pixel->world homography, i.e. world->pixel.

    Raises ``numpy.linalg.LinAlgError`` for a singular matrix. Callers
    holding a real ``calibration.calibration.Calibration`` should never
    see this in practice: ``calibration.py`` rejects a singular or
    near-degenerate homography *before* ever constructing one
    (degenerate-geometry rejection, UC-04's "reject rather than store a
    bad homography"). This function does not duplicate that check -- it
    stays a pure, honest inversion, exactly as risky as the matrix it is
    handed.
    """
    return np.linalg.inv(matrix)


def pixel_to_world(homography: np.ndarray, pixel: PixelPoint) -> WorldPoint:
    """``Calibration.to_world``'s math: map one pixel through an
    already-solved pixel->world homography, in cm.

    A1-centred, +x east, +y north -- purely a property of what world
    coordinates *homography* was solved against (``calibration.py``'s
    job), never adjusted or re-derived here (see the module docstring).
    """
    x, y = apply_homography(homography, pixel.x, pixel.y)
    return WorldPoint(x=x, y=y)


def world_to_pixel(homography: np.ndarray, world: WorldPoint) -> PixelPoint:
    """``Calibration.to_pixel``'s math: the inverse of
    :func:`pixel_to_world`, via *homography*'s inverse."""
    inverse = invert_homography(homography)
    x, y = apply_homography(inverse, world.x, world.y)
    return PixelPoint(x=x, y=y)


# ---------------------------------------------------------------------------
# Lens distortion (sprint 030 ticket 001): point-level undistort/redistort.
# See the module docstring's "Lens distortion" section for why this is
# point-level, confined to this module and calibration.py, and never a
# whole-frame undistort.
# ---------------------------------------------------------------------------

#: :func:`distort_point`'s fixed-point iteration cap -- generous for a
#: function only ever called on a handful of points per use (a query's
#: single world point, or ``Deskewer``'s 4 corners), never per-frame at
#: scale, so a large cap costs nothing in practice. Mild distortion
#: (a few percent correction, most of the frame near the principal
#: point) converges in a handful of iterations; a strong, wide-angle-
#: lens-style fit (``|k1|`` around 0.5, a large-radius off-centre
#: point) measurably slower -- around 55 iterations for the
#: regression case ``test_calibration.py``'s solve-order test exercises
#: -- because the plain ``guess += residual`` update is a Picard
#: iteration whose convergence *rate*, not just whether it converges,
#: degrades as the distortion strengthens (see the function's own
#: "Convergence assumption" docstring section). This cap gives several
#: multiples of headroom above that measured worst case.
_DISTORT_POINT_MAX_ITERATIONS = 200

#: :func:`distort_point` stops iterating once its current guess's
#: undistorted image lands within this many pixels of the true target --
#: far tighter than any calibration or detection noise floor in this
#: project (:data:`~aprilcam.calibration.calibration.DRIFT_TOLERANCE_CM`
#: is 2 cm, orders of magnitude coarser once converted through any
#: realistic px-per-cm scale), so this tolerance is never the limiting
#: source of error in a distortion-aware round trip.
_DISTORT_POINT_TOLERANCE_PX = 1e-6


def undistort_point(
    pixel: PixelPoint, camera_matrix: np.ndarray, dist_coeffs: np.ndarray
) -> PixelPoint:
    """Remove lens distortion from one *pixel*, reprojected back into
    **pixel space** (not OpenCV's intermediate normalized-image-plane
    coordinates) via *camera_matrix* passed as ``cv2.undistortPoints``'s
    own ``P`` argument -- matching the "always pixel or world space,
    never OpenCV's normalized coordinates" convention every other
    function in this module follows (:func:`pixel_to_world`/
    :func:`world_to_pixel` and the deskew/mount-correction functions
    below all take and return pixel or world points).

    A thin wrapper: ``cv2.undistortPoints`` already implements the
    (OpenCV-internal, iterative) inverse of a lens's forward distortion
    model -- this function adds no numerics of its own beyond the
    single-point ``(1, 1, 2)`` array marshalling ``cv2.undistortPoints``
    expects. *camera_matrix* is a 3x3 array, *dist_coeffs* whatever
    length OpenCV's distortion model accepts (4, 5, 8, 12, or 14
    coefficients; this project stores the standard 5,
    ``(k1, k2, p1, p2, k3)``, per :func:`cv2.calibrateCamera`'s own
    default).

    See :func:`distort_point` for this function's own inverse --
    needed because OpenCV has no ``distortPoints`` counterpart.
    """
    src = np.array([[[float(pixel.x), float(pixel.y)]]], dtype=np.float64)
    dst = cv.undistortPoints(src, camera_matrix, dist_coeffs, P=camera_matrix)
    x, y = dst[0, 0]
    return PixelPoint(x=float(x), y=float(y))


def distort_point(
    pixel: PixelPoint, camera_matrix: np.ndarray, dist_coeffs: np.ndarray
) -> PixelPoint:
    """Re-apply lens distortion to one already-undistorted *pixel* --
    the inverse of :func:`undistort_point`. OpenCV has no
    ``distortPoints`` counterpart to ``undistortPoints``, so this
    function finds it with a short fixed-point iteration seeded at
    *pixel* itself: repeatedly undistort the current guess and correct
    it by the residual between the guess's undistorted image and the
    true target::

        guess_0 = pixel
        guess_{k+1} = guess_k + (pixel - undistort_point(guess_k))

    stopping once ``undistort_point(guess)`` reproduces *pixel* to
    within :data:`_DISTORT_POINT_TOLERANCE_PX`, or after
    :data:`_DISTORT_POINT_MAX_ITERATIONS` iterations, whichever comes
    first.

    **Convergence assumption**: this is a Picard/fixed-point iteration,
    not a globally-convergent solve -- it converges whenever
    :func:`undistort_point` behaves as a contraction mapping near the
    fixed point, true for the radial/tangential distortion an ordinary
    camera lens produces, which covers every camera this project
    targets (including a deliberately strong, wide-angle-style fit --
    see :data:`_DISTORT_POINT_MAX_ITERATIONS`'s own docstring for the
    measured iteration count that case needs). The convergence *rate*
    is not uniform, though: it is fast (a handful of iterations) near
    the principal point and for mild distortion, and measurably slower
    -- tens of iterations, still well inside the cap -- for a point far
    off-centre under strong distortion, since the plain ``guess +=
    residual`` update implicitly assumes the local Jacobian is close to
    identity, an assumption that weakens as the correction grows. It is
    not guaranteed to converge at all for pathological, large-magnitude
    distortion coefficients (well outside what a real lens fit ever
    produces, and outside the sanity bound ticket 002's intrinsics
    estimation enforces on any coefficients it stores).

    Deliberately expressed in terms of :func:`undistort_point` itself
    -- whatever ``cv2.undistortPoints`` actually computes for the given
    *dist_coeffs*, rational/tangential/thin-prism terms included --
    rather than re-deriving OpenCV's forward distortion polynomial by
    hand. This guarantees ``undistort_point(distort_point(p)) == p`` by
    construction, to iteration tolerance, for whatever *dist_coeffs*
    shape is in play, rather than risking a hand-derived forward
    formula silently diverging from what ``cv2.undistortPoints`` itself
    implements.

    Only ever called on a handful of points per use (one query point at
    a time from :meth:`~aprilcam.calibration.calibration.Calibration.to_pixel`,
    or ``Deskewer``'s 4 corners) -- never per-frame at scale, so a
    handful of extra ``cv2.undistortPoints`` calls per invocation is an
    acceptable cost.
    """
    target = np.array([float(pixel.x), float(pixel.y)], dtype=np.float64)
    guess = target.copy()
    for _ in range(_DISTORT_POINT_MAX_ITERATIONS):
        undistorted = undistort_point(
            PixelPoint(x=float(guess[0]), y=float(guess[1])), camera_matrix, dist_coeffs
        )
        residual = target - np.array([undistorted.x, undistorted.y], dtype=np.float64)
        guess = guess + residual
        if float(np.hypot(residual[0], residual[1])) < _DISTORT_POINT_TOLERANCE_PX:
            break
    return PixelPoint(x=float(guess[0]), y=float(guess[1]))


# ---------------------------------------------------------------------------
# Deskew geometry (ticket 022-005): the fixed-bounding-box sizing rule and
# the "anchor invariant" world<->deskewed-pixel mapping. Distinct from
# pixel_to_world/world_to_pixel above -- those convert between a raw
# camera's pixel space and world space via a *solved homography*.
# world_to_deskewed_pixel below converts between world space and a
# *deskewed image*'s pixel space via nothing but that image's own
# published px_per_cm/world_extent metadata (ImageFrame's docstring,
# design.md Sec.6) -- no homography, no Calibration object, because a
# client holding only a deskewed frame's metadata has neither.
# ---------------------------------------------------------------------------

#: The fixed bounding box's default side length, in pixels
#: (specification.md Sec.4.1: "default 800x800, overridable per
#: playfield"). The field rectangle is scaled aspect-preserved to fit
#: this square -- ``main-playfield`` (134.3 x 89.3 cm) yields 800 x 532.
DEFAULT_MAX_DIMENSION_PX = 800


def deskewed_output_size(
    width_cm: float, height_cm: float, max_dimension_px: int = DEFAULT_MAX_DIMENSION_PX
) -> Tuple[int, int]:
    """The deskewed output image's ``(width_px, height_px)`` for a field
    *width_cm* x *height_cm*, aspect-preserved into a
    *max_dimension_px* x *max_dimension_px* bounding box.

    Uniform-scale by construction: one ``scale`` (pixels per cm) is
    computed from whichever field dimension is larger, then applied to
    both -- the output is never stretched non-uniformly. ``round()`` on
    each dimension independently means the two output dimensions may not
    individually satisfy ``dim_px / dim_cm == scale`` to the last bit
    once rounded (a non-square field's shorter dimension can land up to
    half a pixel off the exact scale) -- an accepted, inherent
    consequence of publishing one scalar px-per-cm for a two-dimensional
    box, not a bug (see :func:`world_to_deskewed_pixel`'s docstring).
    """
    if width_cm <= 0 or height_cm <= 0:
        raise ValueError(
            f"width_cm/height_cm must be positive, got {width_cm}x{height_cm}"
        )
    if max_dimension_px <= 0:
        raise ValueError(f"max_dimension_px must be positive, got {max_dimension_px}")

    scale = max_dimension_px / max(width_cm, height_cm)
    out_w = max(1, round(width_cm * scale))
    out_h = max(1, round(height_cm * scale))
    return out_w, out_h


def world_to_deskewed_pixel(
    world: WorldPoint, *, world_extent: Tuple[float, float], px_per_cm: float
) -> PixelPoint:
    """The anchor invariant (``types.ImageFrame``'s docstring, design.md
    Sec.6): a deskewed frame spans *exactly* the field rectangle -- image
    ``(0, 0)`` is the field's north-west corner, world ``(-width/2,
    +height/2)``. North is up, east is right; world -> pixel flips y
    **exactly once**, right here, and nowhere else.

    *px_per_cm* is always the frame's own **published, derived** value
    (``frame_width / field_width_cm`` -- never a chosen constant); this
    function performs no scaling decision of its own, only the anchor
    shift plus that one multiply-and-flip. The single source of truth
    both ``calibration/deskew.py`` (building the warp's destination
    quad) and ``client/renderer.py`` (placing a tag's drawn boundary)
    share, so the sign only has to be right in one place.
    """
    width_cm, height_cm = world_extent
    x = (world.x + width_cm / 2.0) * px_per_cm
    y = (height_cm / 2.0 - world.y) * px_per_cm
    return PixelPoint(x=x, y=y)


# ---------------------------------------------------------------------------
# Mount correction (ticket 025-003, UC-12): turn an observed *tag* pose into
# the *robot's* pose. Two independent steps, composed by correct_tag_pose --
# see that function's docstring for the worked example proving the seam
# between the world frame (+x east, +y north, atan2-based yaw, CCW positive)
# and the robot's own mount frame (+x forward, +y left, rotating with
# mount_yaw) is correctly ordered.
#
# **Camera-geometry caveat -- resolved (sprint 029 ticket 001).**
# project_tag_to_field_plane needs the camera's own ground position and
# height to do its projection -- this was genuinely new math when this
# module was written (see the ticket that requested it: "there is no
# isolated, portable parallax function in the archive to lift directly"),
# derived fresh from optics rather than ported. At the time, nothing in
# the v2 codebase modelled that camera geometry: `calibration.interfaces.
# Calibration` carried only `stale`, `calibrated_at`, `playfield`, and
# the solved homography -- no camera position or height field, even
# though `docs/design/design.md` Sec.7 and `specification.md` Sec.5
# already mentioned a `camera_position` key inside `config.json` in
# their storage-layout prose.
#
# That gap is now closed: `config.interfaces.CameraPosition` (`nadir:
# WorldPoint`, `height_cm: float`) is a real, optional `CameraConfig`
# field, plumbed through `config/loader.py`, `proto/aprilcam_v2.proto`,
# `daemon/rpc.py`, and `client/daemon_client.py`/`client/models.py`
# exactly like every other camera setting (set by hand, or solved by
# `aprilcam camera locate`, sprint 029 ticket 003). `daemon.service.
# DaemonService._camera_geometry_for` reads it directly and is what a
# real daemon process wires in as `camera_geometry_lookup`; a camera
# nobody has run `locate` against (or been hand-authored a
# `camera_position` for) still falls back to the exact pre-029 stopgap
# -- ground position `(0, 0)`, height `0.0`, this module's own
# documented "nothing to correct" sentinel. This module itself is
# unaffected either way: `camera_ground_position`/`camera_height_cm`
# remain plain function parameters, supplied by whichever caller has a
# real geometry source (or, in tests, by hand), which keeps this module
# pure and fully testable independent of where its inputs come from.
# ---------------------------------------------------------------------------


def project_tag_to_field_plane(
    tag_world: WorldPoint,
    *,
    mount_z_cm: float,
    camera_ground_position: WorldPoint,
    camera_height_cm: float,
) -> WorldPoint:
    """Parallax: project an elevated tag's apparent world position down
    onto the field plane (ground, z=0), given its mount height and the
    camera's own ground position and height.

    *tag_world* is what a homography reports for the tag's detected pixel
    -- i.e. the point where the camera ray through that pixel crosses the
    z=0 plane, which is only the tag's true ground footprint when the tag
    actually sits *on* the field. A tag mounted *mount_z_cm* above the
    field is seen along a ray that would reach the ground further from the
    camera than the tag's true footprint (basic similar-triangles optics:
    a ray from the camera at height ``camera_height_cm`` through a point
    at height ``mount_z_cm`` continues past that point to the ground,
    displaced away from the camera's own ground position in proportion to
    ``mount_z_cm / camera_height_cm``). Correcting for this pulls the
    apparent position back *toward* the camera's ground position by that
    same ratio::

        ratio = mount_z_cm / camera_height_cm
        corrected = tag_world + ratio * (camera_ground_position - tag_world)

    Worked example (see the test suite for the full hand-computed case):
    camera ground position ``(0, 0)``, camera height 100 cm, a tag truly
    footed at ``(40, 30)`` but mounted 20 cm up reports an apparent
    position of ``(50, 37.5)`` (ratio ``100/(100-20) = 1.25`` extrapolated
    from the camera through the tag to the ground); this function inverts
    that with ``ratio = 20/100 = 0.2``, recovering ``(40, 30)`` exactly.

    Returns *tag_world* **unchanged** (the identity/no-op case) when
    *mount_z_cm* or *camera_height_cm* is not strictly positive -- no
    elevation to correct for, or no known camera height to correct
    against. This function trusts its inputs: rejecting implausible
    values (a tag mounted at or above the camera's own height) is a
    registration-time / service-layer concern (UC-12's failure modes),
    not this pure function's job.
    """
    if mount_z_cm <= 0.0 or camera_height_cm <= 0.0:
        return tag_world
    ratio = mount_z_cm / camera_height_cm
    x = tag_world.x + ratio * (camera_ground_position.x - tag_world.x)
    y = tag_world.y + ratio * (camera_ground_position.y - tag_world.y)
    return WorldPoint(x=x, y=y)


def apply_mount_offset(
    field_position: WorldPoint, yaw_rad: float, mount: MountParameters
) -> Tuple[WorldPoint, float]:
    """Offset + yaw composition: turn a (parallax-corrected) tag pose into
    the robot's reference-point pose, per *mount*'s ``mount_x``/
    ``mount_y``/``mount_yaw_rad`` (``types.MountParameters``'s frame
    convention -- the robot's own frame, +x forward, +y left, rotating
    with ``mount_yaw_rad``; see that dataclass's docstring).

    **Order matters and is the seam most likely to have a sign error**
    (this ticket's own Definition of Done calls it out): the robot's
    world yaw must be derived *first*, then used to rotate the mount
    offset into the world frame -- not the other way around.

    1. ``mount_yaw_rad`` is the tag's fixed rotation relative to the
       robot's forward direction (UC-12's own table), in the same
       CCW-positive sense as every other angle in this project. The tag
       and the robot are one rigid body, so as the robot turns in place
       both the tag's world-frame facing (*yaw_rad*, observed) and the
       robot's own world-frame heading turn together, offset from each
       other by the *constant* ``mount_yaw_rad``::

           yaw_rad (observed, world) == robot_yaw_rad + mount_yaw_rad

       so the robot's world yaw is the observed yaw *minus* the mount
       yaw::

           robot_yaw_rad = yaw_rad - mount.mount_yaw_rad

    2. ``mount_x``/``mount_y`` are the tag's position in the robot's own
       (forward, left) frame -- fixed relative to the chassis, and so
       rotating with the **robot's** world yaw (just derived), never with
       the tag's own observed yaw directly (those two only coincide when
       ``mount_yaw_rad`` is zero). +x forward, +y left is a standard
       right-handed frame consistent with the world frame's own CCW
       convention (facing east, "left" is north -- 90 degrees CCW from
       forward, exactly like the world frame's east-to-north rotation),
       so the same rotation matrix applies::

           c, s = cos(robot_yaw_rad), sin(robot_yaw_rad)
           offset_world = (mount_x*c - mount_y*s, mount_x*s + mount_y*c)

       The tag sits at ``robot_position + offset_world``, so::

           robot_position = field_position - offset_world

    **Worked example** (UC-12.S-4, the rotated-mount scenario): a tag is
    mounted turned 90 degrees from the robot's forward direction
    (``mount_yaw_rad = pi/2``) on a robot driving due east. The robot's
    true world yaw is 0 (east). Because the tag is rigidly rotated 90
    degrees CCW from the chassis's forward, the tag's own observed world
    yaw is ``0 + pi/2 = pi/2`` (facing north) -- "the 90 degrees the tag
    itself presents". Composing: ``robot_yaw_rad = pi/2 - pi/2 = 0``,
    i.e. east, the robot's true heading -- not the tag's 90 degrees. If
    this function instead *added* ``mount_yaw_rad`` (matching this
    module's own ticket's prose, which says "add"), the result here would
    be ``pi/2 + pi/2 = pi`` (west) -- neither the tag's presented heading
    nor the robot's true one. Subtraction is the only formula that
    reproduces UC-12.S-4's stated result, matches v1's proven
    implementation (``archive/src/aprilcam/daemon/camera_pipeline.py``'s
    ``_robot_centre_from_tag``, which derives an identical ``robot_yaw =
    tag_yaw - mount_yaw`` and rotates the offset by that *derived*
    ``robot_yaw`` rather than the raw tag yaw), and is what the test suite
    (:mod:`tests.aprilcam.calibration.test_geometry`) checks explicitly.

    Returns ``(robot_position, robot_yaw_rad)``.
    """
    robot_yaw_rad = yaw_rad - mount.mount_yaw_rad
    cos_r = math.cos(robot_yaw_rad)
    sin_r = math.sin(robot_yaw_rad)
    offset_x = mount.mount_x * cos_r - mount.mount_y * sin_r
    offset_y = mount.mount_x * sin_r + mount.mount_y * cos_r
    robot_position = WorldPoint(
        x=field_position.x - offset_x, y=field_position.y - offset_y
    )
    return robot_position, robot_yaw_rad


def correct_tag_pose(
    tag_world: WorldPoint,
    yaw_rad: float,
    mount: MountParameters,
    *,
    camera_ground_position: WorldPoint,
    camera_height_cm: float,
) -> Tuple[WorldPoint, float]:
    """The full UC-12 correction: parallax (:func:`project_tag_to_field_plane`,
    via ``mount.mount_z``) followed by offset/yaw composition
    (:func:`apply_mount_offset`, via ``mount.mount_x``/``mount_y``/
    ``mount_yaw_rad``) -- turning an observed tag pose into the robot's.

    A zero-everywhere *mount* (the ``MountParameters`` default) is the
    identity: no elevation to project and no offset/yaw to apply, so
    ``correct_tag_pose(w, yaw, MountParameters(), ...) == (w, yaw)``
    exactly, regardless of *camera_ground_position*/*camera_height_cm*.

    See the two composed functions' own docstrings for the worked
    examples proving each step's sign convention; this function performs
    no math of its own beyond calling them in order.
    """
    field_position = project_tag_to_field_plane(
        tag_world,
        mount_z_cm=mount.mount_z,
        camera_ground_position=camera_ground_position,
        camera_height_cm=camera_height_cm,
    )
    return apply_mount_offset(field_position, yaw_rad, mount)


# ---------------------------------------------------------------------------
# Camera-geometry solver (sprint 029 ticket 002, SUC-003): the inverse of
# project_tag_to_field_plane -- given two or more elevated references at
# known surveyed positions, recover the camera's own ground nadir and
# optical height. Consumed by ``cli/locate.py`` (ticket 003, ``aprilcam
# camera locate``), which supplies the observations; this module still
# talks to no camera, no daemon, no cv2.
#
# **Why least squares over all references, never line intersection.** A
# first attempt at this solve intersected the two displacement lines
# (surveyed -> observed, extended) and recovered a nadir ~20 cm off, with a
# residual 150x worse than optimum. The cause: camera nadir C, a
# reference's true footprint T, and its apparent (uncorrected) observed
# position A are collinear by construction (T = C + (true - C), A = C + k *
# (true - C) for the same k) -- this is exactly what parallax *is*, a ray
# from the camera continuing past the tag to the ground. Two references on
# opposite sides of C produce two displacement *lines* that are nearly
# coincident (same orientation, opposite sense), and intersecting them
# uses only *direction*, discarding *magnitude* -- wildly ill-conditioned.
# Least squares over the full 2D residual (magnitude and direction both)
# on the very same data is well-conditioned. See ``solve_camera_geometry``
# below for the linearization that makes this a single, non-iterative
# ``numpy.linalg.lstsq`` call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ReferenceObservation:
    """One elevated reference used to solve for a camera's geometry:
    a surveyed ground position, seen through some known stand height,
    reported (uncorrected) by ``GetTag`` at an apparent world position.

    ``surveyed`` and ``observed`` are both already-projected world
    points (``pixel_to_world``'s output, or a playfield definition's
    surveyed feature) -- this dataclass carries no pixels, exactly like
    every other value type in this module.
    """

    surveyed: WorldPoint
    observed: WorldPoint
    height_cm: float


@dataclass(frozen=True, kw_only=True)
class CameraGeometrySolution:
    """The result of :func:`solve_camera_geometry`: the camera's solved
    ground nadir and optical height, plus two numbers that make a bad
    solve visible rather than silent.

    ``rms_residual_cm`` refits ``observed`` from the solved ``(nadir,
    height_cm)`` for every reference via the forward relation (the same
    ``apparent = C + k * (true - C)`` this module's own
    :func:`project_tag_to_field_plane` inverts) and RMS's the 2D error
    against the references' actual ``observed`` values -- how well the
    solved geometry explains the data it was fit from.

    ``condition_number`` is the solved design matrix's largest-to-
    smallest singular-value ratio (``numpy.linalg.svd``) -- large means
    the references were too close to collinear-with-the-nadir to
    constrain the fit well, the exact failure mode line-intersection
    suffered from. A caller (``cli/locate.py``) uses this to warn or
    refuse to write an ill-conditioned or high-residual solve.
    """

    nadir: WorldPoint
    height_cm: float
    rms_residual_cm: float
    condition_number: float


def solve_camera_geometry(
    references: Sequence[ReferenceObservation],
) -> CameraGeometrySolution:
    """Solve a camera's ground nadir ``C`` and optical height ``H`` from
    two or more elevated *references* at known surveyed positions --
    the inverse problem :func:`project_tag_to_field_plane` assumes is
    already solved.

    **The math.** The forward relation (already proven correct and
    tested -- see :func:`project_tag_to_field_plane`'s own worked
    example; this function adds no new physics, only inverts an
    already-verified relation)::

        apparent = C + k * (true - C),   k = H / (H - h)

    is bilinear in the unknowns ``(C, H)`` -- direct least squares over
    ``(Cx, Cy, H)`` would be nonlinear. Linearize by substitution: let
    ``A = C / H`` (a 2-vector) and ``b = 1 / H`` (a scalar). Substituting
    and rearranging the equivalent reverse form (``project_tag_to_field_
    plane``'s own ``ratio = h / H`` correction, run backwards)::

        true = apparent + (h / H) * (C - apparent)
             = apparent + h*A - h*b*apparent
        =>  true - apparent = h*A - h*apparent*b

    which is **linear** in ``(Ax, Ay, b)``. Per reference this gives two
    equations::

        true_x - apparent_x = h * Ax - h * apparent_x * b
        true_y - apparent_y = h * Ay - h * apparent_y * b

    Stacking all references into a design matrix ``M`` (shape ``2n x
    3``) and right-hand side ``r`` (shape ``2n``), ``[Ax, Ay, b] =
    lstsq(M, r)`` recovers ``H = 1/b``, ``C = A * H`` -- one
    non-iterative call, no initial guess.

    **Worked/regression example** (measured live on a real camera, no
    mounts registered): tag 15 surveyed at ``(50, 30)`` cm on a 17.2 cm
    stand, observed (uncorrected) at ``(57.38, 34.90)``; tag 14 surveyed
    at ``(-50, -30)`` on a 15.0 cm stand, observed at ``(-57.15,
    -33.40)``. This function recovers nadir ``(3.06, -2.80)`` cm, height
    ``128.23`` cm, ``rms_residual_cm`` ``~0.16``, and
    ``condition_number`` ``~67.4`` -- well-conditioned despite the two
    references sitting diametrically opposite the nadir, because the fit
    uses the full 2D residual (magnitude and direction), unlike a
    line-intersection approach that uses direction alone and is wildly
    ill-conditioned on the very same data (see the module section header
    above). An independent nonlinear grid search over ``(Cx, Cy, H)`` on
    the same data agrees to within ~0.5 mm, by a completely different
    method. Applying this solved geometry via
    :func:`project_tag_to_field_plane` corrects the two references' raw
    ~8-9 cm errors down to ~0.2 cm; see the test suite for the full
    reproduction.

    Raises ``ValueError`` for fewer than 2 *references*, before any
    linear algebra runs -- two references is the minimum that makes
    ``M`` (4 equations) not underdetermined for 3 unknowns.
    """
    if len(references) < 2:
        raise ValueError(
            "solve_camera_geometry requires at least 2 references, got "
            f"{len(references)}"
        )

    rows = []
    rhs = []
    for reference in references:
        h = reference.height_cm
        true_x, true_y = reference.surveyed.x, reference.surveyed.y
        apparent_x, apparent_y = reference.observed.x, reference.observed.y
        rows.append([h, 0.0, -h * apparent_x])
        rhs.append(true_x - apparent_x)
        rows.append([0.0, h, -h * apparent_y])
        rhs.append(true_y - apparent_y)

    design_matrix = np.array(rows, dtype=np.float64)
    residual_vector = np.array(rhs, dtype=np.float64)

    solution, _residuals, _rank, singular_values = np.linalg.lstsq(
        design_matrix, residual_vector, rcond=None
    )
    a_x, a_y, b = (float(value) for value in solution)
    height_cm = 1.0 / b
    nadir = WorldPoint(x=a_x * height_cm, y=a_y * height_cm)
    condition_number = float(singular_values[0] / singular_values[-1])

    squared_errors = []
    for reference in references:
        k = height_cm / (height_cm - reference.height_cm)
        predicted_x = nadir.x + k * (reference.surveyed.x - nadir.x)
        predicted_y = nadir.y + k * (reference.surveyed.y - nadir.y)
        squared_errors.append((predicted_x - reference.observed.x) ** 2)
        squared_errors.append((predicted_y - reference.observed.y) ** 2)
    rms_residual_cm = float(np.sqrt(np.mean(squared_errors)))

    return CameraGeometrySolution(
        nadir=nadir,
        height_cm=height_cm,
        rms_residual_cm=rms_residual_cm,
        condition_number=condition_number,
    )
