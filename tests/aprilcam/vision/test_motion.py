"""Tests for ``aprilcam.vision.motion`` (ticket 021-004; the
below-noise-floor heading fix, ticket 022-007).

Pure-function tests against synthetic ``(timestamp, WorldPoint)``
sequences -- exercising this module in isolation, the way sprint 021
wrote it before any calibration subsystem existed. ``daemon/
detection.py`` now calls :func:`compute_motion` for real on a calibrated
camera's buffered tag history (ticket 022-004); that real call path is
pinned in ``tests/aprilcam/daemon/test_detection.py`` instead of here,
so this module's own contract (its behaviour as a pure function of a
position sequence) stays tested independently of ``daemon/`` or any
calibration fixture.

Every displacement below uses values well clear of either side of
:data:`MIN_RELIABLE_DISPLACEMENT_CM` -- current tests for genuine motion
(``test_due_east_motion_has_heading_zero`` etc.) already move 10-20 cm,
comfortably above the 0.5 cm threshold, so they needed no change; only
the exact-zero-motion case (below) had to change, since that is exactly
the case the fix reclassifies from "defined heading 0.0" to "undefined
heading" -- see ``vision/motion.py``'s module docstring for why.
"""

from __future__ import annotations

import math

import pytest

from aprilcam.types import WorldPoint
from aprilcam.vision.motion import MIN_RELIABLE_DISPLACEMENT_CM, compute_motion


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


def test_zero_motion_case_reports_zero_speed_and_undefined_heading():
    """Regression, ticket 022-007: exactly-zero displacement is the
    n=0 limit of the below-noise-floor case a physically stationary
    real tag hits every burst (sub-pixel detector jitter almost never
    lands on *exactly* zero, but it lands well inside
    ``MIN_RELIABLE_DISPLACEMENT_CM`` -- see
    ``test_displacement_just_below_the_threshold_...`` below for that
    more realistic case). Before this fix, ``heading`` was defined as
    ``0.0`` here (``atan2(0, 0)``) -- a direction of travel for a tag
    that never moved. ``speed`` stays a real, honestly-reported ``0.0``:
    unlike heading, "zero speed" is not a misleading answer for a
    stationary tag."""
    history = [
        (0.0, WorldPoint(x=3.0, y=4.0)),
        (1.0, WorldPoint(x=3.0, y=4.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is None
    assert speed == 0.0
    assert heading is None


def test_displacement_just_below_the_threshold_reports_speed_but_undefined_heading():
    """The realistic case the fix targets: a small, non-zero
    displacement -- exactly what sub-pixel detector jitter on a
    physically stationary real tag produces -- must not manufacture a
    confident-looking ``heading_rad``."""
    jitter = MIN_RELIABLE_DISPLACEMENT_CM * 0.5
    history = [
        (0.0, WorldPoint(x=-67.1, y=0.0)),
        (1.0, WorldPoint(x=-67.1 + jitter, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is None
    assert heading is None
    assert speed is not None and speed == pytest.approx(jitter, abs=1e-9)


def test_displacement_just_above_the_threshold_reports_a_real_heading():
    """The other side of the same boundary: a displacement that clears
    the threshold gets a fully-defined velocity/speed/heading, exactly
    as before this fix -- the fix narrows the "confident answer" case,
    it does not remove it."""
    motion = MIN_RELIABLE_DISPLACEMENT_CM * 1.5
    history = [
        (0.0, WorldPoint(x=0.0, y=0.0)),
        (1.0, WorldPoint(x=motion, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is not None and velocity.x == pytest.approx(motion) and velocity.y == 0.0
    assert speed is not None and speed == pytest.approx(motion)
    assert heading is not None and heading == 0.0


def test_displacement_exactly_at_the_threshold_is_treated_as_reliable():
    """The boundary itself is inclusive on the "reliable" side --
    ``displacement_cm < MIN_RELIABLE_DISPLACEMENT_CM`` suppresses
    heading, so a displacement equal to the threshold (not less than
    it) still reports a real heading."""
    history = [
        (0.0, WorldPoint(x=0.0, y=0.0)),
        (1.0, WorldPoint(x=MIN_RELIABLE_DISPLACEMENT_CM, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is not None
    assert heading is not None and heading == 0.0


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
