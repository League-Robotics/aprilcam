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

**Below-noise-floor motion originally split speed from heading; sprint
036 reunited them (ticket 022-007's fix, then its sprint-036
correction).** Activating this module against real hardware (sprint
022's acceptance gate) surfaced a defect sprint 021's purely-synthetic
testing could not: a physically stationary tag's two most recent
samples are never bit-for-bit identical -- sub-pixel detector jitter
between bursts moves the reported position by a small, essentially
random amount every time, even though nothing moved. Before ticket
022-007's fix, :func:`compute_motion` reported ``atan2`` of that jitter
as a fully-formed ``heading_rad`` -- a *confident-looking, wrong*
answer (exactly the failure mode the world-first contract in
``types.py`` exists to prevent): real hardware output looked like
``heading=-1.633 speed=0.00`` for a tag that had not moved. A direction
of travel is not a small perturbation of a real answer at this
magnitude -- it is pure noise dressed as data, and a robot consuming
``heading_rad`` would steer on it.

Ticket 022-007's fix distinguished **magnitude** from **direction**:
``heading_rad`` (a direction, which is *disproportionately* sensitive
to noise as displacement shrinks -- ``atan2`` of a jitter vector points
anywhere on the circle with equal likelihood) was reported ``None`` --
"undefined", not "zero" -- whenever the two-sample displacement fell
below :data:`MIN_RELIABLE_DISPLACEMENT_CM`, while ``speed`` (a
non-negative scalar, bounded near zero *in magnitude* for jitter)
stayed a real, un-suppressed reading of ``hypot(vx, vy)`` even when
tiny -- the reasoning being that "a small speed value honestly means
roughly stationary."

**That reasoning silently assumed ``dt`` on the order of a second or
more (sprint 036 finding).** ``speed`` is bounded near zero *in
magnitude* for a fixed amount of jitter, but it is ``jitter / dt`` --
and that quotient grows without bound as ``dt`` shrinks, independent of
how small the jitter itself is. At streaming cadence or rapid
interactive polling (confirmed live: ~0.25s between two consecutive
MCP calls), the same ~0.25 cm of detector jitter the displacement
threshold exists to catch produces ``speed = jitter / dt ~= 1`` cm/s --
small in absolute terms, but confidently non-zero: exactly the
"looks like signal, is noise" failure mode ``heading_rad``'s
suppression already existed to prevent, just manifesting in the other
field because the original fix never applied the same test to it.
Sprint 036 closes that gap: ``speed`` is now suppressed to ``None``
alongside ``heading_rad``/``world_velocity`` whenever the two-sample
displacement falls below :data:`MIN_RELIABLE_DISPLACEMENT_CM`,
**regardless of ``dt``** -- the threshold is a pure distance test, so
no new constant was needed, only applying the existing one to a third
field. The accepted, honest consequence: real motion slower than
roughly ``MIN_RELIABLE_DISPLACEMENT_CM / dt`` now reads as ``None``
instead of a small positive number (e.g. ~2 cm/s at a 0.25s query
cadence) -- "no signal" over "confident wrong signal," the same trade
the original fix made for direction, now applied uniformly to
magnitude too.

``world_velocity`` is suppressed alongside ``heading_rad`` and (as of
sprint 036) ``speed``: the *vector* carries direction just as much as
``heading_rad`` does (a caller could trivially recover the same
noise-driven angle via ``atan2(velocity.y, velocity.x)``), so hiding it
while exposing either scalar would not actually protect a caller from
the failure this fix targets.

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

#: Two-sample displacement (cm) below which motion is noise, not signal
#: -- see the module docstring's "Where the threshold comes from"
#: section for the full derivation from ticket 022-003's real
#: ~0.18-0.19 cm reprojection error. Below this, :func:`compute_motion`
#: reports ``velocity``, ``speed``, and ``heading_rad`` **all** as
#: ``None`` (undefined, never a fabricated confident number) -- sprint
#: 036 extended suppression to ``speed`` too, since ``jitter / dt`` at
#: short ``dt`` is just as misleading a magnitude as ``atan2`` of jitter
#: is a misleading direction (module docstring's "Below-noise-floor
#: motion" section).
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
    displacement between the two points, ``velocity``, ``speed``, and
    ``heading_rad`` are **all** ``None`` -- the two samples are
    indistinguishable from detector jitter (module docstring's
    "Below-noise-floor motion" section), and at short ``dt`` that
    jitter divided by ``dt`` is just as misleading a "how fast" answer
    as ``atan2`` of it is a misleading "which way" answer (sprint 036;
    ticket 022-007's original fix suppressed only the direction fields,
    on the assumption ``dt`` would be on the order of a second or
    more -- an assumption that does not hold at streaming/polling
    cadence).

    Args:
        history: Oldest-to-newest ``(timestamp, WorldPoint)`` pairs, in
            seconds and world-frame cm (``types.py``'s A1-centred,
            +x east, +y north convention).

    Returns:
        A ``(velocity, speed, heading_rad)`` triple. All three are
        ``None`` below the reliable-displacement threshold (as well as
        with fewer than two distinct-timestamp points) -- never a
        fabricated zero. Above the threshold: ``velocity`` is a
        :class:`~aprilcam.types.WorldPoint` of cm/s components, ``speed``
        is ``hypot(velocity.x, velocity.y)``, and ``heading_rad`` is
        ``atan2(velocity.y, velocity.x)`` -- ``0`` = east, CCW positive.
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
        # Below the noise floor: direction is undefined, and (sprint
        # 036) so is magnitude at short dt -- jitter / dt is just as
        # misleading a speed as atan2(jitter) is a heading (module
        # docstring's "Below-noise-floor motion" section).
        return None, None, None

    velocity = WorldPoint(x=vx, y=vy)
    heading_rad = math.atan2(vy, vx)
    return velocity, speed, heading_rad
