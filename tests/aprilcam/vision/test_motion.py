"""Tests for ``aprilcam.vision.motion`` (ticket 021-004; the
below-noise-floor heading fix, ticket 022-007; its sprint-036 revision
to also suppress ``speed``, ticket 036-003).

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
the below-threshold cases (below) test the noise-floor branch, since
that is exactly the case the fixes reclassify from "defined" to
"undefined" -- see ``vision/motion.py``'s module docstring for why.

Ticket 036-003's change is dt-independent (the threshold is a pure
distance test), so its boundary tests are parametrized over both a
short ``dt`` (~0.25s, the streaming/polling cadence that motivated the
fix -- jitter/dt is large at this scale) and a long ``dt`` (several
seconds, ticket 022-007's original assumption -- jitter/dt is small at
this scale) to confirm ``speed`` suppression does not depend on which
regime produced the small displacement.
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


def test_zero_motion_case_reports_all_none():
    """Regression, ticket 022-007, revised by ticket 036-003: exactly-zero
    displacement is the n=0 limit of the below-noise-floor case a
    physically stationary real tag hits every burst (sub-pixel detector
    jitter almost never lands on *exactly* zero, but it lands well
    inside ``MIN_RELIABLE_DISPLACEMENT_CM`` -- see
    ``test_displacement_just_below_the_threshold_...`` below for that
    more realistic case). Before ticket 022-007, ``heading`` was defined
    as ``0.0`` here (``atan2(0, 0)``) -- a direction of travel for a tag
    that never moved. Ticket 022-007 fixed that but left ``speed`` a
    real, honestly-reported ``0.0``; ticket 036-003 found that reasoning
    only held at ``dt`` on the order of a second or more, and this
    exact-zero-displacement case has ``speed == 0.0`` at *any* ``dt`` --
    it was never actually the counterexample. It is included here
    anyway, unchanged, as the simplest possible below-threshold case:
    ``speed`` is ``None`` alongside ``velocity``/``heading`` now, same
    as every other below-threshold case."""
    history = [
        (0.0, WorldPoint(x=3.0, y=4.0)),
        (1.0, WorldPoint(x=3.0, y=4.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is None
    assert speed is None
    assert heading is None


@pytest.mark.parametrize(
    "dt, dt_label",
    [
        pytest.param(0.25, "short-dt-streaming-cadence", id="short-dt"),
        pytest.param(4.0, "long-dt-several-seconds", id="long-dt"),
    ],
)
def test_displacement_just_below_the_threshold_reports_all_none(dt, dt_label):
    """The realistic case ticket 022-007 targeted, and the one ticket
    036-003 revisits: a small, non-zero displacement -- exactly what
    sub-pixel detector jitter on a physically stationary real tag
    produces -- must not manufacture a confident-looking ``heading_rad``
    *or*, as of 036-003, a confident-looking ``speed``.

    Parametrized over both a short ``dt`` (~0.25s, the streaming/polling
    cadence that motivated 036-003 -- here ``speed`` would have been
    ``jitter / 0.25 ~= 4x`` the jitter magnitude, clearly non-negligible)
    and a long ``dt`` (several seconds, ticket 022-007's original
    assumption -- here ``speed`` would have been small even before this
    fix) to prove the suppression is a pure distance test, unaffected by
    which regime produced the small displacement."""
    jitter = MIN_RELIABLE_DISPLACEMENT_CM * 0.5
    history = [
        (0.0, WorldPoint(x=-67.1, y=0.0)),
        (dt, WorldPoint(x=-67.1 + jitter, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is None, dt_label
    assert heading is None, dt_label
    assert speed is None, dt_label


@pytest.mark.parametrize(
    "dt, dt_label",
    [
        pytest.param(0.25, "short-dt-streaming-cadence", id="short-dt"),
        pytest.param(4.0, "long-dt-several-seconds", id="long-dt"),
    ],
)
def test_displacement_just_above_the_threshold_reports_a_real_speed(dt, dt_label):
    """The other side of the same boundary, at both time scales: a
    displacement that clears the threshold gets a real, non-``None``
    ``speed`` (and ``velocity``/``heading``) regardless of how large or
    small ``dt`` is -- the threshold ticket 036-003 applies to ``speed``
    is purely about displacement, never about ``dt``."""
    motion = MIN_RELIABLE_DISPLACEMENT_CM * 1.5
    history = [
        (0.0, WorldPoint(x=0.0, y=0.0)),
        (dt, WorldPoint(x=motion, y=0.0)),
    ]
    velocity, speed, heading = compute_motion(history)

    assert velocity is not None, dt_label
    assert heading is not None, dt_label
    assert speed is not None and speed == pytest.approx(motion / dt), dt_label


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
