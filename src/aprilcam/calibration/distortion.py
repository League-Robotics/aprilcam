"""Point-level lens distortion round trip (sprint 030 ticket 001,
split out of ``geometry.py`` in sprint 036 ticket 005).

``undistort_point``/``distort_point`` below are the only genuinely
``cv2``-dependent point-level math in the ``calibration`` package
(``cv2.undistortPoints``). They used to live in ``geometry.py``
alongside every other pure-math function in that module, but that put
a top-level ``import cv2 as cv`` in a file two outside packages
(``client/renderer.py``, ``cli/locate.py``) import for genuinely
``cv2``-free functions (``world_to_deskewed_pixel``,
``solve_camera_geometry``) -- making both of those importers
transitively ``cv2``-dependent for no reason of their own. Splitting
the two distortion functions into this sibling module makes "does
importing this module require ``cv2``" a structural fact checkable by
reading ``geometry.py``'s own imports, rather than a behavioral
property of which functions happen to get called. See this sprint's
``design/calibration-DESIGN.diff.md`` (Decision: "Splitting
``undistort_point``/``distort_point`` into ``distortion.py``") for the
full Design Rationale, including the two alternatives considered and
rejected.

Consumed only by ``calibration.py`` (``Calibration.to_world``/
``to_pixel``, and the homography-solve undistort step) -- no
client-side or ``cli/`` consumer needs lens distortion directly, so
this module introduces no new outside dependency of its own.
"""

from __future__ import annotations

import numpy as np

import cv2 as cv

from aprilcam.types import PixelPoint

__all__ = [
    "undistort_point",
    "distort_point",
]

#: :func:`distort_point`'s fixed-point iteration cap -- generous for a
#: function only ever called on a handful of points per use (a query's
#: single world point, or ``Deskewer``'s 4 corners), never per-frame at
#: scale, so a large cap costs nothing in practice. Mild distortion
#: (a few percent correction, most of the frame near the principal
#: point) converges in a handful of iterations; a strong, wide-angle-
#: lens-style fit (``|k1|`` around 0.5, a large-radius off-centre
#: point) measurably slower -- around 55 iterations for the
#: regression case ``test_distortion.py``'s solve-order test exercises
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
    never OpenCV's normalized coordinates" convention every function in
    ``geometry.py`` follows (:func:`~aprilcam.calibration.geometry.
    pixel_to_world`/:func:`~aprilcam.calibration.geometry.world_to_pixel`
    and the deskew/mount-correction functions there all take and return
    pixel or world points).

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
