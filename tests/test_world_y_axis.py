"""Regression: world +Y must equal physical NORTH (no Y inversion).

Bug (radio-robot-c, 2026-06-16): the daemon/MCP A1-centring transform used
``wy = origin_y - world_xy[1]``, which inverts north/south for the def-driven
homography (which is A1-centred with +y NORTH).  A tag physically north read a
NEGATIVE world_xy.y, so a client driving to a map coordinate hit the Y-mirror.

The transform must be a pure origin shift (``wy = world_xy[1] - origin_y``):
north H-world (+y) stays +y, south stays -y.
"""
import dataclasses

import pytest

from aprilcam.server.mcp_server import _a1_coord_transform


@dataclasses.dataclass
class _Rec:
    world_xy: tuple
    id: int = 1


def _apply(origin, world_xy):
    return _a1_coord_transform(origin[0], origin[1])([_Rec(world_xy=world_xy)])[0].world_xy


def test_north_stays_positive_south_stays_negative():
    # Homography is A1-centred, +y north: a north marker has H-world y > 0.
    origin = (0.03, -0.20)                       # ~AprilTag-1 world (near zero)
    north = _apply(origin, (10.0, 44.65))        # physical north edge
    south = _apply(origin, (10.0, -44.65))       # physical south edge
    assert north[1] > 0, f"north must read +Y, got {north}"
    assert south[1] < 0, f"south must read -Y, got {south}"
    # magnitude preserved (pure shift), not mirrored
    assert north[1] == pytest.approx(44.65 - origin[1])
    assert south[1] == pytest.approx(-44.65 - origin[1])


def test_apriltag1_origin_maps_to_zero():
    # The marker at the origin reads ~ (0, 0) regardless of axis convention.
    origin = (0.03, -0.20)
    out = _apply(origin, origin)
    assert out == pytest.approx((0.0, 0.0))


def test_x_axis_unchanged_east_positive():
    # X was never flipped: east (+x) stays +x.
    origin = (0.03, -0.20)
    east = _apply(origin, (67.0, 0.0))
    assert east[0] > 0
