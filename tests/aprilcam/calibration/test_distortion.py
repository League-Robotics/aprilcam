"""Tests for ``aprilcam.calibration.distortion`` (sprint 030 ticket 001;
moved here from ``test_geometry.py`` in sprint 036 ticket 005, alongside
the ``undistort_point``/``distort_point`` code itself -- see
``distortion.py``'s own module docstring for why the split happened).

Pure math tests: no camera, no vision, no ``Calibrator`` -- every camera
matrix/distortion-coefficient fixture here is hand-chosen.
"""

from __future__ import annotations

import math

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration import distortion
from aprilcam.types import PixelPoint

# ---------------------------------------------------------------------------
# undistort_point / distort_point (sprint 030 ticket 001)
# ---------------------------------------------------------------------------

#: A hand-chosen, plausible-for-a-webcam-lens intrinsic matrix/distortion
#: vector -- nothing pinned to a real camera, just a fixed, nontrivial
#: fixture every test below shares. ``(k1, k2, p1, p2, k3)``, OpenCV's
#: standard 5-coefficient order.
_CAMERA_MATRIX = np.array(
    [[800.0, 0.0, 320.0], [0.0, 800.0, 240.0], [0.0, 0.0, 1.0]], dtype=np.float64
)
_DIST_COEFFS = np.array([-0.25, 0.05, 0.001, -0.0005, 0.0], dtype=np.float64)
_ZERO_DIST_COEFFS = np.zeros(5, dtype=np.float64)


def test_undistort_point_matches_cv2_undistortpoints_directly():
    """A thin-wrapper test: :func:`distortion.undistort_point` must
    report exactly what ``cv2.undistortPoints(..., P=camera_matrix)``
    itself computes for the same input -- proving the marshalling
    (single-point ``(1, 1, 2)`` array, ``P=camera_matrix`` so the
    result reprojects into pixel space) is correct, not a
    reimplementation of OpenCV's own math."""
    pixel = PixelPoint(x=450.0, y=180.0)

    result = distortion.undistort_point(pixel, _CAMERA_MATRIX, _DIST_COEFFS)

    expected = cv.undistortPoints(
        np.array([[[pixel.x, pixel.y]]], dtype=np.float64),
        _CAMERA_MATRIX,
        _DIST_COEFFS,
        P=_CAMERA_MATRIX,
    )
    assert result.x == pytest.approx(float(expected[0, 0, 0]))
    assert result.y == pytest.approx(float(expected[0, 0, 1]))


def test_undistort_point_with_zero_distortion_is_the_identity():
    """No lens distortion -> undistorting is a no-op, in pixel space
    (thanks to ``P=camera_matrix``)."""
    pixel = PixelPoint(x=111.0, y=222.0)

    result = distortion.undistort_point(pixel, _CAMERA_MATRIX, _ZERO_DIST_COEFFS)

    assert result.x == pytest.approx(pixel.x, abs=1e-6)
    assert result.y == pytest.approx(pixel.y, abs=1e-6)


def test_undistort_point_moves_an_off_centre_pixel_away_from_the_principal_point():
    """Sanity check on the *sign* of the correction, independent of
    the ``cv2.undistortPoints`` call itself: OpenCV's forward model
    scales a normalized ideal point's radius by ``(1 + k1*r^2 + ...)``
    to get the *observed* (distorted) radius. This fixture's ``k1`` is
    negative and the normalized radius at this pixel is large enough
    (~0.44, a third of the frame's half-width in normalized units) for
    that factor to be comfortably below 1 -- so the observed/distorted
    point sits at a *smaller* radius than the true ideal ray, and
    undistorting (recovering the ideal ray from the observed point)
    must move it *away* from the principal point (320, 240), not
    toward it."""
    pixel = PixelPoint(x=620.0, y=420.0)  # well off-centre, lower-right

    result = distortion.undistort_point(pixel, _CAMERA_MATRIX, _DIST_COEFFS)

    cx, cy = 320.0, 240.0
    dist_before = math.hypot(pixel.x - cx, pixel.y - cy)
    dist_after = math.hypot(result.x - cx, result.y - cy)
    assert dist_after > dist_before


def test_distort_point_with_zero_distortion_is_the_identity():
    pixel = PixelPoint(x=111.0, y=222.0)

    result = distortion.distort_point(pixel, _CAMERA_MATRIX, _ZERO_DIST_COEFFS)

    assert result.x == pytest.approx(pixel.x, abs=1e-6)
    assert result.y == pytest.approx(pixel.y, abs=1e-6)


@pytest.mark.parametrize(
    "px, py",
    [
        (320.0, 240.0),  # principal point -- zero-radius, no correction either way
        (450.0, 180.0),
        (620.0, 420.0),
        (150.0, 400.0),
        (50.0, 50.0),  # far corner, largest correction this fixture produces
    ],
)
def test_undistort_point_and_distort_point_are_true_inverses(px, py):
    """The ticket's own convergence claim, checked directly:
    ``undistort_point(distort_point(p)) == p`` and
    ``distort_point(undistort_point(p)) == p``, to
    :data:`distortion._DISTORT_POINT_TOLERANCE_PX`-scale precision, for
    a fixed nontrivial lens model across several points spanning the
    image (including the principal point itself, where radial
    distortion is exactly zero -- the round trip must still hold
    there)."""
    original = PixelPoint(x=px, y=py)

    undistorted = distortion.undistort_point(original, _CAMERA_MATRIX, _DIST_COEFFS)
    recovered = distortion.distort_point(undistorted, _CAMERA_MATRIX, _DIST_COEFFS)
    assert recovered.x == pytest.approx(original.x, abs=1e-4)
    assert recovered.y == pytest.approx(original.y, abs=1e-4)

    distorted = distortion.distort_point(original, _CAMERA_MATRIX, _DIST_COEFFS)
    recovered_back = distortion.undistort_point(distorted, _CAMERA_MATRIX, _DIST_COEFFS)
    assert recovered_back.x == pytest.approx(original.x, abs=1e-4)
    assert recovered_back.y == pytest.approx(original.y, abs=1e-4)


def test_distort_point_converges_within_the_documented_iteration_cap():
    """:func:`distortion.distort_point`'s own fixed-point loop must not
    silently run past its documented cap without converging, for
    ordinary (non-pathological) distortion."""
    pixel = PixelPoint(x=600.0, y=400.0)

    result = distortion.distort_point(pixel, _CAMERA_MATRIX, _DIST_COEFFS)
    check = distortion.undistort_point(result, _CAMERA_MATRIX, _DIST_COEFFS)

    assert check.x == pytest.approx(pixel.x, abs=1e-4)
    assert check.y == pytest.approx(pixel.y, abs=1e-4)


def test_distort_point_converges_for_strong_off_centre_distortion():
    """Regression case (found while writing this ticket's tests): a
    strong, wide-angle-style lens model (``|k1| = 0.5``, a smaller
    focal length than :data:`_CAMERA_MATRIX` so a moderately off-centre
    pixel already sits at a large normalized radius) needs roughly 55
    fixed-point iterations to reach
    :data:`distortion._DISTORT_POINT_TOLERANCE_PX`, comfortably inside
    :data:`distortion._DISTORT_POINT_MAX_ITERATIONS`'s cap (raised from
    an initial, too-small 20 specifically because this case exposed it
    stopping short, mid-oscillation, well before convergence), but
    this pins the regression: a future change to the iteration cap or
    update rule must not silently reintroduce a too-small cap for a
    plausible (if strong) real lens fit."""
    camera_matrix = np.array(
        [[400.0, 0.0, 450.0], [0.0, 400.0, 350.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    dist_coeffs = np.array([-0.5, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
    pixel = PixelPoint(x=300.0, y=200.0)  # off-centre relative to (450, 350)

    result = distortion.distort_point(pixel, camera_matrix, dist_coeffs)
    check = distortion.undistort_point(result, camera_matrix, dist_coeffs)

    assert check.x == pytest.approx(pixel.x, abs=1e-4)
    assert check.y == pytest.approx(pixel.y, abs=1e-4)
