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

__all__ = ["apply_homography", "invert_homography", "pixel_to_world", "world_to_pixel"]


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
