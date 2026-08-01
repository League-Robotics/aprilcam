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
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from aprilcam.types import WorldPoint

__all__ = ["compute_motion"]


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

    Args:
        history: Oldest-to-newest ``(timestamp, WorldPoint)`` pairs, in
            seconds and world-frame cm (``types.py``'s A1-centred,
            +x east, +y north convention).

    Returns:
        A ``(velocity, speed, heading_rad)`` triple. ``velocity`` is a
        :class:`~aprilcam.types.WorldPoint` of cm/s components.
        ``heading_rad`` is ``atan2(velocity.y, velocity.x)`` -- ``0`` =
        east, CCW positive -- defined even for exactly-zero velocity
        (``atan2(0, 0) == 0.0``; a stationary tag needs no special case).
    """
    if len(history) < 2:
        return None, None, None

    (t0, p0), (t1, p1) = history[-2], history[-1]
    dt = t1 - t0
    if dt <= 0.0:
        return None, None, None

    vx = (p1.x - p0.x) / dt
    vy = (p1.y - p0.y) / dt
    velocity = WorldPoint(x=vx, y=vy)
    speed = math.hypot(vx, vy)
    heading_rad = math.atan2(vy, vx)
    return velocity, speed, heading_rad
