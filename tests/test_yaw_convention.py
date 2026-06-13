"""Pin the reported ENU orientation convention.

Reported world frame: x right, y up, origin at AprilTag 1. Angles use
``yaw = atan2(Δy, Δx)`` measured 0°=+X, counter-clockwise positive
(ROS REP-103 "math angles"), so ``forward = (cos yaw, sin yaw)`` points
along the tag heading in reported world coordinates. Raw homography /
pixel coordinates are y-down; :func:`world_yaw` negates Y to reach the
reported frame.
"""

import math

import numpy as np
import pytest

from aprilcam.core.models import AprilTag, world_yaw
from aprilcam.camera.composite import map_tags_to_primary


def _tag_corners(center, top_dir, size=10.0):
    """4 pixel corners whose top edge (corners[0], corners[1]) midpoint lies
    ``size`` from ``center`` along ``top_dir``. The corner mean equals center."""
    cx, cy = center
    tx, ty = top_dir
    px, py = -ty, tx  # perpendicular spread for the two top/bottom corners
    tmx, tmy = cx + tx * size, cy + ty * size
    bmx, bmy = cx - tx * size, cy - ty * size
    c0 = (tmx - px * size, tmy - py * size)
    c1 = (tmx + px * size, tmy + py * size)
    c2 = (bmx + px * size, bmy + py * size)
    c3 = (bmx - px * size, bmy - py * size)
    return np.array([c0, c1, c2, c3], dtype=np.float32)


def _angles_close(a, b, tol=1e-6):
    return math.isclose(math.cos(a), math.cos(b), abs_tol=tol) and math.isclose(
        math.sin(a), math.sin(b), abs_tol=tol
    )


@pytest.mark.parametrize(
    "dx,dy,expected",
    [
        (1.0, 0.0, 0.0),            # raw +X -> reported +X
        (0.0, -1.0, math.pi / 2),   # raw image-up (y-down negative) -> world +Y
        (-1.0, 0.0, math.pi),       # raw -X -> 180°
        (0.0, 1.0, -math.pi / 2),   # raw image-down -> world -Y
    ],
)
def test_world_yaw_cardinals(dx, dy, expected):
    assert _angles_close(world_yaw(dx, dy), expected)


@pytest.mark.parametrize("dx,dy", [(1, 0), (0, -1), (3, -4), (-2, -1), (5, 2)])
def test_world_yaw_forward_identity(dx, dy):
    """forward = (cos yaw, sin yaw) equals the reported (y-up) heading,
    i.e. normalize((dx, -dy))."""
    yaw = world_yaw(dx, dy)
    n = math.hypot(dx, dy)
    assert math.isclose(math.cos(yaw), dx / n, abs_tol=1e-9)
    assert math.isclose(math.sin(yaw), -dy / n, abs_tol=1e-9)


@pytest.mark.parametrize(
    "top_dir,expected",
    [
        ((1.0, 0.0), 0.0),           # top edge -> image right -> +X -> yaw 0
        ((0.0, -1.0), math.pi / 2),  # top edge -> image up -> world +Y -> +90°
        ((0.0, 1.0), -math.pi / 2),  # top edge -> image down -> world -Y -> -90°
    ],
)
def test_from_corners_pixel_fallback(top_dir, expected):
    corners = _tag_corners((100.0, 100.0), top_dir)
    tag = AprilTag.from_corners(1, corners, homography=None)
    assert _angles_close(tag.orientation_yaw, expected)
    # forward = (cos, sin) = reported-world heading = (tx, -ty)
    fwd = (math.cos(tag.orientation_yaw), math.sin(tag.orientation_yaw))
    assert math.isclose(fwd[0], top_dir[0], abs_tol=1e-6)
    assert math.isclose(fwd[1], -top_dir[1], abs_tol=1e-6)


def test_from_corners_uses_world_space_not_pixel():
    """A rotation homography must rotate the yaw — proving orientation is
    measured in world space (post-homography), not raw pixel space."""
    # H maps pixel (u, v) -> world (-v, u): a 90° rotation of directions.
    H = np.array(
        [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    corners = _tag_corners((100.0, 100.0), (1.0, 0.0))  # pixel top points +X
    tag = AprilTag.from_corners(1, corners, homography=H)
    # world heading becomes (0, +10) -> world_yaw(0, 10) = -pi/2
    assert _angles_close(tag.orientation_yaw, -math.pi / 2)
    # pixel-only would have yielded yaw 0 — confirm we did NOT get that
    assert not _angles_close(tag.orientation_yaw, 0.0)


def test_composite_yaw_convention():
    corners = _tag_corners((50.0, 50.0), (0.0, -1.0))  # top points image-up
    out = map_tags_to_primary([(corners, None, 7)], np.eye(3, dtype=np.float64))
    assert len(out) == 1
    # image-up -> world +Y -> +90°
    assert _angles_close(out[0]["orientation_yaw"], math.pi / 2)
