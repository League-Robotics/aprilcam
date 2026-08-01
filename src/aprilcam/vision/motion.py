"""Velocity, speed, and heading from a sequence of timestamped world positions.

Pure kinematics, independent of detection and calibration status (sprint
021's architecture table, Step 2: "it simply is not invoked when
``world`` is ``None``"). This module has no dependency on
``vision/pipeline.py``, ``vision/detector.py``, or anything in
``daemon/`` -- it operates entirely on ``(timestamp, WorldPoint)`` pairs
a caller already has in hand.

**Built inert in sprint 021, activated in sprint 022 (ticket 022-004).**
No calibration subsystem existed in sprint 021, so every ``TagRecord``
that sprint carried ``world=None`` and nothing called
:func:`compute_motion` with a real position sequence -- it was built and
tested purely against synthetic ``WorldPoint`` sequences (sprint.md Step
3's module table: "its purpose ... is real and deferred only by
calibration's absence, not by design uncertainty"). ``daemon/
detection.py``'s ``DetectionEngine.run_burst`` now calls this function
for real, for every calibrated hit, with that tag's buffered
world-position history plus its new sample -- no change to this
module's own math was needed to activate it, exactly as sprint 021
predicted.

**Heading convention** (``types.py`` module docstring, restated here
since this is the one place it is actually computed): ``heading_rad =
atan2(world_vy, world_vx)``, ``0`` = east, counter-clockwise positive --
motion direction, not facing direction. ``TagRecord.yaw_rad`` (the tag's
front-facing direction) uses the same numeric convention but is a
distinct quantity computed elsewhere (``daemon/detection.py``, from the
tag's boundary corners) -- a robot driving backwards has the two 180
degrees apart, which is signal, not a bug (``types.TagRecord``'s own
docstring).

**Below-noise-floor motion reports a defined ``speed`` but an undefined
``heading`` (ticket 022-007 fix).** Activating this module against real
hardware (sprint 022's acceptance gate) surfaced a defect sprint 021's
purely-synthetic testing could not: a physically stationary tag's two
most recent samples are never bit-for-bit identical -- sub-pixel
detector jitter between bursts moves the reported position by a small,
essentially random amount every time, even though nothing moved. Before
this fix, :func:`compute_motion` reported ``atan2`` of that jitter as a
fully-formed ``heading_rad`` -- a *confident-looking, wrong* answer
(exactly the failure mode the world-first contract in ``types.py``
exists to prevent): real hardware output looked like ``heading=-1.633
speed=0.00`` for a tag that had not moved. A direction of travel is not
a small perturbation of a real answer at this magnitude -- it is pure
noise dressed as data, and a robot consuming ``heading_rad`` would
steer on it.

The fix distinguishes **magnitude** from **direction**: ``speed`` (a
non-negative scalar, bounded near zero for jitter) stays a real,
un-suppressed reading of ``hypot(vx, vy)`` even when that reading is
tiny -- "roughly stationary" is exactly what a small speed value
honestly means, jitter or not. ``heading_rad`` (a direction, which is
*disproportionately* sensitive to noise as displacement shrinks --
``atan2`` of a jitter vector points anywhere on the circle with equal
likelihood) is instead reported ``None`` -- "undefined", not "zero" --
whenever the two-sample displacement falls below
:data:`MIN_RELIABLE_DISPLACEMENT_CM`. ``world_velocity`` is suppressed
alongside ``heading_rad`` (not alongside ``speed``): the *vector*
carries direction just as much as ``heading_rad`` does (a caller could
trivially recover the same noise-driven angle via
``atan2(velocity.y, velocity.x)``), so hiding one while exposing the
other would not actually protect a caller from the failure this fix
targets.

**Where the threshold comes from.** Ticket 022-003's real hardware
verification against ``main-playfield`` measured this daemon's actual
calibration reprojection error at ~0.18-0.19 cm RMS (that ticket's
completion notes: 0.187 cm, later 0.184 cm) -- the best available
real-world proxy for how much noise one position sample carries. Two
independent samples each carrying that much uncertainty give their
*difference* a noise floor around ``sqrt(2) x 0.18 ~= 0.25`` cm (RMS of
two independent errors combines in quadrature) -- and that figure is
itself an optimistic floor, since ``reprojection_error_cm`` is measured
from calibration's own averaged, multi-frame, purpose-lit capture, while
two consecutive live ``get_tags`` bursts are single frames apiece,
subject to whatever per-frame lighting/exposure/JPEG-compression jitter
calibration's averaging smooths away. :data:`MIN_RELIABLE_DISPLACEMENT_CM`
is set to roughly double that ~0.25 cm noise floor -- comfortably above
it so ordinary jitter reliably clears the "undefined" bar, while still
small enough (about the width of a fingernail on a ~134 x 89 cm field)
that any physically deliberate motion between two samples clears it just
as easily. This is a single fixed constant rather than a per-calibration
value derived from that camera's own ``reprojection_error_cm``: keeping
this module's only dependency ``(timestamp, WorldPoint)`` pairs (no
``Calibration`` object, no daemon import) is deliberate scope, per the
module docstring above -- a per-camera-adaptive threshold is a
reasonable future refinement, not required to fix the defect at hand.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from aprilcam.types import WorldPoint

__all__ = ["compute_motion", "MIN_RELIABLE_DISPLACEMENT_CM"]

#: Two-sample displacement (cm) below which a direction of travel is
#: noise, not signal -- see the module docstring's "Where the threshold
#: comes from" section for the full derivation from ticket 022-003's
#: real ~0.18-0.19 cm reprojection error. Below this, :func:`compute_motion`
#: reports ``heading_rad`` and ``velocity`` as ``None`` (undefined, never
#: a fabricated confident angle) while still reporting ``speed`` honestly
#: -- a small, real, non-negative number is not a misleading answer to
#: "how fast", the way an ``atan2`` of jitter is a misleading answer to
#: "which way".
MIN_RELIABLE_DISPLACEMENT_CM = 0.5


def compute_motion(
    history: Sequence[Tuple[float, WorldPoint]],
) -> Tuple[Optional[WorldPoint], Optional[float], Optional[float]]:
    """Velocity, speed, and heading from a chronological ``(timestamp, WorldPoint)`` sequence.

    *history* is ordered oldest to newest; only the two most recent
    entries are used -- this is a simple two-point finite difference
    between the latest pair, not a fit over the whole sequence, matching
    a per-frame record's "as observed in this frame, relative to the
    previous one" contract.

    Returns ``(None, None, None)`` -- never a fabricated zero -- when
    fewer than two points are available, or when the two most recent
    points share a timestamp (a zero-``dt`` division would otherwise
    silently produce ``inf``/``nan``). "Cannot compute" and "computed,
    and the answer is zero" must stay distinguishable, the same
    Optional-over-zero-default reasoning ``TagRecord.world`` and
    ``Daemon.get_tag`` already apply.

    Below :data:`MIN_RELIABLE_DISPLACEMENT_CM` of straight-line
    displacement between the two points, ``velocity`` and
    ``heading_rad`` are **also** ``None`` -- direction is undefined, not
    zero, when the two samples are indistinguishable from detector
    jitter (module docstring's "Below-noise-floor motion" section).
    ``speed`` is the one exception: it is always returned as a real,
    non-negative ``float`` whenever *history* has 2+ points with
    distinct timestamps, including this near-zero-displacement case --
    a small honest speed reading, jitter or not, is a truthful answer
    to "how fast", unlike the direction fields.

    Args:
        history: Oldest-to-newest ``(timestamp, WorldPoint)`` pairs, in
            seconds and world-frame cm (``types.py``'s A1-centred,
            +x east, +y north convention).

    Returns:
        A ``(velocity, speed, heading_rad)`` triple. ``velocity`` is a
        :class:`~aprilcam.types.WorldPoint` of cm/s components, or
        ``None`` below the reliable-displacement threshold. ``speed`` is
        always a real ``float`` once two distinct-timestamp points
        exist. ``heading_rad`` is ``atan2(velocity.y, velocity.x)`` --
        ``0`` = east, CCW positive -- above the threshold; ``None``
        (undefined) at or below it, rather than the noise-driven angle
        ``atan2`` of a near-zero vector would otherwise produce.
    """
    if len(history) < 2:
        return None, None, None

    (t0, p0), (t1, p1) = history[-2], history[-1]
    dt = t1 - t0
    if dt <= 0.0:
        return None, None, None

    vx = (p1.x - p0.x) / dt
    vy = (p1.y - p0.y) / dt
    speed = math.hypot(vx, vy)

    displacement_cm = math.hypot(p1.x - p0.x, p1.y - p0.y)
    if displacement_cm < MIN_RELIABLE_DISPLACEMENT_CM:
        # Direction is undefined at this magnitude (module docstring) --
        # velocity and heading stay None; speed alone is still honestly
        # reported above.
        return None, speed, None

    velocity = WorldPoint(x=vx, y=vy)
    heading_rad = math.atan2(vy, vx)
    return velocity, speed, heading_rad
