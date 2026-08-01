"""Tests for ``aprilcam.vision.motion`` (ticket 021-004).

Pure-function tests against synthetic ``(timestamp, WorldPoint)``
sequences -- exercising this module in isolation, the way sprint 021
wrote it before any calibration subsystem existed. ``daemon/
detection.py`` now calls :func:`compute_motion` for real on a calibrated
camera's buffered tag history (ticket 022-004); that real call path is
pinned in ``tests/aprilcam/daemon/test_detection.py`` instead of here,
so this module's own contract (its behaviour as a pure function of a
position sequence) stays tested independently of ``daemon/`` or any
calibration fixture.
"""

from __future__ import annotations

import math

from aprilcam.types import WorldPoint
from aprilcam.vision.motion import compute_motion


def test_fewer_than_two_points_returns_all_none():
    velocity, speed, heading = compute_motion([(0.0, WorldPoint(x=0.0, y=0.0))])
    assert velocity is None
    assert speed is None
    assert heading is None


def test_no_points_returns_all_none():
    velocity, speed, heading = compute_motion([])
    assert velocity is None
    assert speed is None
    assert heading is None


def test_zero_dt_returns_all_none_rather_than_dividing_by_zero():
    history = [
        (1.0, WorldPoint(x=0.0, y=0.0)),
        (1.0, WorldPoint(x=5.0, y=5.0)),
    ]
    velocity, speed, heading = compute_motion(history)
    assert velocity is None
    assert speed is None
    assert heading is None


def test_due_east_motion_has_heading_zero():
    history = [
        (0.0, WorldPoint(x=0.0, y=0.0)),
        (1.0, WorldPoint(x=10.0, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is not None and speed is not None and heading is not None
    assert velocity.x == 10.0
    assert velocity.y == 0.0
    assert speed == 10.0
    assert heading == 0.0


def test_due_north_motion_has_heading_quarter_turn_ccw():
    history = [
        (0.0, WorldPoint(x=0.0, y=0.0)),
        (2.0, WorldPoint(x=0.0, y=20.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is not None and speed is not None and heading is not None
    assert velocity.x == 0.0
    assert velocity.y == 10.0
    assert speed == 10.0
    assert math.isclose(heading, math.pi / 2)


def test_due_west_motion_has_heading_pi():
    history = [
        (0.0, WorldPoint(x=10.0, y=0.0)),
        (1.0, WorldPoint(x=0.0, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is not None and speed is not None and heading is not None
    assert math.isclose(heading, math.pi)


def test_zero_motion_case_reports_zero_speed_and_defined_heading():
    history = [
        (0.0, WorldPoint(x=3.0, y=4.0)),
        (1.0, WorldPoint(x=3.0, y=4.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity == WorldPoint(x=0.0, y=0.0)
    assert speed == 0.0
    assert heading == 0.0


def test_only_the_last_two_points_matter():
    """A longer history's earlier points do not skew the finite
    difference -- only the most recent pair is differenced."""
    history = [
        (0.0, WorldPoint(x=0.0, y=0.0)),
        (100.0, WorldPoint(x=-500.0, y=900.0)),  # wildly different if used
        (10.0, WorldPoint(x=0.0, y=0.0)),
        (11.0, WorldPoint(x=10.0, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity == WorldPoint(x=10.0, y=0.0)
    assert speed == 10.0
    assert heading == 0.0
