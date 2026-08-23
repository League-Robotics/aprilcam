"""Real-clock, real-socket integration pass for sprint 027 (ticket 008).

Tickets 001-007 each proved their own scenario correct against a
**fake**/scripted clock (``test_streaming.py``'s ``_FakeClock``/injected
``wait``) so cadence/heartbeat/rate-cap assertions could run instantly
and deterministically. That is the right default for a unit test, but it
never actually exercises genuine timing behaviour: a fake clock cannot
observe whether a real background thread, a real socket, and real OS
thread scheduling still make the same claims true when nothing is
substituted out. This module is ticket 008's answer -- the same
UC-07/UC-11 scenarios, run **at least once** against a real clock
(``time.monotonic()``/``time.time()``), real ``threading.Thread``\\ s,
and (where practical) a real socket connection reached the way an actual
robot program or the viewer would reach it: ``DaemonService`` down
through ``daemon/streaming.py``, ``daemon/stream_transport.py``, a real
socket, ``client/streams.py``, to ``GrpcDaemon``.

**Where a per-ticket test already covers a scenario under a fake clock,
this module's job is the real-clock version of the *same* scenario, not
a second copy of the fake-clock one.** Concretely, this module does not
re-derive the change-threshold/rate-cap math itself (``test_streaming.py``
already does, exhaustively, under a fake clock) -- it targets exactly
the six things sprint.md's Test Strategy and this ticket's own
acceptance criteria name as needing a real-clock observation: shared
one-warp-per-tick computation, backpressure/silent-drop, heartbeat
timing, mid-stream calibration staleness, unsubscribe idle-gating
latency, and the ring buffer's wall-clock span (UC-26).

**Two layers of coverage, deliberately not the same layer for every
scenario:**

* **Full stack, real socket** (``_bind_stream_client`` below -- a real
  ``DaemonService`` + real ``StreamTransportManager`` + real ``grpc.Server``
  + real ``GrpcDaemon`` over a real unix socket, ticket 005/006's own
  pattern with no fake clock/wait injected anywhere in the chain):
  two-subscribers-one-warp (via a monkeypatched, call-counting
  ``Deskewer.deskew`` -- the real class, just counted), heartbeat timing,
  calibration-stale propagation (tag and image), concurrent multi-product
  unsubscribe/idle-gating, an inert client not stalling an active one,
  and the ring buffer's real wall-clock span.
* **Producer level, direct, real clock** (``daemon.streaming.
  TagStreamProducer``/``ImageStreamProducer`` constructed with no
  ``clock``/``wait`` override at all -- production's own
  ``time.monotonic``/bounded-``Event.wait`` -- real threads, real
  timing): the slow-subscriber-drops-without-stalling-a-fast-one
  scenario, for both tag and image streams. This is deliberately **not**
  routed through a real socket: at this sprint's default cadences
  (``IMAGE_STREAM_INTERVAL_S``\\ 's ~15 Hz, tiny fixture JPEGs), the
  kernel's own socket send buffer can absorb dozens of seconds of an
  unread client before ``Broadcaster``'s bounded (depth-2) subscriber
  queue itself would ever see backpressure -- the OS buffer would mask
  exactly the thing this scenario needs to prove, or make the test take
  an unpredictable number of real seconds to actually saturate it. The
  unread-side-of-a-socket variant is still exercised
  (``test_full_stack_an_unread_client_socket_does_not_stall_an_active_client_real_clock``
  below covers the "does not stall" half at the real-socket layer,
  honestly, without claiming to also prove the "drops" half there).

Reuses ``tests.aprilcam.daemon.test_service``'s ``_open_camera_looping``/
``_service_with_camera``/``_frame``/``_calibrated_streaming_service`` and
``tests.aprilcam.daemon.test_streaming``'s ``_camera``/``_pixel_hit``/
``_SequenceDetector`` fixtures -- the same "fixture replay backend"
(``FakeBackend(loop=True)``) every other sprint 027 test file already
established, not a new fake-camera implementation.
"""

from __future__ import annotations

import queue
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Tuple

import grpc
import pytest

from aprilcam.calibration.deskew import Deskewer
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.config.directories import Directories
from aprilcam.daemon.broadcast import DEFAULT_SUBSCRIBER_QUEUE_DEPTH
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.daemon.rpc import build_server, build_stream_transport
from aprilcam.daemon.service import DaemonService
from aprilcam.daemon.stream_transport import StreamTransportManager
from aprilcam.daemon.streaming import (
    TAG_STREAM_HEARTBEAT_INTERVAL_S,
    ImageStreamProducer,
    TagStreamProducer,
)
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.types import ImageFrame, TagFrame

from tests.aprilcam.daemon.test_service import (
    _calibrated_streaming_service,
    _frame,
    _open_camera_looping,
    _service_with_camera,
)
from tests.aprilcam.daemon.test_streaming import _camera, _pixel_hit, _SequenceDetector

# ---------------------------------------------------------------------------
# Shared fixtures/helpers -- matching test_stream_transport.py/test_streams.py
# ---------------------------------------------------------------------------


@pytest.fixture
def directories(tmp_path: Path) -> Directories:
    return Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")


@pytest.fixture
def registry(tmp_path: Path) -> CameraRegistry:
    return CameraRegistry(tmp_path / "state" / "cameras")


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A short top-level ``/tmp`` directory -- unix domain socket paths
    have a ~103-character OS limit, routinely exceeded by pytest's
    deeply nested ``tmp_path`` (same reasoning every other sprint 027
    stream-transport test module documents)."""
    d = Path(tempfile.mkdtemp(prefix="ac-strint-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _looping_service(directories: Directories, registry: CameraRegistry) -> Tuple[DaemonService, str]:
    """A ``DaemonService`` with one uncalibrated camera, backed by a
    *looping* fake backend -- a background stream producer never runs
    dry for as long as a test keeps a real subscriber connected."""
    identity, slug, cam, _backend = _open_camera_looping(registry, _frame())
    service = _service_with_camera(directories, registry, identity, slug, cam)
    return service, slug


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Real-clock polling helper -- ``time.monotonic()``-bounded, the
    same idiom every ticket 005/006/007 test module already uses to
    observe a real background thread's state without a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@contextmanager
def _bind_stream_client(
    service: DaemonService, socket_dir: Path
) -> Iterator[Tuple[GrpcDaemon, StreamTransportManager]]:
    """Bind a real ``grpc.Server`` (control plane) wired to a real
    :class:`StreamTransportManager` (bulk-stream sockets) and yield a
    connected, real ``GrpcDaemon`` -- ticket 005/006's own
    ``_bind_stream_stub``/``_bind_stream_client`` pattern, reused
    verbatim rather than re-derived: this module's job is new
    *scenarios*, not a new way of wiring the stack up."""
    control_dir = Path(tempfile.mkdtemp(prefix="ac-strint-ctrl-", dir="/tmp"))
    transport = build_stream_transport(service, socket_dir)
    try:
        listener = build_server(service, control_dir / "control.sock", transport=transport)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{control_dir / 'control.sock'}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            yield GrpcDaemon(stub, target="localhost"), transport
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        transport.stop()
        shutil.rmtree(control_dir, ignore_errors=True)


# ===========================================================================
# Full stack, real socket, real clock
# ===========================================================================


# ---------------------------------------------------------------------------
# Two subscribers on one deskewed image stream: identical frames, exactly
# one warp per tick -- real threads, real timing (this ticket's own AC
# wording), proven over a real socket rather than in-process queues.
# ---------------------------------------------------------------------------


def test_full_stack_two_subscribers_on_deskewed_stream_share_one_warp_per_tick_real_clock(
    directories, registry, short_socket_dir, monkeypatch
):
    service, slug = _calibrated_streaming_service(directories, registry)

    calls = {"n": 0}
    original_deskew = Deskewer.deskew

    def _counting_deskew(self, frame):  # noqa: ANN001, ANN202 - test double, mirrors Deskewer.deskew's own signature
        calls["n"] += 1
        return original_deskew(self, frame)

    monkeypatch.setattr(Deskewer, "deskew", _counting_deskew)

    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream1 = client.stream_images(slug, deskewed=True)
        stream2 = client.stream_images(slug, deskewed=True)
        # Keep both iterators referenced -- SocketImageStream.__iter__'s own
        # docstring warns a throwaway next(iter(stream)) closes the socket
        # as soon as the anonymous generator is garbage collected.
        it1 = iter(stream1)
        it2 = iter(stream2)
        stream1._sock.settimeout(10.0)  # noqa: SLF001 - bounded real read, test-hang safety net only
        stream2._sock.settimeout(10.0)  # noqa: SLF001
        try:
            received1: Dict[float, ImageFrame] = {}
            received2: Dict[float, ImageFrame] = {}
            deadline = time.monotonic() + 8.0
            while (len(received1) < 6 or len(received2) < 6) and time.monotonic() < deadline:
                if len(received1) < 6:
                    f = next(it1)
                    received1[f.timestamp] = f
                if len(received2) < 6:
                    f = next(it2)
                    received2[f.timestamp] = f

            assert len(received1) >= 4, "did not collect enough deskewed frames within the bounded window"
            assert len(received2) >= 4

            common = set(received1) & set(received2)
            assert len(common) >= 3, (
                f"expected substantial overlap between two continuously-read "
                f"subscribers on the same producer -- only {len(common)} shared "
                f"tick(s) out of {len(received1)}/{len(received2)}"
            )
            for ts in common:
                a, b = received1[ts], received2[ts]
                assert a.data == b.data, "two subscribers to the same tick received different jpeg bytes"
                assert a.width == b.width and a.height == b.height
                assert a.px_per_cm == b.px_per_cm
                assert a.world_extent == b.world_extent
                assert a.deskewed is True and b.deskewed is True

            # The structural proof: if the warp ran once *per subscriber*
            # rather than once *per tick*, calls["n"] would be roughly double
            # the number of distinct ticks observed. It is not.
            distinct_ticks = len(set(received1) | set(received2))
            assert calls["n"] >= distinct_ticks, "fewer warps than observed distinct frames -- impossible"
            assert calls["n"] <= distinct_ticks + 3, (
                f"warp called {calls['n']} times for only {distinct_ticks} distinct "
                f"published ticks -- looks like more than one warp per tick "
                f"(shared computation may have regressed)"
            )
        finally:
            stream1.close()
            stream2.close()


# ---------------------------------------------------------------------------
# A quiet tag stream heartbeats at the configured interval, measured
# against time.monotonic() at actual receipt -- not a fake clock's
# virtual-time bookkeeping.
# ---------------------------------------------------------------------------


def test_full_stack_quiet_tag_stream_heartbeats_at_configured_interval_real_clock(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)  # blank frame -- a quiet field, no tags

    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_tags(slug)
        it = iter(stream)
        stream._sock.settimeout(10.0)  # noqa: SLF001
        try:
            arrivals: List[float] = []
            for _ in range(3):
                frame = next(it)  # record the timestamp *after* the blocking read returns --
                arrivals.append(time.monotonic())  # that is the real receipt moment, not the call time
                assert isinstance(frame, TagFrame)
                assert frame.tags == ()  # genuinely quiet -- every publish is a heartbeat

            gaps = [arrivals[i + 1] - arrivals[i] for i in range(len(arrivals) - 1)]
            for gap in gaps:
                assert gap == pytest.approx(TAG_STREAM_HEARTBEAT_INTERVAL_S, abs=0.6), (
                    f"heartbeat gap {gap:.3f}s is not close to the configured "
                    f"{TAG_STREAM_HEARTBEAT_INTERVAL_S}s interval, measured "
                    f"against a real clock over a real socket"
                )
        finally:
            stream.close()


# ---------------------------------------------------------------------------
# Calibration going stale mid-stream reaches the very next published
# frame, on both the tag stream and the deskewed image stream -- real
# clock, real socket, the real service._mark_stale hook.
# ---------------------------------------------------------------------------


def test_full_stack_calibration_going_stale_mid_stream_reaches_tag_stream_real_clock(
    directories, registry, short_socket_dir
):
    service, slug = _calibrated_streaming_service(directories, registry)

    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_tags(slug)
        it = iter(stream)
        stream._sock.settimeout(10.0)  # noqa: SLF001
        try:
            first = next(it)
            assert first.calibration_stale is False

            service._mark_stale(slug)  # noqa: SLF001 - the established stale-injection hook

            deadline = time.monotonic() + 8.0
            found_stale = False
            while time.monotonic() < deadline:
                frame = next(it)
                if frame.calibration_stale:
                    found_stale = True
                    break
            assert found_stale, "calibration_stale never reached the tag stream within the bounded real-time window"
        finally:
            stream.close()


def test_full_stack_calibration_going_stale_mid_stream_reaches_image_stream_real_clock(
    directories, registry, short_socket_dir
):
    service, slug = _calibrated_streaming_service(directories, registry)

    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_images(slug, deskewed=True)
        it = iter(stream)
        stream._sock.settimeout(10.0)  # noqa: SLF001
        try:
            first = next(it)
            assert first.calibration_stale is False
            geometry_before = (first.px_per_cm, first.world_extent)

            service._mark_stale(slug)  # noqa: SLF001

            deadline = time.monotonic() + 8.0
            found_stale = False
            last_frame: ImageFrame = first
            while time.monotonic() < deadline:
                frame = next(it)
                last_frame = frame
                if frame.calibration_stale:
                    found_stale = True
                    break
            assert found_stale, "calibration_stale never reached the image stream within the bounded real-time window"
            # The staleness flip must not disturb the frozen stream-life geometry.
            assert (last_frame.px_per_cm, last_frame.world_extent) == geometry_before
        finally:
            stream.close()


# ---------------------------------------------------------------------------
# Unsubscribing (closing the client-side stream) is observed daemon-side
# to idle-gate within a bounded real-time window -- exercised
# concurrently across both products on one camera at once, a genuinely
# new scenario beyond ticket 006's single-product close tests.
# ---------------------------------------------------------------------------


def test_full_stack_closing_both_tag_and_image_streams_idle_gates_both_producers_real_clock(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)

    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        tag_stream = client.stream_tags(slug)
        image_stream = client.stream_images(slug, deskewed=False)
        it_tag = iter(tag_stream)
        it_image = iter(image_stream)
        next(it_tag)  # prove both subscriptions are really flowing first
        next(it_image)

        tag_producer = service._tag_stream_producers[slug]  # noqa: SLF001
        image_producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
        assert _wait_until(lambda: tag_producer.running is True)
        assert _wait_until(lambda: image_producer.running is True)

        close_started_at = time.monotonic()
        tag_stream.close()
        image_stream.close()

        assert _wait_until(lambda: tag_producer.running is False, timeout=5.0)
        assert _wait_until(lambda: image_producer.running is False, timeout=5.0)
        elapsed = time.monotonic() - close_started_at
        # Both products are torn down independently but within the same
        # bounded real-time window -- neither one's teardown is gated on
        # the other's.
        assert elapsed < 5.0, f"idle-gating took {elapsed:.2f}s -- outside the bounded window"


# ---------------------------------------------------------------------------
# An inert (never-read) client socket does not stall a second, actively
# reading client on the same producer -- the "does not stall" half of
# the slow/fast-subscriber claim, at the real-socket layer. See the
# module docstring for why the "measurably drops" half is verified
# separately, at the producer level, rather than here.
# ---------------------------------------------------------------------------


def test_full_stack_an_unread_client_socket_does_not_stall_an_active_client_real_clock(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)

    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        inert_stream = client.stream_images(slug, deskewed=False)
        active_stream = client.stream_images(slug, deskewed=False)
        it_inert = iter(inert_stream)  # connected, subscribed, deliberately never read again
        it_active = iter(active_stream)
        active_stream._sock.settimeout(5.0)  # noqa: SLF001
        try:
            received = 0
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                next(it_active)
                received += 1
            assert received >= 5, (
                f"active subscriber only received {received} frame(s) in 1.5s "
                f"real time while an inert sibling subscriber was connected -- "
                f"expected it to keep up unaffected"
            )
        finally:
            active_stream.close()
            inert_stream.close()
            del it_inert  # silence "unused" -- kept alive only to avoid premature close


# ---------------------------------------------------------------------------
# The tag-history ring buffer's wall-clock-bounded span (UC-26), checked
# against a real clock and a real tag-stream subscriber, over the full
# stack -- not merely asserted under a fake clock as ticket 003's own
# tests did.
# ---------------------------------------------------------------------------


def test_full_stack_ring_buffer_span_is_wall_clock_bounded_against_a_real_clock(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    baseline_len = len(service._detection.history(slug))  # noqa: SLF001

    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_tags(slug)
        it = iter(stream)
        stream._sock.settimeout(10.0)  # noqa: SLF001
        try:
            next(it)  # producer confirmed started
            start = time.monotonic()
            time.sleep(1.5)  # a real, measured wall-clock window
            elapsed = time.monotonic() - start
        finally:
            stream.close()

        assert _wait_until(lambda: service._tag_stream_producers[slug].running is False)  # noqa: SLF001

        history = service._detection.history(slug)[baseline_len:]  # noqa: SLF001
        assert len(history) > 1, "ring buffer did not grow while a tag-stream subscriber was open"

        span = history[-1].timestamp - history[0].timestamp
        # Generous bands -- real thread scheduling and a real (if trivial)
        # detection pipeline vary run to run; the point is "close to the
        # measured real duration", not exact equality.
        assert span > elapsed * 0.4, (
            f"ring buffer span {span:.2f}s is far shorter than the measured "
            f"{elapsed:.2f}s real subscription window -- buffer does not "
            f"look wall-clock bounded"
        )
        assert span < elapsed * 1.5, (
            f"ring buffer span {span:.2f}s is far longer than the measured "
            f"{elapsed:.2f}s real subscription window"
        )


# ===========================================================================
# Producer level, direct, real clock (no clock/wait override -- production's
# own time.monotonic()/bounded Event.wait, real threads)
# ===========================================================================


# ---------------------------------------------------------------------------
# A slow (never-drained) subscriber measurably drops frames while a fast
# (continuously-drained) subscriber on the very same producer is
# unaffected -- real clock version of test_streaming.py's fake-clock
# tests of the identical scenario, for both tag and image streams.
# ---------------------------------------------------------------------------


def test_tag_stream_slow_subscriber_drops_frames_without_stalling_fast_subscriber_real_clock():
    def build_hits(call_index: int):
        return [_pixel_hit(1, 20.0 * call_index, 0.0)]  # moves every tick -- always "changed"

    detector = _SequenceDetector(build_hits)
    engine = DetectionEngine(detector_factory=lambda _slug: detector, confirmation_streak=1)
    cam = _camera("cam-real-clock-slow-tags")
    producer = TagStreamProducer(
        "cam-real-clock-slow-tags",
        cam,
        engine,
        interval_s=0.005,
        heartbeat_interval_s=1000.0,
        max_publish_hz=1_000_000.0,  # every changed tick publishes
        change_threshold_px=1.0,
    )
    slow_q = producer.broadcaster.subscribe()
    fast_q: "queue.Queue[TagFrame]" = producer.broadcaster.subscribe()

    fast_received: List[TagFrame] = []
    stop_draining = threading.Event()

    def _drain_fast() -> None:
        while not stop_draining.is_set():
            try:
                fast_received.append(fast_q.get(timeout=0.05))
            except queue.Empty:
                continue

    drainer = threading.Thread(target=_drain_fast, daemon=True)
    drainer.start()
    try:
        producer.start()
        time.sleep(0.5)  # a real, short wall-clock window -- real thread scheduling throughout
        producer.stop()
    finally:
        stop_draining.set()
        drainer.join(timeout=2.0)

    assert slow_q.qsize() <= DEFAULT_SUBSCRIBER_QUEUE_DEPTH, (
        "the never-drained subscriber's queue grew past its bounded depth -- "
        "silent-drop backpressure did not hold under real timing"
    )
    assert len(fast_received) > DEFAULT_SUBSCRIBER_QUEUE_DEPTH, (
        "the continuously-drained subscriber received suspiciously few frames "
        "-- looks stalled by its slow sibling under real timing"
    )


def test_image_stream_slow_subscriber_drops_frames_without_stalling_fast_subscriber_real_clock():
    engine = DetectionEngine(confirmation_streak=1)
    cam = _camera("cam-real-clock-slow-images")
    producer = ImageStreamProducer("cam-real-clock-slow-images", cam, engine, interval_s=0.005)

    slow_q = producer.broadcaster.subscribe()
    fast_q: "queue.Queue[ImageFrame]" = producer.broadcaster.subscribe()

    fast_received: List[ImageFrame] = []
    stop_draining = threading.Event()

    def _drain_fast() -> None:
        while not stop_draining.is_set():
            try:
                fast_received.append(fast_q.get(timeout=0.05))
            except queue.Empty:
                continue

    drainer = threading.Thread(target=_drain_fast, daemon=True)
    drainer.start()
    try:
        producer.start()
        time.sleep(0.5)
        producer.stop()
    finally:
        stop_draining.set()
        drainer.join(timeout=2.0)

    assert slow_q.qsize() <= DEFAULT_SUBSCRIBER_QUEUE_DEPTH
    assert len(fast_received) > DEFAULT_SUBSCRIBER_QUEUE_DEPTH
