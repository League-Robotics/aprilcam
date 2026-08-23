"""Per-camera ring buffer plus one-burst-per-call detection orchestration.

The daemon-internal home for **when** detection runs and **what** it
remembers -- not vision logic itself (that stays in ``vision/pipeline.py``
and ``vision/detector.py``, both stateless and camera-agnostic). This
module is the first thing in the codebase that imports both ``camera/``
and ``vision/`` (sprint 021 architecture's "first time anything imports
[``vision/``]").

**Demand-driven, no background thread this sprint** (sprint.md
Architecture Decision 1). :meth:`DetectionEngine.run_burst` is called
directly by ``daemon.service.DaemonService.get_tags``/``get_tag`` --
once per call, synchronously -- and nothing else in this module ever
calls it. There is no loop, no timer, no thread: an idle daemon runs
zero detections by construction, not because a scheduler decided not to
run one. :meth:`DetectionEngine.history` and :meth:`DetectionEngine.latest`
are pure reads of already-buffered state and never call :meth:`run_burst`.

**One ``TagDetector`` per camera, reused across bursts.** Ticket 003's
handoff is explicit: building a ``TagDetector``'s per-family
``ArucoDetector`` objects is not free, so this engine builds at most one
per camera slug (lazily, on that camera's first burst) and reuses it for
every subsequent burst against that camera -- never one per call.

**Corner reordering happens here, and turns out to be a no-op.**
``vision/detector.py``'s module docstring records the empirically
verified finding (sprint.md Open Question 2): OpenCV's native corner
order for both AprilTag 36h11 and ArUco 4x4 is *already* identical,
index for index, to ``design.md`` Sec.6's boundary/hat order
(front-left, front-right, back-right, back-left) under the
canonical-print-orientation mounting convention. ``_pixel_corners``
below therefore carries ``Detection.corners`` straight into
``TagRecord.pixel_corners`` with no permutation -- the "reordering" this
module is responsible for is verified to require zero data movement,
not skipped by oversight.

**No ``-(N+1)`` encoding, anywhere.** ``TagId`` is built directly from
``Detection.family`` (already a typed ``TagFamily``) and
``Detection.number`` (already the family's native non-negative
dictionary index) -- sprint.md Architecture Decision 2. No code in this
module ever constructs or interprets a negative tag id.

**World coordinates, when a calibration exists (ticket 022-004).**
:meth:`DetectionEngine.run_burst` asks its injected
``calibration_lookup`` for *slug*'s current
``calibration.interfaces.Calibration`` on every burst -- never cached
across bursts, so a camera that becomes calibrated (or whose
calibration goes stale) mid-session is reflected on the very next call,
not just the next process restart. When one exists, :func:`_build_tag_record`
converts ``pixel``/``pixel_corners`` through ``Calibration.to_world``
to populate ``world``/``world_corners`` (preserving corner order --
never independently recomputed), derives ``yaw_rad`` from the observed
front edge (``world_corners[0]`` -> ``world_corners[1]``, the same
``atan2``, 0=east, CCW-positive convention as heading), and sets
``calibrated=True``. :meth:`run_burst` then hands this tag's
world-position history -- the ring buffer's already-buffered entries for
this exact tag, plus this burst's own new sample -- to
``vision.motion.compute_motion`` (sprint 021, inert until now) to fill in
``heading_rad``/``world_velocity``/``speed``, which stay ``None`` until a
second calibrated sample exists for that tag -- and (ticket 022-007,
real-hardware finding) ``heading_rad``/``world_velocity`` stay ``None``
even with a second sample if the two are within
``vision.motion.MIN_RELIABLE_DISPLACEMENT_CM`` of each other: a
physically stationary tag jitters by a small, real amount between
bursts, and that module's docstring works through why a direction
derived from jitter is worse than no direction at all, while ``speed``
alone is still safe to report honestly at that magnitude. **An
uncalibrated camera's path is completely untouched**: ``calibration_lookup``
returning ``None``
(the default, and every case before ticket 003's calibration engine
existed) reproduces sprint 021's output byte-for-byte -- ``world``,
``heading_rad``, ``world_velocity``, ``speed``, ``yaw_rad``, and
``world_corners`` all ``None``, ``calibrated=False``, only
``pixel``/``pixel_corners`` populated. This is a conditional branch
added around sprint 021's code, not a rewrite of it.

**Opportunistic staleness drift check (ticket 023-007, sprint.md
Decision 5).** :meth:`DetectionEngine.run_burst` is also where
``Calibration.stale`` becomes reachable rather than permanently
``False``: for a calibrated camera, at most once every
``drift_check_interval_s`` seconds, this method re-examines the
detections it *already has* (no independent frame read or detection
pass -- the whole point of piggybacking) for whichever of the
calibration's matched static ArUco markers happen to be in view, and
compares their reprojected position against the playfield definition's
surveyed ground truth (``calibration.calibration.compute_drift_cm``). A
measured drift beyond ``drift_tolerance_cm`` calls the injected
``mark_stale`` callable; too few matched markers visible (darkness,
occlusion, a covered field) is "cannot verify," never evidence of
drift, and marks nothing. See :meth:`DetectionEngine._maybe_check_drift`
for the full contract, and ``camera.camera.Camera``'s ``on_reopen``
hook for staleness' *other* trigger (sprint.md Decision 6, outside this
module entirely -- a reopen marks stale unconditionally, with no drift
measurement involved).

**Scope boundary closed for a camera with an active tag-stream
subscriber (sprint 027 ticket 003).** Sprint 021's own scope boundary
here used to read "query-driven, not wall-clock-bounded, this sprint"
(sprint.md Open Question 3): because a buffer only grew when
:meth:`run_burst` was called, and nothing called it except a live
``get_tags``/``get_tag`` request, "roughly 10 seconds" (UC-26) was not a
wall-clock guarantee -- a camera queried once a minute had a buffer that
spanned 300 minutes, not 300 frames' worth of seconds, before it
filled. ``daemon.streaming.TagStreamProducer`` (sprint 027 ticket 003)
is the "background/streaming producer" this note said to wait for: it
drives :meth:`run_burst` at a steady wall-clock cadence for as long as
at least one tag-stream subscriber exists for that camera, so the
buffer genuinely fills at that cadence, not only on demand, exactly as
UC-26's "~10 seconds" wants. **This remains query-driven, unchanged,
for any camera with no active tag-stream subscriber** -- a caller that
only ever uses one-shot ``get_tags``/``get_tag`` sees exactly sprint
021's original behaviour (sprint.md Design Rationale 3's stated
consequence: intentional, not a regression). This was never a defect,
only an accepted, honest consequence of shipping burst detection before
streaming (Decision 1).

**Mount correction (ticket 025-004, UC-12).** :meth:`DetectionEngine.run_burst`
also asks its injected ``mount_lookup`` for each confirmed tag's currently
registered ``types.MountParameters``, immediately after
:func:`_build_tag_record` has populated ``world``/``world_corners``/
``yaw_rad`` from calibration and *before* :meth:`_finish_record` derives
motion from history. When a registration exists, ``world``/``yaw_rad``
are replaced with ticket 003's ``calibration.geometry.correct_tag_pose``
result -- the *robot's* pose, not the tag's -- so every consumer of the
ring buffer (``get_tag``/``get_tags``/``get_tag_history``, and the
not-yet-built streams) sees the correction for free, with no per-call
logic duplicated anywhere else. ``world_corners`` is deliberately left
untouched: per ``types.TagRecord``'s own docstring, corners outline the
tag as physically seen (for drawing), never the robot. A tag with no
registration (:meth:`DetectionEngine._apply_mount_correction`'s
``mount_lookup(tag) is None`` branch) or with no calibrated ``world`` yet
is returned completely unchanged -- the default ``mount_lookup``
(:func:`_default_mount_lookup`) returns ``None`` for every tag, so this
whole mechanism is a true no-op for every existing caller/test that does
not opt in, exactly mirroring :func:`_default_calibration_lookup`'s own
regression guarantee.

**Camera-geometry caveat -- resolved (sprint 029 ticket 001).**
``correct_tag_pose``'s parallax half
(``calibration.geometry.project_tag_to_field_plane``) needs a camera's
own ground position and height above the field. ``config.interfaces.
CameraConfig`` now carries an optional ``camera_position``
(``config.interfaces.CameraPosition``: ``nadir``/``height_cm``),
plumbed through the same proto/config-loader/CLI-push path every other
camera setting uses (see ``calibration/geometry.py``'s module docstring
for the full history of this gap). This module's own
``camera_geometry_lookup`` still defaults to
:func:`_default_camera_geometry_lookup` -- the ground position ``(0,
0)``, height ``0.0`` stopgap -- for a bare :class:`DetectionEngine`
constructed with no real lookup injected (every test in this module
that never sets ``camera_geometry_lookup`` explicitly, plus any future
caller with no reason to need real geometry). A real daemon process
instead injects ``daemon.service.DaemonService._camera_geometry_for``,
which reads a configured camera's real ``camera_position`` when one
exists, falling back to this exact stopgap only for a camera nobody has
run ``aprilcam camera locate`` against (or hand-authored
``camera_position`` for) -- so a registered ``mount_z`` now has a real,
measurable effect against a configured live camera, not only against
hand-picked test geometry (``mount_x``/``mount_y``/``mount_yaw_rad``'s
offset+yaw composition needed no camera geometry and was already
verified working). A registered ``mount_z`` at or above a configured
camera's own height is guarded separately, in
:meth:`DetectionEngine._apply_mount_correction`: rather than
extrapolating past the camera, correction is skipped and the raw
position is reported.

**Presence-based temporal confirmation (ticket 024-006; streak raised to
3 by ticket 024-007's real-hardware run).** Before any ``Detection``
becomes a ``TagRecord``, :meth:`DetectionEngine.run_burst` requires it
to have been detected in ``confirmation_streak`` (default
:data:`CONFIRMATION_STREAK`, 3) strictly consecutive bursts for that
camera -- checked by tag id alone, never position, so a real
fast-moving tag whose reported position legitimately differs between
consecutive sightings still confirms normally. This exists because a
single false ArUco decode (observed live: ArUco 17, not on the field,
recurring at a *different* world position each time it happened) would
otherwise enter the ring buffer and, joined by the motion pipeline with
a second phantom sighting, be reported with a fabricated speed/heading
for an object that does not exist. Ticket 024-007's real-hardware run
against a static, clean ``main-playfield`` on camera 3 found **zero**
off-field ids in *confirmed* ``Daemon.get_tags`` output over 25 polls
at the then-current streak=2 -- no confirmed false accept was directly
observed in that sample. But a separate raw-detector sample (``run_
pipeline`` + ``TagDetector.detect`` called directly, bypassing
confirmation, on 40 fetched frames) found an off-field id in 6 of those
40 frames across four distinct phantom ids, and a follow-up
60-consecutive-frame streak-length analysis of that same raw stream
found one phantom persisting 2 strictly consecutive frames -- exactly
what streak=2 requires to confirm. The 25-poll confirmed sample simply
did not happen to land on that timing; the raw data shows it can occur.
The constant is raised to 3 on that boundary evidence -- one above the
longest phantom streak actually observed -- not on a directly-observed
confirmed false accept. See :data:`CONFIRMATION_STREAK`'s own docstring
and ticket 024-007's completion notes for the full real counts. An
unconfirmed detection is not reported with a pending flag -- it is
simply absent from that burst's ``TagFrame.tags``, exactly as if it had
not been detected at all, and therefore never reaches the ring buffer
or ``vision.motion.compute_motion`` either. See :meth:`DetectionEngine.
_confirm_detections` for the full mechanism and the presence-vs-position
rationale.

**Disappearance grace (ticket 034-001).** The streak above only ever
governs *appearance* -- becoming confirmed in the first place -- and is
unchanged by this addition. Once a tag id *is* confirmed, it now
survives up to ``confirmation_grace`` (default :data:`CONFIRMATION_GRACE`,
8) consecutive missed bursts before its confirmed status is dropped,
instead of dropping on the very next miss. This closes a real gap found
by the sprint 034 root-cause analysis (``clasi/sprints/034-detection-
confirmation-grace-for-marginal-tags/issues/apriltag-1-detects-
unreliably-on-arducam-ov9281-usb-camera.md``, 2026-08-23): a confirmed
tag with a marginal raw per-burst detection rate (AprilTag 1 on
``arducam-ov9281-usb-camera``, raw ~0.45-0.6) was being bounced back to
a fresh ``confirmation_streak`` on every single miss, collapsing its
*reported* rate to roughly the raw rate cubed (observed live: 1/12
polls) even though the tag was genuinely present most of the time. A
missed burst within the grace window never fabricates or re-emits a
record -- the tag is simply, honestly absent from that one
``TagFrame.tags``, exactly as an unconfirmed miss already is; only the
*confirmed status itself* survives the gap, so the tag's very next
genuine detection is reported immediately rather than paying the full
re-confirmation cost again. See :data:`CONFIRMATION_GRACE`'s own
docstring for the geometric argument behind its default, and
:meth:`DetectionEngine._confirm_detections` for the three-state
mechanism (``_streaks``/``_confirmed``/``_grace_counters``) that
implements both the appearance streak and the disappearance grace
without either affecting the other.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import replace
from typing import Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from aprilcam.calibration.calibration import Calibration as _ConcreteCalibration
from aprilcam.calibration.calibration import (
    DRIFT_CHECK_INTERVAL_S,
    DRIFT_TOLERANCE_CM,
    compute_drift_cm,
)
from aprilcam.calibration.geometry import correct_tag_pose
from aprilcam.calibration.interfaces import Calibration
from aprilcam.camera.interfaces import Camera
from aprilcam.config.interfaces import PlayfieldDefinition as PlayfieldDefinitionIface
from aprilcam.config.loader import PlayfieldDefinition as _ConcretePlayfieldDefinition
from aprilcam.types import (
    MountParameters,
    PixelPoint,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    WorldPoint,
)
from aprilcam.vision.detector import Detection, TagDetector
from aprilcam.vision.motion import compute_motion
from aprilcam.vision.pipeline import PipelineConfig, run_pipeline

__all__ = ["RING_BUFFER_SIZE", "CONFIRMATION_STREAK", "CONFIRMATION_GRACE", "DetectionEngine"]

#: v1's ``RingBuffer(maxlen=300)`` sizing (``archive/src/aprilcam/core/
#: detection.py``) -- roughly 10 seconds at 30 fps *when something fills
#: it at frame rate*. See the module docstring's scope-boundary note:
#: this sprint's buffer only grows on demand, so that "~10 seconds"
#: framing is v1's, not a guarantee this sprint makes.
RING_BUFFER_SIZE = 300

#: Number of strictly consecutive detection bursts a tag id must be
#: detected in before it enters any returned or buffered ``TagFrame`` --
#: ticket 024-006's phantom-detection fix. **Raised from 2 to 3 by
#: ticket 024-007's real-hardware acceptance run**: the provisional
#: value of 2 (chosen from thin evidence -- on the order of a dozen
#: real polls across two sessions, two phantom hits, zero consecutive)
#: was tested three ways against real camera 3 / the real, static,
#: clean main-playfield, daemon restarted onto the build under test.
#: (1) 25 polls of *confirmed* ``Daemon.get_tags`` output at the
#: then-current streak=2: zero off-field ids -- no confirmed false
#: accept was directly observed in this sample. (2) The *raw* detector
#: output (``run_pipeline`` + ``TagDetector.detect`` called directly,
#: bypassing confirmation entirely) on 40 fetched frames: 6 of 40
#: frames (15%) contained an off-field id, across four distinct phantom
#: ids -- ``aruco 37`` (x3), ``aruco 17`` (x1), ``aruco 22`` (x1),
#: ``aruco 16`` (x1). (3) A consecutive-streak analysis of 60 further
#: raw frames, tracking how many frames in a row each phantom id
#: persisted: 6 frames contained a phantom, streak-length histogram
#: ``{1: 4, 2: 1}`` -- four phantoms lasted a single frame, one
#: persisted across 2 strictly consecutive frames (the observed
#: maximum). Streak=2 confirms on exactly 2 consecutive detections, so
#: that observed 2-run sat exactly at the confirmation boundary: it
#: would have passed the streak=2 filter had it landed inside a polled
#: window instead of the un-polled raw stream sampled in (3). The
#: constant is raised to 3 on that boundary evidence -- one above the
#: longest phantom streak actually observed across (2) and (3) -- not
#: on a directly-observed confirmed false accept from (1). This is
#: still a bounded sample (60 consecutive frames); a longer real-
#: hardware campaign could in principle observe a 3-frame phantom
#: streak, which is exactly why this value stays constructor-injectable
#: rather than hardcoded (see ticket 024-007's completion notes for the
#: full real counts and the honest limits of this sample). Overridable
#: per instance via the constructor's ``confirmation_streak`` parameter,
#: matching ``drift_tolerance_cm``'s override pattern. See
#: :meth:`DetectionEngine._confirm_detections` for the mechanism.
CONFIRMATION_STREAK = 3

#: Number of consecutive missed bursts a tag id may go, **after** it has
#: already satisfied :data:`CONFIRMATION_STREAK` appearance confirmation,
#: before its confirmed status is dropped and it must re-earn the full
#: streak from scratch -- ticket 034-001's disappearance-grace fix (sprint
#: 034's root-cause issue, ``clasi/sprints/034-detection-confirmation-
#: grace-for-marginal-tags/issues/apriltag-1-detects-unreliably-on-
#: arducam-ov9281-usb-camera.md``, measurements dated 2026-08-23).
#: **The problem this closes.** Before this ticket, ``_confirm_detections``
#: had no disappearance side at all: a confirmed tag that missed even one
#: burst, for any reason, paid the exact same cost as a brand-new tag
#: first appearing. For AprilTag 1 on ``arducam-ov9281-usb-camera``, whose
#: raw per-burst detection rate measured ~0.45-0.6 (a real, marginal
#: physical property of that tag/camera combination, not a detector bug --
#: a byte-identical v1-vs-v2 detector comparison ruled that out), the
#: *reported* rate collapses to roughly the raw rate cubed under
#: ``CONFIRMATION_STREAK``'s 3-burst gate -- matching the issue's directly
#: observed 1 of 12 live polls. **The geometric argument for 8.** Treating
#: consecutive misses as independent, ``P(>= N consecutive misses while
#: the tag is still genuinely present) ~= q^N`` where ``q`` is the
#: per-burst miss probability. At the documented worst case ``q~=0.55``,
#: ``N=8`` gives ``q^8 ~= 1.1%``; at a more typical ``q~=0.5`` it is
#: ``q^8 ~= 0.4%`` -- both small enough that a marginal-but-present tag
#: almost never gets bounced back to full re-confirmation by chance, while
#: a genuinely-removed tag still leaves confirmed status within a handful
#: of bursts (fractions of a second at tag-stream cadence). This mirrors
#: :data:`CONFIRMATION_STREAK`'s own evidence-based-but-explicitly-
#: revisable framing exactly: 8 sits in the middle of the sprint's
#: considered 5-15 range (5 trades a slightly higher false-reset rate for
#: slightly faster genuine-disappearance detection; 15 does the reverse)
#: and is the smallest value that gets the documented worst case
#: comfortably under 2%. A longer real-hardware campaign could in
#: principle motivate a different value, exactly as ``CONFIRMATION_STREAK``
#: anticipates for itself -- which is why this stays constructor-
#: injectable rather than hardcoded, via the constructor's
#: ``confirmation_grace`` parameter. See :meth:`DetectionEngine.
#: _confirm_detections` for the mechanism: grace only ever preserves
#: confirmed *status* across a gap -- it never fabricates, re-emits, or
#: backdates a ``TagRecord`` for a burst the tag was not actually detected
#: in (sprint.md Design Rationale Decision 3).
CONFIRMATION_GRACE = 8

#: A callable that builds one ``TagDetector`` (or, in tests, a
#: duck-typed substitute exposing the same ``detect(gray, scale=...)``
#: method -- see ``tests/aprilcam/daemon/test_detection.py``'s
#: burst-counting doubles). Injectable so tests can pin exactly how many
#: times detection actually runs without needing real pixel data.
DetectorFactory = Callable[[], TagDetector]

#: A callable that resolves a camera slug -> its currently-held
#: ``Calibration``, or ``None`` if that camera has never been
#: calibrated -- ``daemon.service.DaemonService.get_calibration`` in
#: production (module docstring's "threaded through" section), a fixture
#: lookup or a plain fake in tests. Called fresh on every
#: :meth:`DetectionEngine.run_burst`, never cached, so a newly-completed
#: calibration takes effect on the very next burst.
CalibrationLookup = Callable[[str], Optional[Calibration]]

#: A callable that resolves a playfield *name* -> its
#: ``PlayfieldDefinition``, or ``None`` if the daemon does not currently
#: hold one by that name -- ``daemon.service.DaemonService.
#: _playfield_for_calibration`` in production, the same lookup
#: ``calibration.calibration.Calibrator`` already uses for the identical
#: reason (ticket 023-007's opportunistic drift check needs a matched
#: marker's *surveyed* position, exactly like a fresh solve does). Called
#: only when a drift check is actually due for a calibrated camera --
#: see :meth:`DetectionEngine._maybe_check_drift`.
PlayfieldLookup = Callable[[str], Optional[PlayfieldDefinitionIface]]

#: A callable that resolves a ``TagId`` -> its currently registered
#: ``types.MountParameters``, or ``None`` if no mount is registered for
#: that tag right now (ticket 025-004, UC-12). ``daemon.service.
#: DaemonService._mount_lookup`` in production (module docstring's
#: "mount correction" section), a plain dict lookup or lambda in tests.
#: Called once per confirmed detection, every burst -- never cached, so
#: a registration made between two bursts takes effect on the very next
#: one.
MountLookup = Callable[[TagId], Optional[MountParameters]]

#: A callable that resolves a camera slug -> ``(camera_ground_position,
#: camera_height_cm)``, the two inputs
#: ``calibration.geometry.project_tag_to_field_plane`` needs to do its
#: parallax projection (ticket 025-004). **Explicitly provisional** --
#: see the module docstring's "camera-geometry caveat" section and
#: :func:`_default_camera_geometry_lookup`'s own docstring for the full
#: reasoning; this is a stopgap seam for a real source, not a settled
#: design.
CameraGeometryLookup = Callable[[str], Tuple[WorldPoint, float]]

#: A callable invoked with a camera slug when the drift check (ticket
#: 023-007) has decided that camera's calibration should be marked
#: stale. ``daemon.service.DaemonService._mark_stale`` in production --
#: the single, ``self._lock``-guarded mutation point that also backs
#: ``Camera``'s ``on_reopen`` hook, so both staleness triggers
#: (sprint.md Decisions 5 and 6) share one implementation. Never called
#: directly by :class:`DetectionEngine` for any other reason.
MarkStale = Callable[[str], None]


def _default_detector_factory() -> TagDetector:
    return TagDetector()


def _default_calibration_lookup(slug: str) -> Optional[Calibration]:
    """No calibration subsystem wired in -- every camera is uncalibrated.

    The default for both :class:`DetectionEngine`'s own constructor and
    every pre-ticket-004 test/caller that never passed
    ``calibration_lookup`` at all: reproduces sprint 021's
    always-uncalibrated behaviour exactly (module docstring's regression
    guarantee).
    """
    return None


def _default_playfield_lookup(name: str) -> Optional[PlayfieldDefinitionIface]:
    """No playfield store wired in -- every drift check reports "cannot
    verify" and marks nothing stale. The default for both
    :class:`DetectionEngine`'s own constructor and every pre-ticket-007
    test/caller that never passed ``playfield_lookup`` -- combined with
    :func:`_default_calibration_lookup` always returning ``None``, this
    also means the drift check never runs at all for those callers
    (:meth:`DetectionEngine.run_burst` only invokes it for a calibrated
    camera in the first place)."""
    return None


def _default_mark_stale(slug: str) -> None:
    """No mutation point wired in -- a drift check that decides a camera
    has moved has nowhere to report that decision, so it is silently
    dropped. The default for both :class:`DetectionEngine`'s own
    constructor and every pre-ticket-007 test/caller that never passed
    ``mark_stale``."""
    return None


def _default_mount_lookup(tag: TagId) -> Optional[MountParameters]:
    """No mount registry wired in -- every tag reports its raw pose.

    The default for both :class:`DetectionEngine`'s own constructor and
    every pre-ticket-025-004 test/caller that never passed
    ``mount_lookup`` at all -- reproduces every earlier ticket's
    behaviour exactly (module docstring's regression guarantee): a tag
    with no registration is completely unaffected by this ticket's
    correction step.
    """
    return None


def _default_camera_geometry_lookup(slug: str) -> Tuple[WorldPoint, float]:
    """The constructor default for a bare :class:`DetectionEngine` with
    no real geometry source injected (module docstring's "camera-
    geometry caveat" section; ``calibration/geometry.py``'s module
    docstring has the full history of this gap, closed by sprint 029
    ticket 001).

    Returns ground position ``(0, 0)`` and height ``0.0`` for every
    camera. Height ``0.0`` is ``calibration.geometry.
    project_tag_to_field_plane``'s own documented "nothing to correct"
    sentinel (that function short-circuits to the identity whenever
    ``camera_height_cm`` is not strictly positive), so under this
    default a registered ``mount_z`` has **no effect** -- only
    ``mount_x``/``mount_y``/``mount_yaw_rad``'s offset+yaw composition
    (:func:`~aprilcam.calibration.geometry.apply_mount_offset`, which
    needs no camera geometry at all) actually moves a reported
    position. This is no longer what a real daemon process wires in:
    ``daemon.service.DaemonService`` injects its own
    ``_camera_geometry_for`` instead, which reads a configured camera's
    real ``camera_position`` when one exists and falls back to this
    exact stopgap only when one does not (see that method's own
    docstring). This function remains the default for
    :class:`DetectionEngine`'s own constructor and every test/caller
    that has no reason to inject real geometry.
    """
    return WorldPoint(x=0.0, y=0.0), 0.0


def _pixel_corners(
    corners: np.ndarray,
) -> Tuple[PixelPoint, PixelPoint, PixelPoint, PixelPoint]:
    """``Detection.corners`` (an ``(4, 2)`` array, native detector order)
    to ``TagRecord.pixel_corners`` -- verified equal to the boundary/hat
    order already (module docstring), so this is a plain type
    conversion, not a permutation."""
    pts = [PixelPoint(x=float(x), y=float(y)) for x, y in corners]
    return (pts[0], pts[1], pts[2], pts[3])


def _front_edge_yaw(
    world_corners: Tuple[WorldPoint, WorldPoint, WorldPoint, WorldPoint]
) -> float:
    """``yaw_rad`` from the observed front edge, in world space.

    ``design.md`` Sec.6 / ``types.TagRecord``'s corner-order contract:
    ``world_corners[0]`` is front-left, ``world_corners[1]`` is
    front-right, and the ``0 -> 1`` edge is the front edge the hat sits
    on. ``atan2`` over that edge vector, 0=east CCW-positive -- the same
    numeric convention ``vision.motion.compute_motion`` uses for
    ``heading_rad`` (``types.py``'s module docstring), applied here to
    orientation rather than motion.
    """
    front_left, front_right = world_corners[0], world_corners[1]
    return math.atan2(front_right.y - front_left.y, front_right.x - front_left.x)


def _build_tag_record(
    detection: Detection, *, timestamp: float, calibration: Optional[Calibration]
) -> TagRecord:
    """One raw ``Detection`` -> one ``TagRecord``.

    *calibration* ``None`` reproduces sprint 021's honestly-uncalibrated
    output exactly: ``world``/``heading_rad``/``world_velocity``/
    ``speed``/``yaw_rad``/``world_corners`` all ``None``, ``calibrated``
    ``False``, only ``pixel``/``pixel_corners`` populated.

    A real *calibration* populates ``world`` (via ``Calibration.to_world``
    on the detection's centre), ``world_corners`` (the same conversion
    applied to each of the four ``pixel_corners`` in order -- never
    independently recomputed), and ``yaw_rad`` (:func:`_front_edge_yaw`
    on those ``world_corners``), and sets ``calibrated=True``.
    ``heading_rad``/``world_velocity``/``speed`` are left ``None`` here
    regardless -- :meth:`DetectionEngine.run_burst` fills those in once
    it has assembled this tag's world-position history, which this
    module-level function has no access to (it builds one record from
    one detection, not a tag's history across bursts).
    """
    tag = TagId(family=detection.family, number=detection.number)
    pixel_corners = _pixel_corners(detection.corners)

    if calibration is None:
        return TagRecord(
            tag=tag,
            world=None,
            heading_rad=None,
            world_velocity=None,
            speed=None,
            timestamp=timestamp,
            pixel=detection.center,
            calibrated=False,
            yaw_rad=None,
            world_corners=None,
            pixel_corners=pixel_corners,
        )

    world = calibration.to_world(detection.center)
    world_corners = (
        calibration.to_world(pixel_corners[0]),
        calibration.to_world(pixel_corners[1]),
        calibration.to_world(pixel_corners[2]),
        calibration.to_world(pixel_corners[3]),
    )
    return TagRecord(
        tag=tag,
        world=world,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=timestamp,
        pixel=detection.center,
        calibrated=True,
        yaw_rad=_front_edge_yaw(world_corners),
        world_corners=world_corners,
        pixel_corners=pixel_corners,
    )


class DetectionEngine:
    """Owns every camera's ring buffer and runs one-shot detection bursts on demand.

    Keyed throughout by camera **slug** (``Camera.slug``), matching the
    key ``daemon.service.DaemonService`` already uses for its held-open
    camera cache -- callers pass the slug of an already-resolved,
    already-open ``Camera``; resolving *which* camera is
    ``DaemonService._camera_for``'s job, not this class's.

    Parameters
    ----------
    buffer_size:
        Per-camera ring buffer capacity. Defaults to
        :data:`RING_BUFFER_SIZE` (v1's sizing).
    pipeline_config:
        Passed straight through to ``vision.pipeline.run_pipeline`` on
        every burst. ``None`` uses that function's own default
        (``PipelineConfig()``).
    detector_factory:
        Builds one detector per camera, lazily, on that camera's first
        burst (see the module docstring's "one ``TagDetector`` per
        camera" section). Overridable so tests can substitute a
        call-counting double without needing real pixel data or a real
        ``cv2.aruco`` build.
    calibration_lookup:
        Resolves a camera slug -> its currently-held ``Calibration``, or
        ``None`` if uncalibrated (module docstring's "world coordinates,
        when a calibration exists" section). Defaults to
        :func:`_default_calibration_lookup` (always ``None``), which is
        exactly sprint 021's behaviour -- every existing caller that
        constructs a ``DetectionEngine`` without this parameter keeps
        producing honestly-uncalibrated records. ``daemon.service.
        DaemonService`` wires its own ``get_calibration`` in here for a
        real daemon process.
    playfield_lookup:
        Resolves a playfield *name* -> its ``PlayfieldDefinition``
        (ticket 023-007's opportunistic drift check, sprint.md
        Decision 5) -- the source of a matched marker's *surveyed*
        position, since :class:`~aprilcam.calibration.calibration.Calibration`
        itself carries no stored positions of its own (see
        :func:`~aprilcam.calibration.calibration.compute_drift_cm`'s
        docstring). Defaults to :func:`_default_playfield_lookup`
        (always ``None``), under which the drift check always reports
        "cannot verify" and marks nothing stale. ``daemon.service.
        DaemonService`` wires its own ``_playfield_for_calibration`` in
        here -- the same lookup ``calibration.calibration.Calibrator``
        already uses for the identical reason.
    mark_stale:
        Invoked with a camera slug when the drift check decides that
        camera's calibration should be marked stale (ticket 023-007).
        Defaults to :func:`_default_mark_stale` (a no-op). ``daemon.
        service.DaemonService`` wires its own ``_mark_stale`` in here --
        the single ``self._lock``-guarded mutation point every
        ``self._calibrations`` write beyond :meth:`~aprilcam.daemon.
        service.DaemonService.calibrate` goes through (sprint.md
        Decisions 5 and 6 share one implementation).
    drift_tolerance_cm:
        How far (cm) a matched marker's reprojected position may drift
        from its surveyed position before the drift check calls
        *mark_stale* (sprint.md Decision 5). Defaults to
        :data:`~aprilcam.calibration.calibration.DRIFT_TOLERANCE_CM`
        (2.0 cm, stakeholder-confirmed) but is genuinely overridable
        per instance, the same "domain-tuning call" shape
        ``calibration.calibration.Calibrator``'s
        ``max_reprojection_error_cm`` already establishes.
    drift_check_interval_s:
        The minimum time (seconds) between two drift checks for the
        same camera slug (sprint.md Decision 5, "at most once per 10
        seconds"). Defaults to :data:`~aprilcam.calibration.calibration.
        DRIFT_CHECK_INTERVAL_S` (10.0 s, stakeholder-confirmed),
        overridable the same way *drift_tolerance_cm* is -- tests use a
        small or zero value to avoid needing to wait on a real clock.
    confirmation_streak:
        Number of strictly consecutive detection bursts a tag id must be
        detected in, for a given camera, before it is built into a
        ``TagRecord`` and included in the returned/buffered ``TagFrame``
        at all (ticket 024-006's phantom-detection fix, raised from 2 to
        3 by ticket 024-007's real-hardware run). Defaults to
        :data:`CONFIRMATION_STREAK` (3) but is genuinely overridable per
        instance, the same "domain-tuning call" shape *drift_tolerance_cm*
        already establishes -- tests that are not themselves about
        confirmation pass ``confirmation_streak=1`` to keep today's
        single-burst-reports-immediately behaviour.
    confirmation_grace:
        Number of consecutive missed bursts a tag id may go **after**
        already satisfying *confirmation_streak* before its confirmed
        status is dropped and it must re-earn the full streak again from
        scratch (ticket 034-001's disappearance-grace fix -- module
        docstring's "Disappearance grace" section). Defaults to
        :data:`CONFIRMATION_GRACE` (8) but is genuinely overridable per
        instance, the same "domain-tuning call" shape *confirmation_streak*
        already establishes -- ``confirmation_grace=0`` reproduces exactly
        the pre-ticket instant-drop-on-any-miss behaviour. Appearance
        confirmation (*confirmation_streak*) is completely unaffected by
        this parameter: an id that never reaches *confirmation_streak*
        consecutive hits never enters ``_confirmed`` and is therefore
        never eligible for grace at all.
    mount_lookup:
        Resolves a ``TagId`` -> its currently registered
        ``types.MountParameters`` (ticket 025-004, UC-12), or ``None``
        if unregistered. Defaults to :func:`_default_mount_lookup`
        (always ``None``), which reproduces every earlier ticket's
        output exactly -- every existing caller/test that does not pass
        this keeps reporting the raw tag pose, unaffected. ``daemon.
        service.DaemonService`` wires its own ``_mount_lookup`` in here
        for a real daemon process.
    camera_geometry_lookup:
        Resolves a camera slug -> ``(camera_ground_position,
        camera_height_cm)`` for :func:`~aprilcam.calibration.geometry.
        project_tag_to_field_plane`'s parallax projection (ticket
        025-004). Defaults to :func:`_default_camera_geometry_lookup` --
        an **explicitly provisional stopgap** (see that function's own
        docstring and the module docstring's "camera-geometry caveat"
        section): no real per-camera position/height source exists yet
        anywhere in this codebase, so this parameter is a seam for a
        future ticket to wire a real one into, not a settled answer.
    """

    def __init__(
        self,
        *,
        buffer_size: int = RING_BUFFER_SIZE,
        pipeline_config: Optional[PipelineConfig] = None,
        detector_factory: DetectorFactory = _default_detector_factory,
        calibration_lookup: CalibrationLookup = _default_calibration_lookup,
        playfield_lookup: PlayfieldLookup = _default_playfield_lookup,
        mark_stale: MarkStale = _default_mark_stale,
        drift_tolerance_cm: float = DRIFT_TOLERANCE_CM,
        drift_check_interval_s: float = DRIFT_CHECK_INTERVAL_S,
        confirmation_streak: int = CONFIRMATION_STREAK,
        confirmation_grace: int = CONFIRMATION_GRACE,
        mount_lookup: MountLookup = _default_mount_lookup,
        camera_geometry_lookup: CameraGeometryLookup = _default_camera_geometry_lookup,
    ) -> None:
        self._buffer_size = buffer_size
        self._pipeline_config = pipeline_config
        self._detector_factory = detector_factory
        self._calibration_lookup = calibration_lookup
        self._playfield_lookup = playfield_lookup
        self._mark_stale = mark_stale
        self._drift_tolerance_cm = drift_tolerance_cm
        self._drift_check_interval_s = drift_check_interval_s
        self._mount_lookup = mount_lookup
        self._camera_geometry_lookup = camera_geometry_lookup
        self._confirmation_streak = confirmation_streak
        self._confirmation_grace = confirmation_grace
        self._buffers: Dict[str, Deque[TagFrame]] = {}
        self._detectors: Dict[str, TagDetector] = {}
        self._frame_counts: Dict[str, int] = {}
        self._last_drift_check: Dict[str, float] = {}
        self._streaks: Dict[str, Dict[TagId, int]] = {}
        self._confirmed: Dict[str, Set[TagId]] = {}
        self._grace_counters: Dict[str, Dict[TagId, int]] = {}
        self._lock = threading.Lock()

    def run_burst(self, slug: str, camera: Camera) -> TagFrame:
        """Run **exactly one** detection burst against *camera* and append it to *slug*'s buffer.

        The full pipeline: read one frame, run
        ``vision.pipeline.run_pipeline``, run *slug*'s (lazily built,
        reused) ``TagDetector``, filter the raw hits through
        :meth:`_confirm_detections` (ticket 024-006's temporal
        confirmation -- module docstring), build one ``TagRecord`` per
        *confirmed* hit, wrap into a ``TagFrame``, append to the ring
        buffer, and return that same ``TagFrame`` -- the one
        ``get_tags``/``get_tag`` need. A detection whose id was not also
        present in the immediately preceding burst never reaches
        ``TagRecord`` construction at all this call.

        Raises ``RuntimeError`` if the camera yields no frame this call
        (mirrors ``DaemonService.get_frame``'s existing "no frame
        available" contract exactly, for the same reason: a capture
        failure is not "zero tags detected" and must not be recorded as
        one -- no buffer entry is appended when this raises).

        When ``calibration_lookup(slug)`` returns a real ``Calibration``
        (module docstring), each hit's ``world``/``world_corners``/
        ``yaw_rad``/``calibrated`` are populated by :func:`_build_tag_record`,
        and this method then assembles that tag's world-position history
        -- every already-buffered entry for the same ``TagId`` with a
        non-``None`` ``world``, oldest to newest, plus this burst's own
        new sample last -- and hands it to
        ``vision.motion.compute_motion`` to fill in
        ``heading_rad``/``world_velocity``/``speed``. A tag with no prior
        calibrated history (its first calibrated sighting) gets a
        one-entry history, so ``compute_motion`` correctly reports all
        three as ``None`` rather than a fabricated zero (its own
        "insufficient history" contract, sprint 021). An uncalibrated
        camera skips this step entirely: ``record.world is None`` short-
        circuits before any history is gathered or ``compute_motion`` is
        ever called, leaving sprint 021's path untouched.

        **Opportunistic drift check (ticket 023-007, sprint.md
        Decision 5).** When *slug* is calibrated, this method also asks
        :meth:`_maybe_check_drift` whether a drift check is due (at most
        once every ``self._drift_check_interval_s`` seconds per camera)
        and, if so, re-examines *this same burst's* ``detections`` -- no
        independent frame read or detection pass, preserving the
        idle-efficiency budget by construction: a camera with no
        calibration, or simply never queried, never reaches this step at
        all. The returned ``TagFrame.calibration_stale`` reflects
        whatever :attr:`_calibration_lookup` reports for *slug*
        immediately afterwards, so a drift check (or a concurrent
        ``Camera.on_reopen`` firing on another thread) that just flipped
        the calibration stale is reflected on the very same burst that
        found it, not the next one.
        """
        frame = camera.read()
        if frame is None:
            raise RuntimeError(f"no frame available from camera {slug!r}")

        gray, rescale = run_pipeline(frame, self._pipeline_config)
        detector = self._detector_for(slug)
        detections = detector.detect(gray, scale=rescale.scale)
        confirmed = self._confirm_detections(slug, detections)

        timestamp = time.time()
        calibration = self._calibration_lookup(slug)
        if calibration is not None:
            # Drift-check geometry uses *detections* (this burst's raw
            # hits), not *confirmed* -- ticket 023-007's matched corner
            # markers are a separate, already-trusted mechanism (a
            # camera's calibration only exists because those ids were
            # already matched during calibration), and gating the drift
            # check on confirmation would only add reporting latency to
            # staleness detection for no benefit: a phantom id is never
            # one of ``calibration.matched_ids`` in the first place, so
            # confirmation status of the corner markers is orthogonal to
            # this ticket's fix.
            self._maybe_check_drift(slug, calibration, detections, timestamp)
            calibration = self._calibration_lookup(slug)  # re-fetch: may have just been marked stale

        records = tuple(
            self._finish_record(
                self._apply_mount_correction(
                    _build_tag_record(detection, timestamp=timestamp, calibration=calibration),
                    slug=slug,
                ),
                slug=slug,
            )
            for detection in confirmed
        )
        tag_frame = TagFrame(
            tags=records,
            timestamp=timestamp,
            frame_index=self._next_frame_index(slug),
            calibration_stale=calibration.stale if calibration is not None else False,
        )
        self._append(slug, tag_frame)
        return tag_frame

    def _confirm_detections(
        self, slug: str, detections: Sequence[Detection]
    ) -> Tuple[Detection, ...]:
        """Presence-based temporal confirmation (ticket 024-006, streak
        raised to 3 by ticket 024-007's real-hardware run) plus
        disappearance grace (ticket 034-001).

        Filters *detections* down to only the ids that are currently
        *confirmed for* and *actually present in* this exact burst,
        updating three pieces of per-slug, per-id state as a side effect:
        an appearance-building streak (``self._streaks``, used only
        while an id is not yet confirmed), the set of currently-confirmed
        ids (``self._confirmed``), and a consecutive-miss counter for
        each confirmed id (``self._grace_counters``). Called exactly once
        per :meth:`run_burst`, before any ``TagRecord`` is built -- an id
        that ends this method neither confirmed nor present this burst
        never reaches :func:`_build_tag_record` and therefore never
        enters the returned ``TagFrame`` or the ring buffer. It is not
        reported with a pending flag; it is simply absent, exactly as if
        it had not been detected at all.

        **Presence, never position -- the load-bearing design choice.**
        The only question ever asked, for both appearance and
        disappearance, is "was this exact ``TagId`` also present in the
        immediately preceding burst for this camera" -- never *where* it
        was. The bug this exists to fix (a single false ArUco decode,
        observed live as ArUco 17 -- not on the field -- recurring at a
        *different* world position each time: ``(-59.4, -19.6)`` in one
        session, ``(50.1, -30.3)`` in another) rules out a
        same-place-twice rule: that would catch this particular phantom,
        but would just as reliably reject a real, fast-moving robot tag
        whose reported position legitimately differs between two
        consecutive sightings simply because it moved. Checking presence
        only satisfies that mobile-tag constraint by construction, and is
        simpler to reason about besides. Do not add a position-similarity
        check here -- it would look like a reasonable extra safeguard and
        would actually reintroduce the exact bug this design avoids
        (ticket 024-006).

        **Appearance (unchanged).** An id detected this burst that is
        *not yet* confirmed increments its building streak by exactly 1
        from whatever it was last burst (0 if this is its first sighting,
        or if a prior gap already dropped it); once that streak reaches
        ``self._confirmation_streak`` the id is promoted into
        ``self._confirmed``. An id that is *not yet* confirmed and *not*
        detected this burst is dropped from the building streak entirely
        -- exactly today's behaviour -- so a gap of even one burst during
        build-up restarts appearance at a fresh streak of 1 on the next
        sighting, never a resumed count (accepted trade-off, sprint.md
        Migration Concerns).

        **Disappearance grace (new, ticket 034-001).** An id detected
        this burst that *is already* confirmed stays confirmed and has
        its grace (miss) counter reset to 0 -- a real redetection always
        clears any accumulated grace. An id that *is* confirmed but *not*
        detected this burst has its grace counter incremented; it
        *remains* confirmed -- silently absent from just this one
        burst's output, never a fabricated or re-emitted record -- as
        long as the incremented count is ``<= self._confirmation_grace``
        (default :data:`CONFIRMATION_GRACE`, 8); the instant that bound
        is exceeded the id is dropped back to fully unconfirmed, with no
        partial credit carried into a future streak. ``confirmation_grace
        == 0`` reproduces the pre-ticket instant-drop-on-any-miss
        behaviour exactly, since a single miss already exceeds a bound of
        0. Once confirmed, a steadily-redetected id stays confirmed (and
        reported) on every further consecutive burst -- there is no
        re-drop once past the threshold, on either the streak or the
        grace side.

        The final confirmed-and-reported set for this burst is
        ``self._confirmed`` (after the above updates) intersected with
        this burst's own raw ids -- i.e. a ``TagRecord`` is only ever
        built from a detection that is *both* currently confirmed *and*
        genuinely present in this exact burst. A grace burst therefore
        never builds a record, never re-emits a stale one, and never
        touches the ring buffer for the tag it is being lenient about
        (sprint.md Design Rationale Decision 3).
        """
        ids_this_burst: Set[TagId] = {
            TagId(family=detection.family, number=detection.number) for detection in detections
        }
        with self._lock:
            previous_streaks = self._streaks.get(slug, {})
            previous_confirmed = self._confirmed.get(slug, set())
            previous_grace = self._grace_counters.get(slug, {})

            new_streaks: Dict[TagId, int] = {}
            new_confirmed: Set[TagId] = set()
            new_grace: Dict[TagId, int] = {}

            for tag_id in ids_this_burst:
                if tag_id in previous_confirmed:
                    # Already confirmed and genuinely redetected -- stays
                    # confirmed; grace resets to 0 (omitted from
                    # new_grace, whose .get default is 0).
                    new_confirmed.add(tag_id)
                    continue
                # Not yet confirmed: keep building the appearance streak.
                streak = previous_streaks.get(tag_id, 0) + 1
                if streak >= self._confirmation_streak:
                    new_confirmed.add(tag_id)
                else:
                    new_streaks[tag_id] = streak

            for tag_id in previous_confirmed - ids_this_burst:
                # Confirmed but missing this burst -- disappearance grace.
                misses = previous_grace.get(tag_id, 0) + 1
                if misses <= self._confirmation_grace:
                    new_confirmed.add(tag_id)
                    new_grace[tag_id] = misses
                # else: grace exceeded -- dropped entirely, no partial
                # credit carried into new_streaks.

            self._streaks[slug] = new_streaks
            self._confirmed[slug] = new_confirmed
            self._grace_counters[slug] = new_grace

        confirmed_ids = new_confirmed & ids_this_burst
        return tuple(
            detection
            for detection in detections
            if TagId(family=detection.family, number=detection.number) in confirmed_ids
        )

    def _maybe_check_drift(
        self,
        slug: str,
        calibration: Calibration,
        detections: Sequence[Detection],
        now: float,
    ) -> None:
        """Ticket 023-007's opportunistic drift check body -- called only
        from :meth:`run_burst`, only when *slug* is already calibrated.

        Rate-limited to at most once every ``self._drift_check_interval_s``
        seconds per camera slug, tracked in :attr:`_last_drift_check`
        (locked, since concurrent bursts against the same camera are real
        -- ``daemon/rpc.py``'s server runs a ``ThreadPoolExecutor``). The
        rate limit consumes its slot on every *attempt*, whether or not
        the attempt could actually verify anything -- a camera sitting in
        the dark still only gets checked once per interval, not on every
        single burst, which matters just as much for the idle-efficiency
        budget as for a calibrated-and-queried one.

        *detections* is *this burst's* own detection results -- already
        read, already run through the detector for tag tracking; this
        method performs no I/O and no additional detection pass of its
        own, only filters and geometry (module docstring's "piggybacks on
        frames already being read").

        Calls :attr:`_mark_stale` at most once, only when
        :func:`~aprilcam.calibration.calibration.compute_drift_cm`
        returns a real measurement (never ``None`` -- "cannot verify" is
        never treated as evidence of drift) exceeding
        ``self._drift_tolerance_cm``.

        *calibration*'s declared type is ``calibration.interfaces.
        Calibration`` (this module's injection surface types against the
        interface throughout, matching *camera*'s own ``camera.
        interfaces.Camera`` typing) -- but ``compute_drift_cm`` needs
        ``matched_ids``, which only the concrete ``calibration.
        calibration.Calibration`` declares. The ``isinstance`` check
        below narrows to that concrete type, mirroring
        ``Calibrator.calibrate``'s identical defensive check for
        ``PlayfieldDefinition``: every calibration this module is ever
        actually handed (``daemon.service.DaemonService.get_calibration``
        in production, every test fixture) already is one, so this
        never fires in practice -- it exists so a hypothetical future
        ``Calibration`` implementation fails this check safely (`cannot
        verify`) instead of an ``AttributeError``.
        """
        with self._lock:
            last = self._last_drift_check.get(slug)
            if last is not None and now - last < self._drift_check_interval_s:
                return
            self._last_drift_check[slug] = now

        if not isinstance(calibration, _ConcreteCalibration):
            return  # cannot verify -- no matched_ids to check drift against

        definition = self._playfield_lookup(calibration.playfield)
        if not isinstance(definition, _ConcretePlayfieldDefinition):
            return  # cannot verify -- never treated as evidence of drift

        detected = {
            detection.number: detection.center
            for detection in detections
            if detection.family == TagFamily.ARUCO and detection.number in calibration.matched_ids
        }
        drift_cm = compute_drift_cm(calibration, definition, detected)
        if drift_cm is None:
            return  # too few matched markers visible -- cannot verify
        if drift_cm > self._drift_tolerance_cm:
            self._mark_stale(slug)

    def _apply_mount_correction(self, record: TagRecord, *, slug: str) -> TagRecord:
        """Ticket 025-004, UC-12: replace *record*'s ``world``/``yaw_rad``
        with the *robot's* pose when a mount is registered for its tag,
        called immediately after :func:`_build_tag_record`, before
        :meth:`_finish_record` derives motion from history -- so a
        registered tag's velocity/heading are computed from the robot's
        own trajectory, not the tag's (module docstring's "mount
        correction" section).

        A no-op (returns *record* unchanged) in any of three cases, the
        first two required by this ticket's acceptance criteria and the
        third added by sprint 029 ticket 001:

        * ``record.world is None`` -- an uncalibrated hit has nothing to
          correct (mirrors :meth:`_finish_record`'s identical guard).
        * ``self._mount_lookup(record.tag)`` returns ``None`` -- no
          registration exists for this tag, so it is **unaffected**, by
          construction, regardless of any other tag's registration.
        * the looked-up ``camera_height_cm`` is a real configured value
          (``> 0``) and ``mount.mount_z >= camera_height_cm`` -- an
          implausible mount at or above the observing camera's own
          height. ``project_tag_to_field_plane``'s ``ratio =
          mount_z_cm / camera_height_cm`` would be ``>= 1``,
          extrapolating the corrected position *past* the camera's own
          nadir -- a plausible-looking but physically nonsensical
          answer, exactly the "silently wrong" failure mode this
          codebase treats as worse than an explicit failure. This is a
          per-burst, per-observing-camera check (sprint.md's Design
          Rationale explains why it lives here rather than at
          ``register_tag`` time): registration itself always succeeds
          regardless of ``mount_z``, and never raises mid-burst.

        ``record.world_corners`` is never touched here -- per
        ``types.TagRecord``'s own docstring, corners outline the tag as
        physically seen (for drawing), never the robot; only
        ``world``/``yaw_rad`` are mount-corrected.
        """
        if record.world is None:
            return record
        mount = self._mount_lookup(record.tag)
        if mount is None:
            return record
        assert record.yaw_rad is not None, (
            "a calibrated TagRecord (record.world is not None) always has "
            "yaw_rad populated by _build_tag_record -- a DetectionEngine bug otherwise"
        )
        camera_ground_position, camera_height_cm = self._camera_geometry_lookup(slug)
        if camera_height_cm > 0.0 and mount.mount_z >= camera_height_cm:
            return record
        corrected_world, corrected_yaw = correct_tag_pose(
            record.world,
            record.yaw_rad,
            mount,
            camera_ground_position=camera_ground_position,
            camera_height_cm=camera_height_cm,
        )
        return replace(record, world=corrected_world, yaw_rad=corrected_yaw)

    def _finish_record(self, record: TagRecord, *, slug: str) -> TagRecord:
        """Fill in *record*'s motion fields from its tag's world-position
        history, when it has one (``run_burst``'s docstring).

        A no-op (returns *record* unchanged) for an uncalibrated hit --
        ``record.world is None`` is exactly ``_build_tag_record``'s
        signal that no calibration applied, and motion is meaningless
        without world positions.
        """
        if record.world is None:
            return record

        history = self._history_for_tag(slug, record.tag)
        history.append((record.timestamp, record.world))
        velocity, speed, heading_rad = compute_motion(history)
        return replace(record, heading_rad=heading_rad, world_velocity=velocity, speed=speed)

    def _history_for_tag(self, slug: str, tag: TagId) -> List[Tuple[float, WorldPoint]]:
        """*tag*'s already-buffered ``(timestamp, world)`` samples for
        *slug*, oldest to newest -- the ring buffer as it stands
        **before** this burst's new ``TagFrame`` is appended (``_append``
        runs after every record in the new burst is finished). Only
        entries with a non-``None`` ``world`` count: a buffered frame
        from before this camera was calibrated (or a frame where this
        tag was uncalibrated for some other reason) cannot contribute a
        world position to a finite-difference velocity.
        """
        with self._lock:
            buf = self._buffers.get(slug)
            frames = tuple(buf) if buf is not None else ()
        return [
            (frame_record.timestamp, frame_record.world)
            for frame in frames
            for frame_record in frame.tags
            if frame_record.tag == tag and frame_record.world is not None
        ]

    def history(self, slug: str) -> Tuple[TagFrame, ...]:
        """Every buffered ``TagFrame`` for *slug*, oldest to newest.

        A pure read -- never calls :meth:`run_burst`. A camera with no
        buffer yet (never had a burst run against it) returns an empty
        tuple, not an error -- UC-26's "buffer not yet full" failure
        mode, degenerate case n=0.
        """
        with self._lock:
            buf = self._buffers.get(slug)
            return tuple(buf) if buf is not None else ()

    def latest(self, slug: str) -> Optional[TagFrame]:
        """The most recent buffered ``TagFrame`` for *slug*, or ``None``
        if its buffer is empty or does not exist yet.

        Used by ``DaemonService.get_frame`` to embed the latest
        detection result without itself triggering a burst (sprint.md
        Decision 4) -- the ``None``/populated distinction here is
        exactly ``ImageFrame.tags``'s "``None`` means detection is off"
        contract.
        """
        with self._lock:
            buf = self._buffers.get(slug)
            if not buf:
                return None
            return buf[-1]

    # -- internals -------------------------------------------------------

    def _detector_for(self, slug: str) -> TagDetector:
        with self._lock:
            detector = self._detectors.get(slug)
            if detector is None:
                detector = self._detector_factory()
                self._detectors[slug] = detector
            return detector

    def _append(self, slug: str, tag_frame: TagFrame) -> None:
        with self._lock:
            buf = self._buffers.get(slug)
            if buf is None:
                buf = deque(maxlen=self._buffer_size)
                self._buffers[slug] = buf
            buf.append(tag_frame)

    def _next_frame_index(self, slug: str) -> int:
        with self._lock:
            index = self._frame_counts.get(slug, 0)
            self._frame_counts[slug] = index + 1
            return index
