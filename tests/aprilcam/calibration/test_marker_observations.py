"""Tests for ``Calibrator._detect_markers`` (sprint 045 ticket 003).

Real ``TagDetector`` on synthetic frames: ArUco markers rendered with
``cv2.aruco.generateImageMarker`` and warped by a known homography, so the
recovered corners can be compared to analytically known truth.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import cv2 as cv
import numpy as np

from aprilcam.calibration.calibration import Calibrator
from aprilcam.calibration.interfaces import MarkerObservation
from aprilcam.types import TagFamily

MARKER_PX = 120  # marker side in marker-local pixels
QUIET = 30
CANVAS = (640, 480)


def _marker_with_quiet(tag_id: int, rotate_k: int = 0) -> np.ndarray:
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    m = cv.aruco.generateImageMarker(dictionary, tag_id, MARKER_PX)
    img = np.full((MARKER_PX + 2 * QUIET,) * 2, 255, np.uint8)
    img[QUIET : QUIET + MARKER_PX, QUIET : QUIET + MARKER_PX] = m
    return np.rot90(img, rotate_k).copy()  # rot90 is CCW; image-level rotation


def _render(
    placements: Sequence[Tuple[int, np.ndarray, int]],
) -> Tuple[np.ndarray, dict]:
    """Render markers; return (BGR frame, {id: true 4x2 corners in marker TL,TR,BR,BL})."""
    canvas = np.full((CANVAS[1], CANVAS[0]), 255, np.uint8)
    truth = {}
    side = MARKER_PX + 2 * QUIET
    for tag_id, homography, rotate_k in placements:
        src = _marker_with_quiet(tag_id, rotate_k)
        warped = cv.warpPerspective(
            src, homography, CANVAS, flags=cv.INTER_LINEAR, borderValue=255
        )
        mask = cv.warpPerspective(
            np.full(src.shape, 255, np.uint8), homography, CANVAS, borderValue=0
        )
        canvas = np.where(mask > 0, warped, canvas)
        # Marker's own corners in the (possibly rotated) source image.
        own = np.array(
            [[QUIET, QUIET], [QUIET + MARKER_PX, QUIET],
             [QUIET + MARKER_PX, QUIET + MARKER_PX], [QUIET, QUIET + MARKER_PX]],
            np.float64,
        )
        # Apply rot90 CCW k times to the point coordinates: (x, y) -> (y, side - x).
        for _ in range(rotate_k % 4):
            own = np.stack([own[:, 1], side - own[:, 0]], axis=1)
        # Pixel centres: corner coordinates are edges, shift by -0.5 for pixel-centre
        # convention used by OpenCV warps/detectors.
        pts = cv.perspectiveTransform((own - 0.5).reshape(-1, 1, 2), homography)
        truth[tag_id] = pts.reshape(4, 2)
    return cv.cvtColor(canvas, cv.COLOR_GRAY2BGR), truth


def _homography(tx: float, ty: float, angle_deg: float = 8.0, persp: float = 8e-5) -> np.ndarray:
    a = np.deg2rad(angle_deg)
    h = np.array([[np.cos(a), -np.sin(a), tx], [np.sin(a), np.cos(a), ty], [persp, persp / 2, 1.0]])
    return h


def _calibrator(frames: List[np.ndarray]) -> Calibrator:
    return Calibrator(
        get_camera_config=lambda slug: None,
        get_playfield=lambda name: None,
        capture_frames=lambda slug: frames,
    )


def test_corners_recovered_within_tolerance():
    placements = [(3, _homography(60, 50), 0), (7, _homography(380, 250, -12.0), 0)]
    frame, truth = _render(placements)
    obs = _calibrator([frame])._detect_markers([frame])[TagFamily.ARUCO]
    assert set(obs) == {3, 7}
    for tag_id, ob in obs.items():
        assert isinstance(ob, MarkerObservation)
        got = np.array([[c.x, c.y] for c in ob.corners])
        assert np.abs(got - truth[tag_id]).max() < 0.3
        assert ob.frames == 1 and ob.corner_spread_px == 0.0


def test_rotated_marker_keeps_its_own_corner_order():
    for k in (1, 2, 3):  # 90, 180, 270 degrees
        frame, truth = _render([(5, _homography(200, 150, 0.0, 0.0), k)])
        ob = _calibrator([frame])._detect_markers([frame])[TagFamily.ARUCO][5]
        got = np.array([[c.x, c.y] for c in ob.corners])
        assert np.abs(got - truth[5]).max() < 0.3, f"rot90 k={k} was renumbered"
    # Sanity: the upside-down marker's reported TL is the image's bottom-right.
    assert truth[5][0][0] > truth[5][2][0]


def test_subset_of_frames_averages_over_those_frames():
    h = _homography(100, 100)
    with_marker, _ = _render([(2, h, 0)])
    without = np.full_like(with_marker, 255)
    obs = _calibrator([])._detect_markers([with_marker, without, with_marker])
    assert obs[TagFamily.ARUCO][2].frames == 2


def test_averaging_reduces_spread_and_matches_detect_averaged():
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(12):
        dx, dy = rng.uniform(-0.6, 0.6, 2)
        f, _ = _render([(4, _homography(150 + dx, 120 + dy), 0)])
        frames.append(f)
    cal = _calibrator(frames)
    ob = cal._detect_markers(frames)[TagFamily.ARUCO][4]
    assert ob.frames == 12
    # Jitter of up to 0.6 px per axis gives a real, nonzero spread...
    assert 0.05 < ob.corner_spread_px < 1.0
    # ...and the averaged corners sit closer to the true mean position than
    # a typical single frame does.
    single = cal._detect_markers([frames[0]])[TagFamily.ARUCO][4]
    assert single.corner_spread_px == 0.0
    centres = cal._detect_averaged(frames)[TagFamily.ARUCO]
    assert centres[4] == ob.centre
