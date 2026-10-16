"""Synthetic ChArUco board rendering shared by the intrinsics tests."""
from __future__ import annotations

import cv2
import numpy as np

from aprilcam.intrinsics.detector import BoardDetector
from aprilcam.intrinsics.interfaces import BoardSpec, DEFAULT_BOARD_SPEC

SPEC = BoardSpec.parse(DEFAULT_BOARD_SPEC)
W, H = 1280, 720
PX_PER_M = 8000.0  # source board raster resolution


def board_image(spec: BoardSpec = SPEC) -> np.ndarray:
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, spec.dict_name))
    b = cv2.aruco.CharucoBoard((spec.cols, spec.rows), spec.square_mm / 1000,
                               spec.marker_mm / 1000, d)
    size = (int(spec.cols * spec.square_mm / 1000 * PX_PER_M),
            int(spec.rows * spec.square_mm / 1000 * PX_PER_M))
    return b.generateImage(size, marginSize=0, borderBits=1)


_IMG = None


def render(centre_xy, dist_m, tilt_deg=0.0, az_deg=0.0, spec: BoardSpec = SPEC):
    """Render the board seen from a camera with f = W, centred on *centre_xy*.

    The board is tilted *tilt_deg* about an in-plane axis chosen so its normal
    leans toward *az_deg* (image-plane direction), then placed at *dist_m*.
    """
    global _IMG
    if _IMG is None:
        _IMG = board_image(spec)
    img = _IMG
    bw = spec.cols * spec.square_mm / 1000
    bh = spec.rows * spec.square_mm / 1000
    k = np.array([[W, 0, W / 2], [0, W, H / 2], [0, 0, 1]], dtype=np.float64)
    a = np.radians(az_deg)
    axis = np.array([-np.sin(a), np.cos(a), 0.0]) * np.radians(tilt_deg)
    rot, _ = cv2.Rodrigues(axis)
    # Board centre goes to the requested pixel at depth dist_m.
    cx = (centre_xy[0] - W / 2) / W * dist_m
    cy = (centre_xy[1] - H / 2) / W * dist_m
    t = np.array([cx, cy, dist_m])
    centre = np.array([bw / 2, bh / 2, 0.0])
    t = t - rot @ centre
    m = k @ np.column_stack([rot[:, 0], rot[:, 1], t])
    m = m @ np.diag([1 / PX_PER_M, 1 / PX_PER_M, 1.0])
    out = cv2.warpPerspective(img, m, (W, H), flags=cv2.INTER_LINEAR, borderValue=255)
    return out


_DET = None


def detect(gray):
    global _DET
    if _DET is None:
        _DET = BoardDetector(SPEC)
    return _DET.detect(gray)
