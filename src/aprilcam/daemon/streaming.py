"""``TagStreamProducer`` -- idle-gated, change-driven, rate-capped,
heartbeated background tag detection for one camera's tag stream
(sprint 027 ticket 003, UC-11/SUC-001/SUC-004).

**Thin scheduler around ``DetectionEngine.run_burst``, not a parallel
detection path.** Exactly the same call ``DaemonService.get_tags``
already makes, one-shot, per RPC -- this module just calls it
repeatedly, on a timer, for as long as at least one subscriber exists.
No vision logic lives here (module boundary unchanged from
``daemon/detection.py``'s own).

**Idle gating mirrors ``DetectionEngine``'s own declared invariant,
extended from "no loop, no timer" per burst to "no thread" per
stream.** ``daemon/detection.py``'s module docstring: "there is no
loop, no timer, no thread: an idle daemon runs zero detections by
construction, not because a scheduler decided not to run one." This
module makes the identical claim true for a *persistent* background
loop: :class:`TagStreamProducer` does not exist as a running thread
with zero subscribers (sprint.md Design Rationale 2) -- wired through
``daemon.broadcast.Broadcaster``'s ``on_active``/``on_idle`` 0<->1
transition hooks (ticket 001), which is *the* idle-gating mechanism,
not a policy this class re-implements. By default a
:class:`TagStreamProducer` owns and wires its own :class:`~aprilcam.
daemon.broadcast.Broadcaster` this way (see :attr:`TagStreamProducer.
broadcaster`); a caller that wants extra bookkeeping around the same
transitions (``daemon.service.DaemonService`` tracking every live
producer for shutdown, ticket 003's own acceptance criterion) passes
its own pre-wired ``Broadcaster`` in instead.

**No sentinel/close-signalling on the queue itself (ticket 001's
completion notes).** This module's background loop parks in a bounded
wait against an internal ``threading.Event`` between ticks -- the same
"bounded ``queue.get(timeout=...)``/wait against a stop flag" pattern
v1's ``_sender_loop`` (``archive/src/aprilcam/daemon/stream.py``) used
-- rather than blocking uninterruptibly, so :meth:`TagStreamProducer.stop`
can join the thread promptly instead of merely signalling it (this
ticket's own acceptance criterion).

**Heartbeat mechanism (UC-11.S-2): republish the latest, unchanged
``TagFrame`` once the heartbeat interval elapses since the last
publish.** ``run_burst`` already returns a *fresh* ``TagFrame`` (a new
``timestamp``/``frame_index``) on every tick, whether or not its
``.tags`` content differs from what was last published -- so "a
heartbeat" needs no separate message shape or sentinel value at all: it
is simply publishing that same-content frame when nothing has changed
recently enough to have published for its own sake. This is the
"simplest reading" the ticket named up front, chosen over inventing a
distinct heartbeat payload.

**Change threshold: world-cm when calibrated, pixel-space fallback
when not (sprint.md Design Rationale 5) -- decided per tag record, not
per frame.** Each :class:`~aprilcam.types.TagRecord` already carries
its own ``world``/``pixel`` populated-or-not signal
(``daemon/detection.py``'s module docstring); comparing in whichever
space *that record* actually has avoids needing this module to ask a
separate "is this camera calibrated" question of its own (no direct
``calibration/`` import needed, exactly as the ticket's implementation
plan anticipates -- ``calibration_stale`` and calibration state both
ride in on the ``TagFrame``/``TagRecord`` ``run_burst`` already
returns).

* **World-cm threshold** (:data:`TAG_STREAM_CHANGE_THRESHOLD_CM`):
  reuses ``vision.motion.MIN_RELIABLE_DISPLACEMENT_CM`` (0.5 cm) rather
  than inventing a fresh, untested number. That constant already
  answers the adjacent question "how much world-position displacement
  between two samples of the same tag is real motion, not per-frame
  reprojection noise" (derived from ticket 022-003's real ~0.18-0.19 cm
  RMS reprojection error measurement, doubled for margin -- see
  ``vision/motion.py``'s module docstring for the full derivation).
  "Did this tag's position change enough to be worth telling a
  subscriber about" is close enough to that same question that reusing
  the constant is more honest than picking an independent guess this
  sprint has no real-hardware means to tune (sprint.md Open Question 1)
  -- and it keeps exactly one place in the codebase making the
  noise-floor call, rather than two that could silently drift apart.
* **Pixel-space threshold** (:data:`TAG_STREAM_CHANGE_THRESHOLD_PX`):
  carried forward unchanged from v1's ``TagStreamProducer.
  change_threshold_px`` default (``archive/src/aprilcam/daemon/
  stream.py``, 8.0 px) -- v1 never had world coordinates at all, so
  this is the only precedent for the uncalibrated fallback path.
* **A tag entering or leaving the visible set is always a change**,
  regardless of threshold -- matches v1's ``_has_changed`` exactly (a
  differing id set short-circuits to "changed" before any distance is
  computed).

**Rate cap** (:data:`TAG_STREAM_MAX_PUBLISH_HZ`): carried forward
unchanged from v1's ``TagStreamProducer.max_hz`` default (20.0). At
this module's own provisional detection cadence
(:data:`TAG_STREAM_INTERVAL_S`, ~30 Hz) the cap is genuinely binding --
persistent per-tick change would otherwise publish faster than 20 Hz.
Kept as its own independent, overridable constant (not derived from
the cadence) so a future cadence change does not silently change the
publish rate cap as a side effect.

**Detection cadence** (:data:`TAG_STREAM_INTERVAL_S`): an honestly
provisional constant (sprint.md Open Question 1, the same
"starting-guess, not tuned" status ``DEFAULT_POLL_HZ``/
``CONFIRMATION_STREAK`` had before real-hardware evidence existed for
them) -- real-hardware tuning is blocked this sprint (the field is dark
and obstructed). Set to match ``daemon/detection.py``'s own
:data:`~aprilcam.daemon.detection.RING_BUFFER_SIZE` docstring, which
already assumes "roughly 10 seconds at 30 fps" -- choosing ~30 Hz here
is what actually makes that assumption true once a stream subscriber
exists, rather than picking an unrelated new number. Not validated
against real detection CPU cost (blocked, same reason); revisit once
the field is clear.

**Wall-clock-bounded ring buffer, closing sprint 021's scope boundary
(sprint.md Design Rationale 3, UC-26).** ``DetectionEngine.run_burst``
appends to its per-camera ring buffer on *every* call regardless of
this module's own publish decision -- so as long as a
:class:`TagStreamProducer` thread is alive (i.e. at least one
subscriber exists), the buffer grows at this module's own steady
wall-clock cadence, not only when something happens to poll
``get_tags``/``get_tag``. A camera with no active tag-stream subscriber
is completely unaffected: its buffer stays exactly as query-driven as
sprint 021 left it (``daemon/detection.py``'s own updated docstring
note).

----------------------------------------------------------------------

``ImageStreamProducer`` -- idle-gated background capture(+warp)+encode
for one (camera, deskewed) pair's image stream (sprint 027 ticket 004,
UC-07/SUC-002/SUC-004).

**Thin scheduler around the same routines ``DaemonService.get_frame``/
``_get_deskewed_frame`` already call, not a second implementation of
them.** The raw branch is ``camera.read()`` + ``cv2.imencode`` exactly
as ``get_frame(deskewed=False)`` does; the deskewed branch reuses
``calibration.deskew.Deskewer`` -- the *exact same class*
``_get_deskewed_frame`` constructs -- for the warp itself. Nothing here
reimplements the homography or the JPEG encode.

**One producer per (camera, deskewed) key is what makes "two
subscribers, one warp" true -- structurally, not by a dedup check.**
``daemon.service.DaemonService.stream_images`` keys its producer cache
by ``(slug, deskewed)`` (mirroring ticket 003's per-slug
``_tag_stream_producers`` cache, one dimension wider): two calls for
the *same* pair reuse the *same* :class:`ImageStreamProducer`, so N
subscribers to it share one background loop calling
``Deskewer.deskew`` once per tick and one :class:`~aprilcam.daemon.
broadcast.Broadcaster` fanning the *same* published ``ImageFrame``
object out to every subscriber's queue -- there is only ever one thing
to compute per tick, the same structural argument
``daemon/broadcast.py``'s own module docstring makes. Raw and deskewed
are independent producers (different key), so subscribing to one never
starts or affects the other.

**Streams are always JPEG** (``daemon.interfaces.Daemon.get_frame``'s
own docstring: "streams are JPEG, where per-frame loss is the accepted
trade") -- unlike ``get_frame``'s one-shot ``encoding`` parameter,
:class:`ImageStreamProducer` has no encoding choice to make.

**No change-detection, no heartbeat, no rate cap -- unlike
:class:`TagStreamProducer`.** ``sprint.md``'s SUC-002 main flow says
plainly: capture, (warp,) encode, publish, "at a steady cadence" --
every tick publishes unconditionally. Tag streams need change-driven
publish because a robot control loop cares about *positions*, where
"nothing changed" is real signal; an image stream is frames, where the
steady cadence itself is the contract (a viewer wants the next frame,
not a decision about whether this one was interesting).

**Frame geometry is snapshotted once per stream life, not
recomputed per tick (UC-07's "frame geometry stable for the life of a
stream" contract, this ticket's AC4).** ``Deskewer`` itself already
computes ``output_size``/``px_per_cm``/``world_extent`` once, at
*its own* construction, and never recomputes them
(``calibration/deskew.py``'s own docstring) -- this class's
contribution is constructing exactly **one** ``Deskewer`` per
:meth:`start` (the 0->1 subscriber transition that begins a stream's
life) and reusing that same instance for every tick until :meth:`stop`,
rather than building a fresh one from whatever the calibration/
playfield stores currently say. Concretely: a ``set_playfield`` push
that replaces the linked playfield's definition (different
``width_cm``/``height_cm``) mid-stream has **zero** effect on an
already-running stream's ``px_per_cm``/``world_extent`` -- the new
dimensions only take effect on the *next* stream (a fresh
:meth:`start` after the last subscriber left and a new one arrived),
exactly mirroring one-shot ``get_frame``'s own "one call, one
snapshot" behaviour extended across a whole stream's life instead of
one call. ``calibration_stale`` is deliberately **not** part of this
snapshot -- it is read fresh from ``calibration_lookup`` on every tick,
so a mid-stream staleness flip still reaches the very next published
frame (this ticket's own acceptance criterion) without disturbing the
frozen geometry.

**Preconditions are checked before a producer is ever created, never
inside the background thread.** ``daemon.service.DaemonService.
stream_images`` raises ``NotCalibrated`` for a deskewed request against
an uncalibrated camera (or one whose calibrated-against playfield is no
longer held) *before* constructing or reusing an
:class:`ImageStreamProducer` -- the same "checked before any hardware
access, never a raw frame silently substituted" discipline
``_get_deskewed_frame`` already established for the one-shot path
(module docstring of ``daemon/service.py``). A deskewed stream request
therefore either raises synchronously, in the caller's own thread, or
succeeds with a calibration that stays valid for the rest of the
process's life (calibration is never un-set, only marked stale) -- the
background thread's own calibration/playfield lookup at :meth:`start`
is consequently a should-not-fail path, guarded defensively (logged and
retried next tick) rather than left to crash the thread.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, Optional, Tuple

import cv2 as cv
import numpy as np
import queue

from aprilcam.calibration.calibration import Calibration
from aprilcam.calibration.deskew import Deskewer
from aprilcam.camera.interfaces import Camera, CameraLifecycle
from aprilcam.config.interfaces import PlayfieldDefinition
from aprilcam.daemon.broadcast import DEFAULT_SUBSCRIBER_QUEUE_DEPTH, Broadcaster
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.daemon.interfaces import ImageStream, TagStream
from aprilcam.types import AnnotationSet, ImageFrame, TagFrame, TagId
from aprilcam.vision.motion import MIN_RELIABLE_DISPLACEMENT_CM

__all__ = [
    "TagStreamProducer",
    "BroadcasterTagStream",
    "TAG_STREAM_INTERVAL_S",
    "TAG_STREAM_HEARTBEAT_INTERVAL_S",
    "TAG_STREAM_MAX_PUBLISH_HZ",
    "TAG_STREAM_CHANGE_THRESHOLD_CM",
    "TAG_STREAM_CHANGE_THRESHOLD_PX",
    "ImageStreamProducer",
    "BroadcasterImageStream",
    "IMAGE_STREAM_INTERVAL_S",
]

logger = logging.getLogger(__name__)

#: How often :class:`TagStreamProducer` calls ``DetectionEngine.run_burst``
#: while at least one subscriber exists -- see the module docstring's
#: "Detection cadence" section for the full provenance/honesty note.
#: Overridable per instance (``TagStreamProducer(..., interval_s=...)``).
TAG_STREAM_INTERVAL_S = 1.0 / 30.0

#: Maximum time (seconds) between two published frames before an
#: unchanged scene still gets republished -- UC-11.S-2's "a quiet field
#: stays distinguishable from a dead stream", the exact 1.0 s value this
#: ticket's own dispatch specifies. See the module docstring's
#: "Heartbeat mechanism" section.
TAG_STREAM_HEARTBEAT_INTERVAL_S = 1.0

#: Maximum publish rate (Hz) for *change-driven* publishes -- carried
#: forward unchanged from v1's ``TagStreamProducer.max_hz`` default
#: (``archive/src/aprilcam/daemon/stream.py``). Does not limit heartbeat
#: publishes, which have their own, independent interval.
TAG_STREAM_MAX_PUBLISH_HZ = 20.0

#: World-space (cm) two-sample displacement above which a tag's
#: position is considered to have "changed" for publish purposes, used
#: when a record carries a ``world`` position (camera calibrated). See
#: the module docstring's "Change threshold" section for why this
#: reuses ``vision.motion.MIN_RELIABLE_DISPLACEMENT_CM`` rather than an
#: independently chosen value.
TAG_STREAM_CHANGE_THRESHOLD_CM = MIN_RELIABLE_DISPLACEMENT_CM

#: Pixel-space fallback threshold, used when a record has no ``world``
#: position (camera uncalibrated) -- carried forward unchanged from
#: v1's ``TagStreamProducer.change_threshold_px`` default (8.0 px).
TAG_STREAM_CHANGE_THRESHOLD_PX = 8.0

#: How often :class:`ImageStreamProducer` captures(+warps)+encodes a
#: fresh frame while at least one subscriber exists -- an honestly
#: provisional constant (sprint.md Open Question 1's "detection
#: cadence, image cadence... are provisional constants", same status
#: :data:`TAG_STREAM_INTERVAL_S` has), not tuned against real hardware
#: (blocked this sprint, dark/obstructed field). Set lower than
#: :data:`TAG_STREAM_INTERVAL_S` deliberately -- a JPEG encode, plus a
#: perspective warp for the deskewed case, is real per-tick CPU cost
#: `run_burst` does not pay in the same way, so this starts at a more
#: conservative 15 Hz rather than reusing the tag cadence's ~30 Hz
#: unexamined. Overridable per instance
#: (``ImageStreamProducer(..., interval_s=...)``).
IMAGE_STREAM_INTERVAL_S = 1.0 / 15.0

#: One tag's last-published position, tagged with which space it was
#: measured in (``True`` = world cm, ``False`` = pixel) so a record that
#: changes space between two publishes (e.g. a camera calibrated for
#: the first time mid-stream) is treated as a change rather than
#: compared across incompatible units.
_Position = Tuple[bool, float, float]


def _default_wait(stop_event: threading.Event, timeout: float) -> bool:
    """Bounded wait against *stop_event* -- the production default.

    Mirrors v1's ``_sender_loop`` bounded-``queue.get(timeout=...)``-
    against-a-stop-flag pattern (ticket 001's completion notes): returns
    as soon as *stop_event* is set, rather than sleeping the full
    *timeout* uninterruptibly, which is what lets :meth:`TagStreamProducer.stop`
    join promptly instead of waiting out an in-flight tick's full
    interval. Overridable (``TagStreamProducer(..., wait=...)``) so
    tests can substitute a fake clock/timer that advances virtual time
    instead of blocking on a real one.
    """
    return stop_event.wait(timeout=timeout)


class TagStreamProducer:
    """Drives ``DetectionEngine.run_burst`` at a steady cadence for one
    camera, publishing change-driven, rate-capped, heartbeated
    ``TagFrame``\\ s through a :class:`~aprilcam.daemon.broadcast.Broadcaster`.

    See the module docstring for the idle-gating, heartbeat, and
    change-threshold design.

    Parameters
    ----------
    slug:
        The camera slug this producer detects against -- passed
        straight through to ``DetectionEngine.run_burst`` exactly as
        ``DaemonService.get_tags`` already does.
    camera:
        The already-open ``Camera`` this producer reads from. **Never
        opened or closed by this class** -- the caller sequences
        ``camera.open()`` before the first ``start()`` (i.e. before the
        first subscriber arrives) and ``camera.close()`` only after the
        producer is fully stopped/joined (ticket 002's completion
        notes: concurrent ``open()``/``close()`` vs. an in-flight
        ``read()`` is not synchronized).
    detection:
        The shared ``DetectionEngine`` whose per-camera ring buffer and
        detector this producer drives -- the same instance
        ``DaemonService.get_tags``/``get_tag`` already use, so a burst
        run by this producer and a one-shot query both land in, and
        read from, the identical buffer.
    broadcaster:
        The ``Broadcaster[TagFrame]`` this producer publishes into.
        ``None`` (the default) builds one internally, wired so its own
        ``on_active``/``on_idle`` call :meth:`start`/:meth:`stop`
        directly -- the ergonomic default for a standalone producer.
        Pass an already-constructed ``Broadcaster`` when a caller needs
        its own ``on_active``/``on_idle`` (e.g. ``DaemonService``
        additionally tracking this producer in a dict for shutdown) --
        that caller is then responsible for wiring :meth:`start`/
        :meth:`stop` into its own callbacks.
    interval_s / heartbeat_interval_s / max_publish_hz /
    change_threshold_cm / change_threshold_px:
        See the module-level constants of the same name (module
        docstring) for defaults and provenance. All genuinely
        overridable per instance.
    queue_depth:
        Only used when *broadcaster* is ``None`` -- passed to the
        internally-constructed ``Broadcaster``.
    clock:
        Monotonic time source, used for cadence timing and for
        rate-cap/heartbeat interval decisions. Defaults to
        ``time.monotonic``; tests inject a fake clock so cadence tests
        run instantly and deterministically rather than waiting on a
        real one.
    wait:
        The bounded per-tick wait, called as ``wait(stop_event,
        remaining_seconds)`` and expected to return promptly once
        *stop_event* is set (mirrors ``threading.Event.wait``'s own
        return contract). Defaults to :func:`_default_wait` (a real
        bounded wait); tests inject a fake that advances a fake clock
        without actually blocking.
    stop_timeout_s:
        How long :meth:`stop` waits for the background thread to join
        before giving up (it still clears internal state either way --
        see :meth:`stop`'s docstring).
    """

    def __init__(
        self,
        slug: str,
        camera: Camera,
        detection: DetectionEngine,
        broadcaster: "Optional[Broadcaster[TagFrame]]" = None,
        *,
        interval_s: float = TAG_STREAM_INTERVAL_S,
        heartbeat_interval_s: float = TAG_STREAM_HEARTBEAT_INTERVAL_S,
        max_publish_hz: float = TAG_STREAM_MAX_PUBLISH_HZ,
        change_threshold_cm: float = TAG_STREAM_CHANGE_THRESHOLD_CM,
        change_threshold_px: float = TAG_STREAM_CHANGE_THRESHOLD_PX,
        queue_depth: int = DEFAULT_SUBSCRIBER_QUEUE_DEPTH,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] = _default_wait,
        stop_timeout_s: float = 2.0,
    ) -> None:
        self._slug = slug
        self._camera = camera
        self._detection = detection
        self._interval_s = interval_s
        self._heartbeat_interval_s = heartbeat_interval_s
        self._min_publish_interval_s = 1.0 / max_publish_hz if max_publish_hz > 0 else 0.0
        self._change_threshold_cm = change_threshold_cm
        self._change_threshold_px = change_threshold_px
        self._clock = clock
        self._wait = wait
        self._stop_timeout_s = stop_timeout_s

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_stop_lock = threading.Lock()

        self._last_publish_at: Optional[float] = None
        self._last_published_positions: Dict[TagId, _Position] = {}
        #: Whether the last tick found the camera not PRESENT (sprint 047
        #: ticket 005); drives the absent-heartbeat cadence and the
        #: immediate publish when the camera comes back.
        self._was_absent = False
        self._last_frame_index = 0

        self.broadcaster: "Broadcaster[TagFrame]" = (
            broadcaster
            if broadcaster is not None
            else Broadcaster(queue_depth=queue_depth, on_active=self.start, on_idle=self.stop)
        )

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def running(self) -> bool:
        """Whether the background thread currently exists -- a direct
        internal-state observable (not merely behavioural) for AC1's
        "does not exist as a running thread" requirement."""
        return self._thread is not None

    # -- lifecycle: Broadcaster's on_active/on_idle hooks -----------------

    def start(self) -> None:
        """Begin the background production loop. Intended as
        ``Broadcaster``'s ``on_active`` callback (fired exactly on the
        0->1 subscriber transition) -- idempotent regardless, so a
        caller with its own wiring can call it directly without
        double-starting."""
        with self._start_stop_lock:
            if self._thread is not None:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run, name=f"tag-stream-{self._slug}", daemon=True
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        """Stop and **join** the background thread -- not merely
        signalled (this ticket's own acceptance criterion). Intended as
        ``Broadcaster``'s ``on_idle`` callback (fired exactly on the
        1->0 subscriber transition). A no-op if not running.

        If the thread does not finish within *stop_timeout_s* this logs
        a warning and still clears internal state -- a wedged
        ``run_burst`` (e.g. a backend hung on ``read()``) must not make
        every future ``start()``/``stop()`` cycle hang forever, though
        in that case the old thread genuinely is still alive and this
        is surfaced, not silently hidden.
        """
        with self._start_stop_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
            thread.join(timeout=self._stop_timeout_s)
            if thread.is_alive():
                logger.warning(
                    "tag stream producer %s: background thread did not stop "
                    "within %.1fs",
                    self._slug,
                    self._stop_timeout_s,
                )
            self._thread = None

    # -- the production loop ----------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            tick_start = self._clock()
            tag_frame = None
            if self._camera.lifecycle is not CameraLifecycle.PRESENT:
                # Camera gone (unplugged / reopening): do not read it.
                # Keep the stream alive with an explicit "absent"
                # heartbeat instead of going silent.
                self._publish_absent()
                self._wait_remaining(tick_start)
                continue
            try:
                tag_frame = self._detection.run_burst(self._slug, self._camera)
            except RuntimeError:
                # No frame available this tick (camera.read() returned
                # None) -- mirrors run_burst's own contract: a capture
                # failure is not "zero tags detected" and is not
                # recorded. Skip this tick's publish decision and try
                # again next tick; do not crash the loop.
                logger.debug(
                    "tag stream producer %s: no frame available this tick", self._slug
                )
                tag_frame = None

            if tag_frame is not None:
                self._maybe_publish(tag_frame)

            self._wait_remaining(tick_start)

    def _wait_remaining(self, tick_start: float) -> None:
        remaining = self._interval_s - (self._clock() - tick_start)
        if remaining > 0:
            self._wait(self._stop, remaining)

    def _publish_absent(self) -> None:
        """Publish a ``camera_absent`` heartbeat: at once on the first
        absent tick, then every heartbeat interval."""
        now = self._clock()
        since_publish = (
            now - self._last_publish_at if self._last_publish_at is not None else math.inf
        )
        if self._was_absent and since_publish < self._heartbeat_interval_s:
            return
        self._was_absent = True
        self._last_publish_at = now
        self._last_published_positions = {}
        self.broadcaster.publish(
            TagFrame(
                tags=(),
                timestamp=time.time(),
                frame_index=self._last_frame_index,
                camera_absent=True,
            )
        )

    # -- change-driven / rate-capped / heartbeat publish decision ---------

    def _maybe_publish(self, tag_frame: TagFrame) -> None:
        now = self._clock()
        self._last_frame_index = tag_frame.frame_index
        if self._was_absent:
            # Camera is back: say so immediately rather than waiting for
            # a change or the next heartbeat.
            self._was_absent = False
            self._publish(tag_frame, self._positions_for(tag_frame), now)
            return
        positions = self._positions_for(tag_frame)
        since_publish = (
            now - self._last_publish_at if self._last_publish_at is not None else math.inf
        )

        if self._has_changed(positions) and since_publish >= self._min_publish_interval_s:
            self._publish(tag_frame, positions, now)
            return
        if since_publish >= self._heartbeat_interval_s:
            self._publish(tag_frame, positions, now)

    def _publish(self, tag_frame: TagFrame, positions: Dict[TagId, _Position], now: float) -> None:
        self._last_publish_at = now
        self._last_published_positions = positions
        self.broadcaster.publish(tag_frame)

    def _has_changed(self, positions: Dict[TagId, _Position]) -> bool:
        """v1's ``_has_changed`` exactly (module docstring): a differing
        set of visible tag ids is always a change; otherwise any tag's
        displacement past its space's threshold is."""
        previous = self._last_published_positions
        if positions.keys() != previous.keys():
            return True
        for tag_id, (is_world, x, y) in positions.items():
            was_world, px, py = previous[tag_id]
            if is_world != was_world:
                return True
            threshold = self._change_threshold_cm if is_world else self._change_threshold_px
            if math.hypot(x - px, y - py) > threshold:
                return True
        return False

    @staticmethod
    def _positions_for(tag_frame: TagFrame) -> Dict[TagId, _Position]:
        """World-cm when a record has one, pixel-space fallback when
        not (module docstring's "Change threshold" section) -- decided
        per tag record."""
        positions: Dict[TagId, _Position] = {}
        for record in tag_frame.tags:
            if record.world is not None:
                positions[record.tag] = (True, record.world.x, record.world.y)
            elif record.pixel is not None:
                positions[record.tag] = (False, record.pixel.x, record.pixel.y)
        return positions


class BroadcasterTagStream(TagStream):
    """The concrete ``daemon.interfaces.TagStream`` a subscription to a
    :class:`TagStreamProducer`'s ``Broadcaster`` returns
    (``daemon.service.DaemonService.stream_tags``).

    Derives from ``daemon.interfaces.TagStream`` -- mechanical
    inheritance, not structural (``daemon/interfaces.py``'s module
    design rule, zero ``typing.Protocol``). Wraps one
    ``Broadcaster.subscribe()`` queue; :meth:`close` unsubscribes,
    which -- via whichever ``on_idle`` callback the owning
    ``TagStreamProducer``'s ``Broadcaster`` was wired with -- tears the
    producer thread down when this was the last subscriber (SUC-001
    main flow step 5).
    """

    def __init__(
        self,
        broadcaster: "Broadcaster[TagFrame]",
        subscriber_queue: "queue.Queue[TagFrame]",
        *,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._broadcaster = broadcaster
        self._queue = subscriber_queue
        self._poll_interval_s = poll_interval_s
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[TagFrame]:
        """Yield published ``TagFrame``\\ s until :meth:`close` is
        called. No sentinel/close-signalling exists on the queue itself
        (ticket 001's completion notes) -- a bounded
        ``queue.get(timeout=...)`` poll against :attr:`_closed`, the
        same pattern :class:`TagStreamProducer` itself uses for its own
        per-tick wait, is what makes this notice a :meth:`close`
        promptly instead of blocking on ``queue.get()`` forever."""
        while not self._closed.is_set():
            try:
                yield self._queue.get(timeout=self._poll_interval_s)
            except queue.Empty:
                continue

    def close(self) -> None:
        """Idempotent: a second ``close()`` is a safe no-op, matching
        ``Broadcaster.unsubscribe``'s own no-op-on-unknown-queue
        guarantee."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._broadcaster.unsubscribe(self._queue)


def _default_deskewer_factory(calibration: Calibration, definition: PlayfieldDefinition) -> Deskewer:
    """The real warp routine :class:`ImageStreamProducer` uses by
    default -- *exactly* the ``calibration.deskew.Deskewer`` class
    ``DaemonService._get_deskewed_frame`` constructs for a one-shot
    request, not a second implementation. Overridable
    (``ImageStreamProducer(..., deskewer_factory=...)``) so a test can
    substitute a call-counting wrapper around this same class to prove
    "one warp per tick, not two" (this ticket's own acceptance
    criterion) without monkeypatching this module."""
    return Deskewer(calibration=calibration, width_cm=definition.width_cm, height_cm=definition.height_cm)


@dataclass
class _DeskewContext:
    """What :meth:`ImageStreamProducer.start` snapshots exactly once
    per stream life (module docstring's "Frame geometry" section):
    the ``Deskewer`` instance whose ``px_per_cm``/``world_extent`` stay
    fixed for every tick until :meth:`ImageStreamProducer.stop`, and the
    playfield name (from ``calibration.playfield``) that
    ``annotations_lookup`` is called against every tick -- annotations
    themselves are *not* frozen, only the geometry is."""

    deskewer: Deskewer
    playfield_name: str


class ImageStreamProducer:
    """Captures(+warps if deskewed)+JPEG-encodes at a steady cadence for
    one (camera, deskewed) pair, publishing ``ImageFrame``\\ s through a
    :class:`~aprilcam.daemon.broadcast.Broadcaster`.

    See the module docstring (the section beginning "``ImageStreamProducer``")
    for the idle-gating, shared-computation, geometry-stability, and
    precondition-checking design -- all identical in spirit to
    :class:`TagStreamProducer`, adapted for images.

    Parameters
    ----------
    slug:
        The camera slug this producer captures from -- passed straight
        through to *camera*/*detection* exactly as
        :class:`TagStreamProducer` does.
    camera:
        The already-open ``Camera`` this producer reads from. Same
        ownership contract as :class:`TagStreamProducer`'s *camera*:
        never opened or closed by this class.
    detection:
        The shared ``DetectionEngine`` this producer reads
        :meth:`~aprilcam.daemon.detection.DetectionEngine.latest` from
        to embed each published frame's ``tags`` -- the same instance
        ``DaemonService.get_frame`` already reads for its own tag embed
        (module docstring: "matching ``get_frame``'s existing embed
        contract exactly"). This producer never calls
        ``run_burst`` itself -- it only reads whatever the camera's tag
        stream (or one-shot queries) have already put in the buffer.
    broadcaster:
        The ``Broadcaster[ImageFrame]`` this producer publishes into.
        Same default-construct-or-accept-a-pre-wired-one contract as
        :class:`TagStreamProducer`'s *broadcaster*.
    deskewed:
        ``False`` (the default) for the raw product: capture + encode
        only, ``tags`` embedded, ``annotations`` always ``None``,
        ``px_per_cm``/``world_extent`` always ``None`` -- matching
        ``get_frame(deskewed=False)`` exactly. ``True`` for the
        deskewed product: also warps via *deskewer_factory* and embeds
        the linked playfield's current ``AnnotationSet`` -- matching
        ``get_frame(deskewed=True)``/``_get_deskewed_frame`` exactly.
        Requires *calibration_lookup*, *playfield_lookup*, and
        *annotations_lookup* to all be given (``ValueError`` otherwise).
    calibration_lookup:
        Called as ``calibration_lookup(slug)``, returning the camera's
        current ``Calibration`` or ``None`` -- the same shape as
        ``DetectionEngine``'s own ``calibration_lookup`` (and, in
        practice, the identical bound method,
        ``DaemonService.get_calibration``). Used every tick (both raw
        and deskewed) to overlay ``calibration_stale``; used once, at
        :meth:`start`, to build the deskewed geometry snapshot.
    playfield_lookup:
        Called as ``playfield_lookup(calibration.playfield)``, returning
        the linked ``PlayfieldDefinition`` or ``None`` -- only used
        (once, at :meth:`start`) when *deskewed* is ``True``, to build
        *deskewer_factory*'s input.
    annotations_lookup:
        Called as ``annotations_lookup(playfield_name)`` **every tick**
        (deliberately not snapshotted -- see the module docstring) to
        embed the playfield's *current* ``AnnotationSet``. Only used
        when *deskewed* is ``True``.
    deskewer_factory:
        Builds the one ``Deskewer`` :meth:`start` snapshots for the
        stream's life. Defaults to :func:`_default_deskewer_factory`
        (the real warp); a test overrides this to inject a
        call-counting double around the same real ``Deskewer``.
    interval_s:
        See :data:`IMAGE_STREAM_INTERVAL_S` for the default and its
        provenance. Overridable per instance.
    queue_depth / clock / wait / stop_timeout_s:
        Identical meaning to :class:`TagStreamProducer`'s parameters of
        the same name -- see that class's docstring.
    """

    def __init__(
        self,
        slug: str,
        camera: Camera,
        detection: DetectionEngine,
        broadcaster: "Optional[Broadcaster[ImageFrame]]" = None,
        *,
        deskewed: bool = False,
        calibration_lookup: Optional[Callable[[str], Optional[Calibration]]] = None,
        playfield_lookup: Optional[Callable[[str], Optional[PlayfieldDefinition]]] = None,
        annotations_lookup: Optional[Callable[[str], AnnotationSet]] = None,
        deskewer_factory: Callable[[Calibration, PlayfieldDefinition], Deskewer] = (
            _default_deskewer_factory
        ),
        interval_s: float = IMAGE_STREAM_INTERVAL_S,
        queue_depth: int = DEFAULT_SUBSCRIBER_QUEUE_DEPTH,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] = _default_wait,
        stop_timeout_s: float = 2.0,
        placeholder_source: Optional[Callable[..., np.ndarray]] = None,
    ) -> None:
        if deskewed and (
            calibration_lookup is None or playfield_lookup is None or annotations_lookup is None
        ):
            raise ValueError(
                "ImageStreamProducer(deskewed=True) requires calibration_lookup, "
                "playfield_lookup, and annotations_lookup"
            )
        self._slug = slug
        self._camera = camera
        self._detection = detection
        self._deskewed = deskewed
        self._calibration_lookup = calibration_lookup
        self._playfield_lookup = playfield_lookup
        self._annotations_lookup = annotations_lookup
        self._deskewer_factory = deskewer_factory
        #: ``source(size=..., last_size=...) -> BGR array``: the "NO
        #: SIGNAL" card published while the camera delivers no frames
        #: (sprint 047 ticket 005). ``None`` keeps the old behaviour of
        #: skipping the tick.
        self._placeholder_source = placeholder_source
        self._last_size: Optional[Tuple[int, int]] = None
        self._interval_s = interval_s
        self._clock = clock
        self._wait = wait
        self._stop_timeout_s = stop_timeout_s

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_stop_lock = threading.Lock()

        #: The stream-life geometry snapshot (module docstring) -- built
        #: lazily, on the first tick after :meth:`start`, and reset to
        #: ``None`` again by every :meth:`start` so a resubscribe after
        #: a full stop genuinely starts fresh (this ticket's own
        #: acceptance criterion). ``None`` for a raw producer for its
        #: entire life -- never consulted on that path.
        self._deskew_ctx: Optional[_DeskewContext] = None

        self.broadcaster: "Broadcaster[ImageFrame]" = (
            broadcaster
            if broadcaster is not None
            else Broadcaster(queue_depth=queue_depth, on_active=self.start, on_idle=self.stop)
        )

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def deskewed(self) -> bool:
        return self._deskewed

    @property
    def running(self) -> bool:
        """Whether the background thread currently exists -- a direct
        internal-state observable, identical in spirit to
        :attr:`TagStreamProducer.running`."""
        return self._thread is not None

    # -- lifecycle: Broadcaster's on_active/on_idle hooks -----------------

    def start(self) -> None:
        """Begin the background production loop. Idempotent; intended
        as ``Broadcaster``'s ``on_active`` callback (the 0->1 subscriber
        transition). Resets the geometry snapshot (module docstring) so
        a fresh stream life always rebuilds it from whatever calibration
        currently says, rather than reusing a snapshot from a previous
        stream's life."""
        with self._start_stop_lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._deskew_ctx = None
            thread = threading.Thread(
                target=self._run,
                name=f"image-stream-{self._slug}-{'deskewed' if self._deskewed else 'raw'}",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        """Stop and **join** the background thread -- identical
        contract to :meth:`TagStreamProducer.stop`, including the
        bounded-timeout-then-log-and-continue behaviour for a wedged
        thread."""
        with self._start_stop_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
            thread.join(timeout=self._stop_timeout_s)
            if thread.is_alive():
                logger.warning(
                    "image stream producer %s (deskewed=%s): background thread did not "
                    "stop within %.1fs",
                    self._slug,
                    self._deskewed,
                    self._stop_timeout_s,
                )
            self._thread = None

    # -- the production loop ----------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            tick_start = self._clock()
            try:
                self._tick()
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "image stream producer %s (deskewed=%s): unhandled error in "
                    "production tick",
                    self._slug,
                    self._deskewed,
                )
            elapsed = self._clock() - tick_start
            remaining = self._interval_s - elapsed
            if remaining > 0:
                self._wait(self._stop, remaining)

    def _tick(self) -> None:
        deskew_ctx: Optional[_DeskewContext] = None
        if self._deskewed:
            if self._deskew_ctx is None:
                self._deskew_ctx = self._build_deskew_context()
            deskew_ctx = self._deskew_ctx
            if deskew_ctx is None:
                # Should not happen in practice -- DaemonService.stream_images
                # checks calibration before ever constructing/reusing this
                # producer (module docstring's "Preconditions" section).
                # Guarded here regardless, rather than crashing the loop, so
                # a genuinely-impossible-today race is a silent skipped tick,
                # not a dead background thread.
                logger.debug(
                    "image stream producer %s: calibration/playfield not yet "
                    "available -- skipping tick",
                    self._slug,
                )
                return

        # A non-PRESENT camera is not read at all (no reopen budget burnt).
        frame = (
            self._camera.read()
            if self._camera.lifecycle is CameraLifecycle.PRESENT
            else None
        )
        placeholder = frame is None
        if frame is None:
            if self._placeholder_source is None:
                logger.debug(
                    "image stream producer %s: no frame available this tick", self._slug
                )
                return
            card_size = deskew_ctx.deskewer.output_size if deskew_ctx is not None else None
            frame = self._placeholder_source(size=card_size, last_size=self._last_size)
        elif deskew_ctx is None:
            self._last_size = (frame.shape[1], frame.shape[0])

        calibration = self._calibration_lookup(self._slug) if self._calibration_lookup else None
        calibration_stale = calibration.stale if calibration is not None else False

        if deskew_ctx is not None:
            # The card is already at the deskewed output size.
            image = frame if placeholder else deskew_ctx.deskewer.deskew(frame)
            px_per_cm: Optional[float] = deskew_ctx.deskewer.px_per_cm
            world_extent: Optional[Tuple[float, float]] = deskew_ctx.deskewer.world_extent
            assert self._annotations_lookup is not None  # guaranteed by __init__'s guard
            annotations: Optional[AnnotationSet] = self._annotations_lookup(
                deskew_ctx.playfield_name
            )
        else:
            image = frame
            px_per_cm = None
            world_extent = None
            annotations = None

        ok, buf = cv.imencode(".jpg", image)
        if not ok:
            logger.warning(
                "image stream producer %s: failed to encode frame as jpeg", self._slug
            )
            return

        height, width = image.shape[:2]
        image_frame = ImageFrame(
            data=buf.tobytes(),
            width=width,
            height=height,
            encoding="jpeg",
            timestamp=time.time(),
            deskewed=self._deskewed,
            px_per_cm=px_per_cm,
            world_extent=world_extent,
            tags=None if placeholder else self._detection.latest(self._slug),
            annotations=annotations,
            calibration_stale=calibration_stale,
            placeholder=placeholder,
        )
        self.broadcaster.publish(image_frame)

    def _build_deskew_context(self) -> Optional[_DeskewContext]:
        """The one-time (per stream life) geometry snapshot -- see the
        module docstring's "Frame geometry" section. Returns ``None``
        (never raises) when calibration or its linked playfield is
        momentarily unavailable, so a should-not-happen race degrades to
        a skipped tick rather than a dead thread."""
        assert self._calibration_lookup is not None and self._playfield_lookup is not None
        calibration = self._calibration_lookup(self._slug)
        if calibration is None:
            logger.warning(
                "image stream producer %s: no calibration available at stream start",
                self._slug,
            )
            return None
        definition = self._playfield_lookup(calibration.playfield)
        if definition is None:
            logger.warning(
                "image stream producer %s: calibrated playfield %r is no longer held",
                self._slug,
                calibration.playfield,
            )
            return None
        deskewer = self._deskewer_factory(calibration, definition)
        return _DeskewContext(deskewer=deskewer, playfield_name=calibration.playfield)


class BroadcasterImageStream(ImageStream):
    """The concrete ``daemon.interfaces.ImageStream`` a subscription to
    an :class:`ImageStreamProducer`'s ``Broadcaster`` returns
    (``daemon.service.DaemonService.stream_images``).

    Derives from ``daemon.interfaces.ImageStream`` -- mechanical
    inheritance, not structural, mirroring :class:`BroadcasterTagStream`
    exactly (module design rule, zero ``typing.Protocol``).
    """

    def __init__(
        self,
        broadcaster: "Broadcaster[ImageFrame]",
        subscriber_queue: "queue.Queue[ImageFrame]",
        *,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._broadcaster = broadcaster
        self._queue = subscriber_queue
        self._poll_interval_s = poll_interval_s
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[ImageFrame]:
        """Yield published ``ImageFrame``\\ s until :meth:`close` is
        called -- identical bounded-poll pattern to
        :meth:`BroadcasterTagStream.__iter__`."""
        while not self._closed.is_set():
            try:
                yield self._queue.get(timeout=self._poll_interval_s)
            except queue.Empty:
                continue

    def close(self) -> None:
        """Idempotent, identical contract to
        :meth:`BroadcasterTagStream.close`."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._broadcaster.unsubscribe(self._queue)
