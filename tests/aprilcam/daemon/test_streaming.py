"""Tests for ``aprilcam.daemon.streaming`` (sprint 027 ticket 003).

Headless throughout -- hardware is currently blocked this sprint (dark,
obstructed field). Two fixture styles:

* A substitutable, call-counting/scripted ``TagDetector``-shaped double
  (mirroring ``test_detection.py``'s own ``_CountingDetector``/
  ``_ScriptedDetector`` pattern) injected via
  ``DetectionEngine(detector_factory=...)`` -- deterministic content
  with no real pixel data needed, for the idle-gating/cadence/
  change-threshold/heartbeat/rate-cap/shutdown tests below.
* One end-to-end test against the real ``vision.pipeline``/
  ``vision.detector`` and ticket 003's new looping ``FakeBackend``
  (``loop=True``), proving the "fixture replay backend" this ticket's
  implementation plan asked for actually supplies continuous frames
  with something real to detect, not just a scripted double.

**Fake clock, not real ``time.sleep``, for every cadence/timing test.**
``TagStreamProducer`` accepts injectable ``clock``/``wait`` callables
(mirroring ``view/viewer.py``'s ``PollLoop``'s own ``clock``/``sleep``
injection, and ``tests/aprilcam/view/test_viewer.py``'s ``_FakeClock``).
The fake ``wait`` here advances a virtual clock without actually
blocking, so a background thread ticks through many iterations near-
instantly in real wall-clock time; tests synchronize with it via a
``threading.Event`` set once a target tick count is reached (a real,
generous timeout bounds this purely as a test-hang safety net, not as
part of the cadence being tested), then stop the producer from the main
thread. Because the producer does not check the fake wait's return
value mid-tick, at most one extra tick can complete after the target is
reached before ``stop()`` takes effect -- assertions below use ``>=``/
range checks rather than tight equality to stay robust to that, and to
real thread-scheduling variance in general.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration.interfaces import Calibration
from aprilcam.camera.camera import Camera
from aprilcam.daemon.broadcast import DEFAULT_SUBSCRIBER_QUEUE_DEPTH
from aprilcam.daemon.detection import CONFIRMATION_STREAK, DetectionEngine
from aprilcam.daemon.streaming import (
    TAG_STREAM_CHANGE_THRESHOLD_CM,
    TAG_STREAM_CHANGE_THRESHOLD_PX,
    BroadcasterTagStream,
    TagStreamProducer,
)
from aprilcam.types import PixelPoint, TagFamily, TagFrame, TagId
from aprilcam.vision.detector import Detection

from tests.aprilcam.camera.test_camera import FakeBackend
from tests.aprilcam.daemon.test_detection import _detection_at, _fixture_calibration
from tests.aprilcam.vision._tag_fixtures import synthetic_tag_image


def _blank_frame() -> np.ndarray:
    return np.full((32, 32, 3), 255, dtype=np.uint8)


def _tag_frame_bgr(family: TagFamily, tag_id: int) -> np.ndarray:
    """A synthetic tag image, converted to BGR -- what a real
    ``Camera.read()`` returns. Mirrors ``test_detection.py``'s helper of
    the same name."""
    gray = synthetic_tag_image(family, tag_id)
    return cv.cvtColor(gray, cv.COLOR_GRAY2BGR)


def _camera(slug: str, *, loop: bool = True) -> Camera:
    """A ``Camera`` over a looping ``FakeBackend`` -- continuous frames
    for as long as a producer's background thread runs, per this
    ticket's shared test infrastructure (module docstring)."""
    cam = Camera(slug, FakeBackend(read_queue=[_blank_frame()], loop=loop))
    cam.open()
    return cam


def _pixel_hit(number: int, x: float, y: float, *, family: TagFamily = TagFamily.APRILTAG) -> Detection:
    corners = np.array(
        [[x - 5, y - 5], [x + 5, y - 5], [x + 5, y + 5], [x - 5, y + 5]], dtype=np.float32
    )
    return Detection(family=family, number=number, corners=corners, center=PixelPoint(x=x, y=y))


class _CallCountingDetector:
    """Returns *hits* (default: none) on every call -- content is fixed;
    only the call count matters (idle-gating/cadence tests)."""

    def __init__(self, hits: Optional[List[Detection]] = None) -> None:
        self._hits = list(hits) if hits else []
        self.calls = 0

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> List[Detection]:
        self.calls += 1
        return list(self._hits)


class _SequenceDetector:
    """Calls ``build_hits(call_index)`` (0-based) on every ``detect()``
    call -- lets a test script an arbitrary, unbounded per-tick
    detection sequence (a moving tag, a jittering tag, ...) without a
    fixed-length list that could run out under a fast-spinning
    fake-clock loop."""

    def __init__(self, build_hits: Callable[[int], List[Detection]]) -> None:
        self._build_hits = build_hits
        self.calls = 0

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> List[Detection]:
        hits = self._build_hits(self.calls)
        self.calls += 1
        return hits


class _FakeClock:
    """Advances only via the injected ``wait`` callable below -- keeps
    cadence/heartbeat/rate-cap/threshold tests instant and deterministic,
    no real waiting (mirrors ``tests/aprilcam/view/test_viewer.py``'s
    ``_FakeClock``)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.wait_calls = 0

    def clock(self) -> float:
        return self.now


def _clock_and_wait_stopping_after(
    n: int,
) -> Tuple[_FakeClock, Callable[[threading.Event, float], bool], threading.Event]:
    """A fake clock + wait pair for :class:`TagStreamProducer`, plus a
    ``threading.Event`` set once *n* ticks have occurred -- the test
    synchronization primitive described in the module docstring."""
    clock = _FakeClock()
    reached = threading.Event()

    def wait(stop_event: threading.Event, timeout: float) -> bool:
        clock.wait_calls += 1
        clock.now += timeout
        if clock.wait_calls >= n:
            reached.set()
        return stop_event.is_set()

    return clock, wait, reached


def _drain(q: "queue.Queue[TagFrame]") -> List[TagFrame]:
    items: List[TagFrame] = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition not met within timeout"


# ---------------------------------------------------------------------------
# Idle gating: no thread, zero run_burst calls, with zero subscribers
# ---------------------------------------------------------------------------


def test_producer_thread_does_not_exist_with_zero_subscribers():
    detector = _CallCountingDetector()
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-idle")
    producer = TagStreamProducer("cam-idle", cam, engine)

    assert producer.running is False
    time.sleep(0.05)  # a real, short wall-clock window -- nothing should run
    assert producer.running is False
    assert detector.calls == 0
    assert engine.history("cam-idle") == ()


def test_subscribe_starts_the_thread_and_unsubscribe_stops_and_joins_it():
    detector = _CallCountingDetector()
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-lifecycle")
    producer = TagStreamProducer("cam-lifecycle", cam, engine, interval_s=0.01)

    q = producer.broadcaster.subscribe()
    _wait_until(lambda: producer.running)
    thread_ref = producer._thread  # noqa: SLF001 - internal, proving a real join below
    assert thread_ref is not None
    assert thread_ref.is_alive() is True

    producer.broadcaster.unsubscribe(q)

    assert producer.running is False
    assert thread_ref.is_alive() is False  # actually joined, not merely signalled


def test_resubscribe_after_unsubscribe_starts_a_fresh_thread():
    detector = _CallCountingDetector()
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-resub")
    producer = TagStreamProducer("cam-resub", cam, engine, interval_s=0.01)

    q1 = producer.broadcaster.subscribe()
    _wait_until(lambda: producer.running)
    first_thread = producer._thread  # noqa: SLF001
    producer.broadcaster.unsubscribe(q1)
    assert producer.running is False

    q2 = producer.broadcaster.subscribe()
    _wait_until(lambda: producer.running)
    second_thread = producer._thread  # noqa: SLF001
    assert second_thread is not None
    assert second_thread is not first_thread

    producer.broadcaster.unsubscribe(q2)
    assert producer.running is False


def test_start_and_stop_are_idempotent():
    detector = _CallCountingDetector()
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-idempotent")
    producer = TagStreamProducer("cam-idempotent", cam, engine, interval_s=0.01)

    producer.start()
    producer.start()  # second call: no-op, no second thread
    _wait_until(lambda: producer.running)
    producer.stop()
    producer.stop()  # second call: no-op, no error
    assert producer.running is False


# ---------------------------------------------------------------------------
# Steady cadence + wall-clock-bounded ring buffer, under a fake clock
# ---------------------------------------------------------------------------


def test_run_burst_runs_at_the_configured_cadence_and_ring_buffer_grows_wall_clock_bounded():
    detector = _CallCountingDetector()
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1, buffer_size=1000)
    cam = _camera("cam-cadence")
    n_ticks = 25
    clock, wait, reached = _clock_and_wait_stopping_after(n_ticks)
    producer = TagStreamProducer(
        "cam-cadence", cam, engine, interval_s=0.05, clock=clock.clock, wait=wait
    )

    producer.start()
    assert reached.wait(timeout=5.0)
    producer.stop()

    assert detector.calls >= n_ticks
    # every successful run_burst call appends to the ring buffer,
    # regardless of the producer's own publish decision -- this is the
    # sprint 021 scope-boundary fix (UC-26): the buffer grows on this
    # producer's own wall-clock cadence, not only on a one-shot query.
    assert len(engine.history("cam-cadence")) == detector.calls
    # the scheduling clock advanced in exact interval_s increments --
    # proves the cadence math itself, independent of real wall time.
    assert clock.now == pytest.approx(clock.wait_calls * 0.05)


# ---------------------------------------------------------------------------
# Heartbeat: a quiet fixture still publishes at the configured interval
# ---------------------------------------------------------------------------


def test_quiet_fixture_still_delivers_heartbeats_at_the_configured_interval():
    detector = _CallCountingDetector()  # empty tags, every tick -- a quiet field
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-quiet")
    n_ticks = 60
    clock, wait, reached = _clock_and_wait_stopping_after(n_ticks)
    producer = TagStreamProducer(
        "cam-quiet",
        cam,
        engine,
        interval_s=0.01,
        heartbeat_interval_s=0.1,
        max_publish_hz=1000.0,
        queue_depth=1000,  # nothing should silently drop in this test
        clock=clock.clock,
        wait=wait,
    )

    q = producer.broadcaster.subscribe()
    assert reached.wait(timeout=5.0)
    producer.stop()

    received = _drain(q)
    # 60 ticks * 0.01s = 0.6s virtual elapsed; a heartbeat every 0.1s
    # virtual is ~6-7 publishes (first tick always publishes
    # immediately too) -- a quiet field never goes fully silent.
    assert 4 <= len(received) <= 9
    assert all(frame.tags == () for frame in received)


# ---------------------------------------------------------------------------
# Change-driven + rate-capped publish, pixel space (uncalibrated)
# ---------------------------------------------------------------------------


def test_pixel_jitter_below_threshold_is_not_published_as_a_change():
    assert TAG_STREAM_CHANGE_THRESHOLD_PX > 1.0  # sanity: the jitter below is genuinely sub-threshold

    def build_hits(call_index: int) -> List[Detection]:
        # +/- 1px jitter around (100, 100) -- well under the 8px default.
        offset = 1.0 if call_index % 2 == 0 else -1.0
        return [_pixel_hit(1, 100.0 + offset, 100.0)]

    detector = _SequenceDetector(build_hits)
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-px-jitter")
    n_ticks = 20
    clock, wait, reached = _clock_and_wait_stopping_after(n_ticks)
    producer = TagStreamProducer(
        "cam-px-jitter",
        cam,
        engine,
        interval_s=0.001,
        heartbeat_interval_s=1000.0,  # far larger than the test window -- isolates change-detection
        clock=clock.clock,
        wait=wait,
    )

    q = producer.broadcaster.subscribe()
    assert reached.wait(timeout=5.0)
    producer.stop()

    received = _drain(q)
    # Only the very first tick's unconditional publish -- jitter never
    # crosses the threshold relative to that baseline.
    assert len(received) == 1


def test_pixel_change_above_threshold_is_published_promptly_and_rate_capped():
    def build_hits(call_index: int) -> List[Detection]:
        return [_pixel_hit(1, 20.0 * call_index, 0.0)]  # moves 20px > 8px threshold every tick

    detector = _SequenceDetector(build_hits)
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-px-moving")
    n_ticks = 40
    clock, wait, reached = _clock_and_wait_stopping_after(n_ticks)
    producer = TagStreamProducer(
        "cam-px-moving",
        cam,
        engine,
        interval_s=0.001,
        heartbeat_interval_s=1000.0,
        max_publish_hz=100.0,  # min publish interval 0.01s == 10 ticks at this cadence
        queue_depth=1000,  # nothing drains until after stop() -- must not silently drop
        clock=clock.clock,
        wait=wait,
    )

    q = producer.broadcaster.subscribe()
    assert reached.wait(timeout=5.0)
    producer.stop()

    received = _drain(q)
    # Every tick is individually "changed", but the rate cap must
    # suppress most of them -- clearly fewer publishes than ticks.
    assert 3 <= len(received) < detector.calls
    xs = [frame.tags[0].pixel.x for frame in received]
    assert xs == sorted(xs)
    assert xs[0] < xs[-1]  # genuinely tracking motion, not repeating a stale value


# ---------------------------------------------------------------------------
# Change-driven publish, world-cm space (calibrated) -- sprint.md
# Design Rationale 5
# ---------------------------------------------------------------------------


def test_world_cm_jitter_below_threshold_is_not_published_as_a_change():
    assert TAG_STREAM_CHANGE_THRESHOLD_CM < 1.0  # sanity: the drift below stays sub-threshold
    calibration = _fixture_calibration("cam-cm-jitter")

    def build_hits(call_index: int) -> List[Detection]:
        # A tiny, monotonic 0.01 cm/tick drift -- total drift across the
        # whole run stays well under the 0.5 cm threshold.
        return [_detection_at(TagFamily.APRILTAG, 1, center_world=(10.0 + 0.01 * call_index, 20.0))]

    detector = _SequenceDetector(build_hits)
    engine = DetectionEngine(
        detector_factory=lambda: detector,
        calibration_lookup=lambda slug: calibration,
        confirmation_streak=1,
    )
    cam = _camera("cam-cm-jitter")
    n_ticks = 10
    clock, wait, reached = _clock_and_wait_stopping_after(n_ticks)
    producer = TagStreamProducer(
        "cam-cm-jitter",
        cam,
        engine,
        interval_s=0.001,
        heartbeat_interval_s=1000.0,
        clock=clock.clock,
        wait=wait,
    )

    q = producer.broadcaster.subscribe()
    assert reached.wait(timeout=5.0)
    producer.stop()

    received = _drain(q)
    assert len(received) == 1
    assert received[0].tags[0].calibrated is True


def test_world_cm_change_above_threshold_triggers_a_prompt_publish():
    calibration = _fixture_calibration("cam-cm-moving")

    def build_hits(call_index: int) -> List[Detection]:
        return [_detection_at(TagFamily.APRILTAG, 1, center_world=(10.0 + 1.0 * call_index, 20.0))]

    detector = _SequenceDetector(build_hits)
    engine = DetectionEngine(
        detector_factory=lambda: detector,
        calibration_lookup=lambda slug: calibration,
        confirmation_streak=1,
    )
    cam = _camera("cam-cm-moving")
    n_ticks = 5
    clock, wait, reached = _clock_and_wait_stopping_after(n_ticks)
    producer = TagStreamProducer(
        "cam-cm-moving",
        cam,
        engine,
        interval_s=0.001,
        heartbeat_interval_s=1000.0,
        max_publish_hz=100000.0,  # effectively no rate cap -- isolates the threshold decision
        queue_depth=1000,  # nothing drains until after stop() -- must not silently drop
        clock=clock.clock,
        wait=wait,
    )

    q = producer.broadcaster.subscribe()
    assert reached.wait(timeout=5.0)
    producer.stop()

    received = _drain(q)
    # 1 cm/tick is far above the 0.5 cm threshold -- (almost) every
    # tick publishes.
    assert len(received) >= n_ticks - 1
    xs = [frame.tags[0].world.x for frame in received]
    assert xs == sorted(xs)
    assert xs[0] < xs[-1]


# ---------------------------------------------------------------------------
# Calibration going stale mid-stream (UC-11.S-3) -- same test hook
# daemon/detection.py's own tests use: a mutable calibration-lookup dict
# ---------------------------------------------------------------------------


def test_calibration_going_stale_mid_stream_reaches_the_next_published_frame():
    calibration = _fixture_calibration("cam-stale")
    calibrations: Dict[str, Calibration] = {"cam-stale": calibration}
    detector = _CallCountingDetector()  # content is irrelevant -- only calibration_stale matters
    engine = DetectionEngine(
        detector_factory=lambda: detector,
        calibration_lookup=lambda slug: calibrations.get(slug),
        confirmation_streak=1,
    )
    cam = _camera("cam-stale")
    n_ticks = 10
    flip_at_tick = 4
    clock = _FakeClock()
    reached = threading.Event()

    def wait(stop_event: threading.Event, timeout: float) -> bool:
        clock.wait_calls += 1
        clock.now += timeout
        if clock.wait_calls == flip_at_tick:
            calibrations["cam-stale"] = replace(calibrations["cam-stale"], stale=True)
        if clock.wait_calls >= n_ticks:
            reached.set()
        return stop_event.is_set()

    producer = TagStreamProducer(
        "cam-stale",
        cam,
        engine,
        interval_s=0.001,
        heartbeat_interval_s=0.001,  # heartbeat fires ~every tick -- publishes flow continuously
        queue_depth=1000,  # nothing drains until after stop() -- must not silently drop
        clock=clock.clock,
        wait=wait,
    )

    q = producer.broadcaster.subscribe()
    assert reached.wait(timeout=5.0)
    producer.stop()

    received = _drain(q)
    assert received, "expected at least one published frame"
    # Every publish strictly before the flip must still read not-stale;
    # calibration_stale=True must appear from some point on and never
    # revert (this test's calibration is never un-flagged).
    stale_flags = [frame.calibration_stale for frame in received]
    assert True in stale_flags
    first_stale_index = stale_flags.index(True)
    assert all(flag is False for flag in stale_flags[:first_stale_index])
    assert all(flag is True for flag in stale_flags[first_stale_index:])


# ---------------------------------------------------------------------------
# Slow subscriber drops frames without affecting a fast subscriber
# ---------------------------------------------------------------------------


def test_slow_subscriber_drops_frames_without_affecting_a_fast_subscriber():
    def build_hits(call_index: int) -> List[Detection]:
        return [_pixel_hit(1, 20.0 * call_index, 0.0)]

    detector = _SequenceDetector(build_hits)
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-slow")
    n_ticks = 30
    clock = _FakeClock()
    reached = threading.Event()
    fast_received: List[TagFrame] = []
    fast_queue_holder: Dict[str, "queue.Queue[TagFrame]"] = {}

    def wait(stop_event: threading.Event, timeout: float) -> bool:
        clock.wait_calls += 1
        clock.now += timeout
        # Simulate a consumer that keeps up in real time: drain the
        # "fast" subscriber's queue on every tick, right after that
        # tick's publish decision has already run.
        fast_q = fast_queue_holder.get("fast")
        if fast_q is not None:
            fast_received.extend(_drain(fast_q))
        if clock.wait_calls >= n_ticks:
            reached.set()
        return stop_event.is_set()

    producer = TagStreamProducer(
        "cam-slow",
        cam,
        engine,
        interval_s=0.001,
        heartbeat_interval_s=1000.0,
        max_publish_hz=1_000_000.0,  # every changed tick publishes
        change_threshold_px=1.0,
        clock=clock.clock,
        wait=wait,
    )
    slow_q = producer.broadcaster.subscribe()
    fast_q = producer.broadcaster.subscribe()
    fast_queue_holder["fast"] = fast_q

    producer.start()
    assert reached.wait(timeout=5.0)
    producer.stop()

    # The slow (never-drained) subscriber never grows past the
    # broadcaster's bounded queue depth, and publishing to it never
    # raised or blocked anything.
    assert slow_q.qsize() <= DEFAULT_SUBSCRIBER_QUEUE_DEPTH
    # The fast (continuously-drained) subscriber on the very same
    # producer received a healthy stream of publishes, unaffected by
    # the slow one's backlog.
    assert len(fast_received) > DEFAULT_SUBSCRIBER_QUEUE_DEPTH


# ---------------------------------------------------------------------------
# BroadcasterTagStream: close() unsubscribes and is idempotent
# ---------------------------------------------------------------------------


def test_broadcaster_tag_stream_close_unsubscribes_and_is_idempotent():
    detector = _CallCountingDetector()
    engine = DetectionEngine(detector_factory=lambda: detector, confirmation_streak=1)
    cam = _camera("cam-stream-close")
    producer = TagStreamProducer("cam-stream-close", cam, engine, interval_s=0.01)

    q = producer.broadcaster.subscribe()
    stream = BroadcasterTagStream(producer.broadcaster, q)
    _wait_until(lambda: producer.running)

    stream.close()
    assert producer.running is False  # last subscriber left -- idle-gated

    stream.close()  # idempotent -- no error


# ---------------------------------------------------------------------------
# End-to-end sanity: real vision pipeline/detector over the looping
# FakeBackend -- proves the "fixture replay backend" (this ticket's
# shared test infrastructure) genuinely supplies continuous frames with
# something real to detect, not just scripted doubles. Real threads, a
# real (tiny) interval -- an existence proof, not a cadence test.
# ---------------------------------------------------------------------------


def test_real_detection_over_the_looping_backend_publishes_the_real_tag():
    tag_image = _tag_frame_bgr(TagFamily.APRILTAG, 7)
    cam = Camera("cam-real", FakeBackend(read_queue=[tag_image], loop=True))
    cam.open()
    engine = DetectionEngine(confirmation_streak=CONFIRMATION_STREAK)  # real pipeline + real detector
    producer = TagStreamProducer("cam-real", cam, engine, interval_s=0.01)

    q = producer.broadcaster.subscribe()
    try:
        deadline = time.monotonic() + 5.0
        found: Optional[TagFrame] = None
        while time.monotonic() < deadline:
            try:
                frame = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if frame.tags:
                found = frame
                break
        assert found is not None, "real detector never confirmed AprilTag 7 over the looping backend"
        assert found.tags[0].tag == TagId(family=TagFamily.APRILTAG, number=7)
    finally:
        producer.broadcaster.unsubscribe(q)

    assert producer.running is False
