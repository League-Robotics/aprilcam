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
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from aprilcam.types import PixelPoint, WorldPoint

__all__ = [
    "apply_homography",
    "invert_homography",
    "pixel_to_world",
    "world_to_pixel",
    "deskewed_output_size",
    "world_to_deskewed_pixel",
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
