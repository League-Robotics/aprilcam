"""Tests for ``aprilcam.camera.camera`` (ticket 020-003).

Headless tests use :class:`FakeBackend`, a fully-controllable
``CaptureBackend`` test double — no hardware, no ``cv2`` behaviour
depended on. ``@pytest.mark.camera`` tests exercise the real
``OpenCVBackend`` against this Mac's two fixture cameras
(``arducam-ov9782-usb-camera``, ``hd-usb-camera``), located by name via
``cv2_enumerate_cameras`` at test-collection time and skipped cleanly
if either is not attached.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import pytest

from aprilcam.camera.backends import CameraNotEnumeratedError, ControlBackend
from aprilcam.camera.backends.opencv_backend import property_map
from aprilcam.camera.camera import _OPEN_SETTLE_READS, FAILURE_THRESHOLD, MAX_REOPENS, Camera
from aprilcam.camera.interfaces import Camera as CameraProtocol
from aprilcam.camera.interfaces import CameraLifecycle
from aprilcam.config.interfaces import CameraConfig, CameraPosition
from aprilcam.errors import UnsupportedSetting
from aprilcam.types import WorldPoint


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


class FakeBackend:
    """A ``CaptureBackend`` test double with fully controllable behaviour.

    ``read_queue`` items are popped one per ``read()`` call; once
    exhausted, ``read()`` returns ``None`` (a failure) as long as the
    backend is open — unless ``loop=True`` (ticket 027-003), in which
    case an exhausted queue is refilled from *read_queue* as originally
    given and popping continues, so a background reader (a
    ``daemon.streaming.TagStreamProducer``, in production) has
    continuous frames to read for as long as it runs, rather than
    degrading to "no frame available" once a short, fixed queue runs
    out — development-plan.md Stage 8's "fake-camera daemon (fixture
    replay backend)". ``fail_open`` makes ``open()`` raise instead of
    succeeding — for AC "present but unopenable" style scenarios.
    """

    def __init__(
        self,
        *,
        read_queue: Optional[List[Optional[np.ndarray]]] = None,
        fail_open: bool = False,
        fail_open_exception: Optional[BaseException] = None,
        size: Tuple[int, int] = (640, 480),
        read_delay: float = 0.0,
        loop: bool = False,
    ) -> None:
        self._is_open = False
        #: sprint 039 (ticket 003) -- when set, ``open()`` raises this
        #: exact exception instead of the generic ``RuntimeError`` below
        #: while ``fail_open`` is ``True``. Lets a test simulate
        #: ``CameraNotEnumeratedError`` specifically (distinct from an
        #: ordinary open failure) without needing a real ``OpenCVBackend``.
        #: ``None`` (the default) preserves every pre-existing
        #: ``fail_open=True`` test's plain ``RuntimeError`` unchanged.
        self.fail_open_exception = fail_open_exception
        #: ticket 027-003 -- the original frames, kept aside so a
        #: ``loop=True`` backend can refill ``self._queue`` from it once
        #: exhausted, rather than exhausting to ``None`` permanently.
        self._original_queue: List[Optional[np.ndarray]] = list(read_queue) if read_queue else []
        self._queue: List[Optional[np.ndarray]] = list(self._original_queue)
        self.fail_open = fail_open
        self._size = size
        #: ticket 027-003 -- when ``True`` and the queue is exhausted,
        #: ``read()`` refills it from the original frames instead of
        #: returning ``None`` -- the "looping/fixture-replay fake camera
        #: backend" this ticket's testing infrastructure needs so a
        #: continuously-running background producer never runs dry.
        #: Default ``False`` leaves every pre-existing exhaust-to-``None``
        #: test unaffected.
        self.loop = loop
        self.open_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self._properties: Dict[str, float] = {}
        #: ticket 027-002 -- an artificial delay inside read(), widening
        #: the window in which a concurrency bug (two threads calling
        #: into this backend at the same time) would actually manifest.
        #: Zero by default, so every pre-existing single-threaded test
        #: is unaffected.
        self.read_delay = read_delay
        #: ticket 027-002 -- a reentrancy detector, not a synchronization
        #: primitive. Set for the duration of any read()/open()/close()
        #: call and checked on entry to each; deliberately *not* guarded
        #: by a lock of its own, since the whole point is to catch
        #: ``Camera``'s read lock failing to serialize calls into this
        #: backend. ``reentrant_hits`` is asserted to be exactly ``0``
        #: after a concurrent test run -- the fake backend's own internal
        #: state never observed concurrent entry.
        self._busy = False
        self.reentrant_hits = 0

    def _enter(self) -> None:
        if self._busy:
            self.reentrant_hits += 1
        self._busy = True

    def _exit(self) -> None:
        self._busy = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self._enter()
        try:
            self.open_calls += 1
            if self.fail_open:
                raise self.fail_open_exception or RuntimeError("simulated open failure")
            self._is_open = True
        finally:
            self._exit()

    def read(self) -> Optional[np.ndarray]:
        self._enter()
        try:
            self.read_calls += 1
            if self.read_delay:
                time.sleep(self.read_delay)
            if not self._is_open:
                return None
            if not self._queue and self.loop and self._original_queue:
                self._queue = list(self._original_queue)
            if self._queue:
                return self._queue.pop(0)
            return None
        finally:
            self._exit()

    def close(self) -> None:
        self._enter()
        try:
            self.close_calls += 1
            self._is_open = False
        finally:
            self._exit()

    def frame_size(self) -> Optional[Tuple[int, int]]:
        return self._size if self._is_open else None

    # -- best-effort settings surface (mirrors OpenCVBackend) -----------

    def get_property(self, name: str) -> Optional[float]:
        return self._properties.get(name)

    def set_property(self, name: str, value: float) -> bool:
        self._properties[name] = value
        return True

    def settable_names(self):
        # Mirrors OpenCVBackend's real, generic key set -- so
        # "unknown key" tests stay meaningful against a realistic
        # boundary rather than an artificially permissive fake.
        return set(property_map())


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_camera_protocol():
    cam = Camera("cam-a", FakeBackend())
    assert isinstance(cam, CameraProtocol)


# ---------------------------------------------------------------------------
# open / close / state
# ---------------------------------------------------------------------------


def test_open_marks_present_and_usable():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    assert backend.open_calls == 1
    assert cam.state.present is True
    assert cam.state.usable is True


def test_open_propagates_backend_exception():
    backend = FakeBackend(fail_open=True)
    cam = Camera("cam-a", backend)
    with pytest.raises(RuntimeError):
        cam.open()
    assert cam.state.present is False
    assert cam.state.usable is False


def test_close_resets_present_and_usable():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cam.close()
    assert backend.close_calls == 1
    assert cam.state.present is False
    assert cam.state.usable is False


def test_state_carries_number_and_slug():
    cam = Camera("cam-a", FakeBackend(), number=3)
    assert cam.slug == "cam-a"
    assert cam.state.name == "cam-a"
    assert cam.state.number == 3
    assert cam.state.playfield is None
    assert cam.state.calibrated is False


# ---------------------------------------------------------------------------
# read() — never raises, returns None on failure (AC2)
# ---------------------------------------------------------------------------


def test_read_returns_frame_on_success():
    frame = _frame()
    backend = FakeBackend(read_queue=[frame])
    cam = Camera("cam-a", backend)
    cam.open()
    assert cam.read() is frame


def test_read_returns_none_on_failure_without_raising():
    backend = FakeBackend(read_queue=[])
    cam = Camera("cam-a", backend)
    cam.open()
    assert cam.read() is None


def test_read_before_open_returns_none():
    cam = Camera("cam-a", FakeBackend())
    assert cam.read() is None


# ---------------------------------------------------------------------------
# Self-heal reopen (AC2): 5 consecutive failures -> reopen, capped at 10
# ---------------------------------------------------------------------------


def test_reopen_triggers_after_five_consecutive_failures():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    assert backend.open_calls == 1

    for _ in range(FAILURE_THRESHOLD - 1):
        assert cam.read() is None
    assert backend.open_calls == 1  # not yet -- one short of the threshold

    assert cam.read() is None  # 5th consecutive failure
    assert backend.open_calls == 2  # self-heal reopened once
    assert backend.close_calls == 1


def test_successful_read_resets_failure_and_reopen_counters():
    backend = FakeBackend(read_queue=[None, None, None, None, _frame()])
    cam = Camera("cam-a", backend)
    cam.open()
    for _ in range(4):
        cam.read()
    assert cam.read() is not None  # the 5th read succeeds, before threshold trips
    assert backend.open_calls == 1  # no reopen -- the healthy frame arrived first

    # A fresh run of failures now needs its own 5 in a row to trigger.
    for _ in range(FAILURE_THRESHOLD - 1):
        cam.read()
    assert backend.open_calls == 1
    cam.read()
    assert backend.open_calls == 2


def test_reopen_budget_capped_at_ten_then_gives_up():
    backend = FakeBackend()  # open() always trivially succeeds; never yields frames
    cam = Camera("cam-a", backend)
    cam.open()

    # Drive FAILURE_THRESHOLD * MAX_REOPENS failed reads: exactly enough
    # to trigger MAX_REOPENS reopen attempts (all "successful" opens
    # that still produce no frames).
    for _ in range(FAILURE_THRESHOLD * MAX_REOPENS):
        cam.read()
    assert backend.open_calls == 1 + MAX_REOPENS
    assert cam.state.usable is True  # budget not spent yet on this exact boundary

    # One more full failure run trips the exhausted path: no further
    # reopen attempt, and usable now reflects "gave up" even though
    # the fake backend's open() would have happily succeeded again.
    for _ in range(FAILURE_THRESHOLD):
        cam.read()
    assert backend.open_calls == 1 + MAX_REOPENS  # unchanged -- budget spent
    assert cam.state.present is True  # "plugged in and broken", not "gone"
    assert cam.state.usable is False


def test_reopen_failure_leaves_camera_unusable_but_present():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()

    backend.fail_open = True  # the reopen attempt itself will now fail
    for _ in range(FAILURE_THRESHOLD):
        cam.read()
    assert backend.close_calls == 1
    assert backend.open_calls == 2  # the failed reopen attempt
    assert cam.state.present is True
    assert cam.state.usable is False  # backend.is_open is False after the failed reopen


# ---------------------------------------------------------------------------
# check_stall (sprint 038 ticket 005): the proactive half of self-heal --
# a backend heartbeat check that fires _attempt_self_heal with nobody
# calling read() at all.
# ---------------------------------------------------------------------------


class _FakeHeartbeatBackend(FakeBackend):
    """:class:`FakeBackend` plus a settable ``heartbeat_age()`` -- the
    only difference between a backend :meth:`Camera.check_stall`
    duck-types as heartbeat-capable (``WorkerBackend``, in production)
    and the plain :class:`FakeBackend` every other test in this module
    uses, which has no ``heartbeat_age`` attribute at all."""

    def __init__(self, *, heartbeat_age: Optional[float], **kwargs) -> None:
        super().__init__(**kwargs)
        self._heartbeat_age = heartbeat_age
        self.heartbeat_age_calls = 0

    def heartbeat_age(self) -> Optional[float]:
        self.heartbeat_age_calls += 1
        return self._heartbeat_age


def test_check_stall_is_a_noop_for_a_backend_without_heartbeat_age():
    """A plain FakeBackend (mirrors OpenCVBackend/V4l2LoopbackBackend --
    no heartbeat_age at all) makes check_stall an unconditional no-op:
    returns False, never touches the backend."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()

    assert cam.check_stall() is False
    assert backend.open_calls == 1  # unchanged
    assert backend.close_calls == 0


def test_check_stall_noop_when_heartbeat_age_is_none():
    """heartbeat_age() itself reporting None (e.g. WorkerBackend not
    currently open) is also a no-op -- distinct from the method not
    existing at all, but the same result."""
    backend = _FakeHeartbeatBackend(heartbeat_age=None)
    cam = Camera("cam-a", backend)
    cam.open()

    assert cam.check_stall(threshold_s=5.0) is False
    assert backend.heartbeat_age_calls == 1
    assert backend.open_calls == 1


def test_check_stall_noop_when_heartbeat_is_fresh():
    backend = _FakeHeartbeatBackend(heartbeat_age=0.1)
    cam = Camera("cam-a", backend)
    cam.open()

    assert cam.check_stall(threshold_s=5.0) is False
    assert backend.open_calls == 1
    assert backend.close_calls == 0


def test_check_stall_triggers_self_heal_exactly_once_on_a_stale_heartbeat():
    backend = _FakeHeartbeatBackend(heartbeat_age=999.0)
    cam = Camera("cam-a", backend)
    cam.open()

    assert cam.check_stall(threshold_s=5.0) is True
    assert backend.close_calls == 1
    assert backend.open_calls == 2  # self-heal reopened once


def test_check_stall_age_exactly_at_threshold_is_not_stale():
    """Boundary: an age equal to (not past) threshold_s is still
    healthy -- matches the docstring's "at or below is healthy"."""
    backend = _FakeHeartbeatBackend(heartbeat_age=5.0)
    cam = Camera("cam-a", backend)
    cam.open()

    assert cam.check_stall(threshold_s=5.0) is False
    assert backend.open_calls == 1


def test_check_stall_respects_max_reopens_budget():
    """Driving repeated self-heal attempts through check_stall alone --
    never through read() -- still respects MAX_REOPENS: no bonus reopen
    beyond what _attempt_self_heal already allows any caller."""
    backend = _FakeHeartbeatBackend(heartbeat_age=999.0)  # permanently stalled
    cam = Camera("cam-a", backend)
    cam.open()

    for _ in range(MAX_REOPENS):
        assert cam.check_stall(threshold_s=5.0) is True
    assert backend.open_calls == 1 + MAX_REOPENS
    assert cam.state.usable is True  # budget not spent yet on this exact boundary

    cam.check_stall(threshold_s=5.0)  # one more -- trips the exhausted path
    assert backend.open_calls == 1 + MAX_REOPENS  # unchanged -- no bonus reopen
    assert cam.state.present is True
    assert cam.state.usable is False


def test_check_stall_does_not_deadlock_with_a_concurrent_read():
    """check_stall reuses _read_lock rather than a new lock (AC5): a
    stall check racing an in-flight read() on another thread must
    serialize cleanly, never deadlock, and never corrupt the
    failure/reopen counters both paths share."""
    backend = _FakeHeartbeatBackend(heartbeat_age=999.0, read_delay=0.02, loop=True)
    cam = Camera("cam-a", backend)
    cam.open()

    stop = threading.Event()
    errors: List[BaseException] = []

    def _reader() -> None:
        while not stop.is_set():
            try:
                cam.read()
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)
                return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            cam.check_stall(threshold_s=1e9)  # never actually stale -- just exercising the lock
    finally:
        stop.set()
        reader.join(timeout=5.0)

    assert not reader.is_alive()  # joined cleanly, no deadlock
    assert errors == []


# ---------------------------------------------------------------------------
# Identity-safe reopen (sprint 039 ticket 003): _attempt_self_heal treats a
# CameraNotEnumeratedError reopen (a backend's index_resolver reporting "not
# currently enumerated") as a distinct, budget-neutral case -- never the same
# as an ordinary "present but broken" open failure. See
# camera-DESIGN.md's "Identity-safe reopen" section and this module's own
# docstring on _attempt_self_heal for the two options the ticket named; this
# is option (a).
# ---------------------------------------------------------------------------


def test_self_heal_not_enumerated_is_budget_neutral_and_marks_absent():
    backend = FakeBackend(fail_open=True, fail_open_exception=CameraNotEnumeratedError("gone"))
    cam = Camera("cam-a", backend)
    backend.fail_open = False
    cam.open()
    assert cam.state.present is True

    backend.fail_open = True  # simulate the hardware key vanishing from enumeration
    for _ in range(FAILURE_THRESHOLD):
        assert cam.read() is None

    # Unlike an ordinary failed reopen (present stays True, one reopen
    # spent), a not-currently-enumerated reopen is positive evidence the
    # hardware itself is gone, and spends nothing from the budget.
    assert cam.state.present is False
    assert cam.state.usable is False
    assert cam._reopens == 0  # noqa: SLF001 -- internal; this *is* "budget-neutral"
    assert cam._exhausted is False  # noqa: SLF001


def test_self_heal_not_enumerated_never_exhausts_the_budget():
    """Contrast with test_check_stall_respects_max_reopens_budget /
    test_on_reopen_not_invoked_once_the_reopen_budget_is_exhausted:
    an ordinary present-but-broken camera exhausts MAX_REOPENS and stops
    trying; a not-currently-enumerated camera never does, however many
    read-failure streaks it goes through -- the daemon's watchdog can
    keep retrying the identity lookup indefinitely."""
    backend = FakeBackend(fail_open=True, fail_open_exception=CameraNotEnumeratedError("gone"))
    cam = Camera("cam-a", backend)
    backend.fail_open = False
    cam.open()
    backend.fail_open = True

    for _ in range(MAX_REOPENS + 3):
        for _ in range(FAILURE_THRESHOLD):
            assert cam.read() is None

    assert cam._reopens == 0  # noqa: SLF001
    assert cam._exhausted is False  # noqa: SLF001
    assert cam.state.present is False
    assert cam.state.usable is False


def test_on_reopen_not_invoked_when_reopen_skipped_as_not_enumerated():
    backend = FakeBackend(fail_open=True, fail_open_exception=CameraNotEnumeratedError("gone"))
    calls = []
    cam = Camera("cam-a", backend, on_reopen=lambda: calls.append(1))
    backend.fail_open = False
    cam.open()

    backend.fail_open = True
    for _ in range(FAILURE_THRESHOLD):
        cam.read()

    assert calls == []


def test_retry_if_exhausted_not_enumerated_marks_absent_but_stays_exhausted():
    """retry_if_exhausted's own not-enumerated handling (camera.py):
    present goes False, same as _attempt_self_heal's, but _exhausted is
    untouched (this method's normal failure contract) -- the daemon's
    STALLED_CAMERA_RETRY_INTERVAL_S watchdog pass is what keeps calling
    this again, exactly like any other failed exhausted-camera retry."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()

    backend.fail_open = True  # every reopen attempt from here fails
    # MAX_REOPENS triggers spend the budget; one more trips _exhausted
    # itself (mirrors test_check_stall_respects_max_reopens_budget's own
    # "one more -- trips the exhausted path" step).
    for _ in range(FAILURE_THRESHOLD * (MAX_REOPENS + 1)):
        cam.read()
    assert cam._exhausted is True  # noqa: SLF001 -- ordinary exhaustion first

    backend.fail_open_exception = CameraNotEnumeratedError("gone")
    assert cam.retry_if_exhausted() is False
    assert cam.state.present is False
    assert cam._exhausted is True  # noqa: SLF001 -- retry_if_exhausted never clears this on failure


# ---------------------------------------------------------------------------
# on_reopen (ticket 023-007): staleness trigger notification hook, fired at
# the end of a *successful* self-heal reopen only -- never on plain open(),
# never on a failed reopen attempt, never once the reopen budget is spent.
# ---------------------------------------------------------------------------


def test_on_reopen_invoked_after_a_successful_self_heal_reopen():
    backend = FakeBackend()
    calls = []
    cam = Camera("cam-a", backend, on_reopen=lambda: calls.append(1))
    cam.open()

    for _ in range(FAILURE_THRESHOLD):
        cam.read()

    assert backend.open_calls == 2  # the reopen genuinely happened
    assert calls == [1]


def test_on_reopen_not_invoked_by_the_initial_open():
    backend = FakeBackend()
    calls = []
    cam = Camera("cam-a", backend, on_reopen=lambda: calls.append(1))

    cam.open()

    assert calls == []


def test_on_reopen_not_invoked_on_a_failed_reopen_attempt():
    backend = FakeBackend()
    calls = []
    cam = Camera("cam-a", backend, on_reopen=lambda: calls.append(1))
    cam.open()

    backend.fail_open = True  # the reopen attempt itself will now fail
    for _ in range(FAILURE_THRESHOLD):
        cam.read()

    assert backend.open_calls == 2  # the failed reopen attempt happened
    assert calls == []  # but it never counts as a successful reopen


def test_on_reopen_invoked_once_per_successful_reopen():
    backend = FakeBackend()
    calls = []
    cam = Camera("cam-a", backend, on_reopen=lambda: calls.append(1))
    cam.open()

    for _ in range(3):
        for _ in range(FAILURE_THRESHOLD):
            cam.read()

    assert backend.open_calls == 4  # initial open + 3 successful reopens
    assert calls == [1, 1, 1]


def test_on_reopen_not_invoked_once_the_reopen_budget_is_exhausted():
    backend = FakeBackend()  # open() always trivially succeeds; never yields frames
    calls = []
    cam = Camera("cam-a", backend, on_reopen=lambda: calls.append(1))
    cam.open()

    for _ in range(FAILURE_THRESHOLD * MAX_REOPENS):
        cam.read()
    assert len(calls) == MAX_REOPENS  # every reopen attempt in the budget succeeded
    assert cam.state.usable is True

    calls.clear()
    for _ in range(FAILURE_THRESHOLD):
        cam.read()  # budget spent -- self-heal gives up before reopening
    assert cam.state.usable is False
    assert calls == []  # no further notification once nothing further happened


def test_default_on_reopen_is_none_and_self_heal_still_works():
    """No ``on_reopen`` passed (every pre-ticket-007 caller) -- self-heal
    must behave exactly as before, never raising for lack of a listener."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()

    for _ in range(FAILURE_THRESHOLD):
        cam.read()

    assert backend.open_calls == 2  # reopened without error


# ---------------------------------------------------------------------------
# retry_if_exhausted (sprint 038 ticket 006): the periodic-retry half of the
# exhausted-budget contract -- daemon.service.DaemonService's watchdog calls
# this on a slow interval to give an exhausted camera one fresh close/open
# attempt instead of requiring a full daemon restart.
# ---------------------------------------------------------------------------


def _exhaust_via_reads(cam: Camera) -> None:
    """Drive *cam* to the exhausted state purely through :meth:`Camera.read`
    failures -- the same drive ``test_reopen_budget_capped_at_ten_then_gives_up``
    uses: ``FAILURE_THRESHOLD * MAX_REOPENS`` failures spend the whole
    reopen budget (``usable`` is still ``True`` on this exact boundary --
    the budget is spent, not yet detected as exhausted), then one more
    ``FAILURE_THRESHOLD`` run trips the exhausted path for real."""
    for _ in range(FAILURE_THRESHOLD * MAX_REOPENS):
        cam.read()
    for _ in range(FAILURE_THRESHOLD):
        cam.read()


def test_retry_if_exhausted_is_a_noop_when_not_exhausted():
    """A camera that has not spent its reopen budget yet has nothing for
    this method to do -- its ordinary self-heal already covers it."""
    backend = FakeBackend(read_queue=[_frame()])
    cam = Camera("cam-a", backend)
    cam.open()

    assert cam.retry_if_exhausted() is False
    assert backend.open_calls == 1  # unchanged -- never touched the backend
    assert backend.close_calls == 0


def test_retry_if_exhausted_recovers_an_exhausted_camera():
    """Once MAX_REOPENS is spent, one call gives the camera a fresh
    close/open attempt: a FakeBackend's open() trivially succeeds, so the
    retry succeeds, clears the exhausted state, and usable becomes True
    again -- without a daemon restart."""
    backend = FakeBackend()  # open() always trivially succeeds; never yields frames
    cam = Camera("cam-a", backend)
    cam.open()
    _exhaust_via_reads(cam)
    assert cam.state.usable is False  # budget genuinely spent

    opens_before = backend.open_calls
    assert cam.retry_if_exhausted() is True
    assert backend.close_calls >= 1
    assert backend.open_calls == opens_before + 1  # exactly one fresh attempt
    assert cam.state.usable is True  # exhausted state cleared


def test_retry_if_exhausted_fires_on_reopen_on_a_successful_retry():
    """A successful retry clears the exhausted state exactly the way a
    normal successful reopen already does -- including firing
    *on_reopen*, the same staleness-notification hook (ticket 023-007)."""
    backend = FakeBackend()
    calls = []
    cam = Camera("cam-a", backend, on_reopen=lambda: calls.append(1))
    cam.open()
    _exhaust_via_reads(cam)
    calls.clear()  # ignore whatever fired during the exhaustion drive itself

    assert cam.retry_if_exhausted() is True
    assert calls == [1]


def test_retry_if_exhausted_leaves_camera_unusable_when_the_retry_itself_fails():
    """The reopen attempt's own open() can still fail (camera genuinely
    still unplugged) -- the retry is then reported as failed, and the
    camera stays unusable so the watchdog knows to try again later."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    _exhaust_via_reads(cam)

    backend.fail_open = True  # the retry's own reopen attempt will fail
    assert cam.retry_if_exhausted() is False
    assert cam.state.usable is False


def test_retry_if_exhausted_respects_the_read_lock():
    """Runs under the same ``_read_lock`` :meth:`read`/:meth:`check_stall`
    already use -- a concurrent read() must never interleave with a retry
    attempt against the same backend handle."""
    backend = FakeBackend(read_delay=0.02, loop=True)
    cam = Camera("cam-a", backend)
    cam.open()
    _exhaust_via_reads(cam)

    stop = threading.Event()
    errors: List[BaseException] = []

    def _reader() -> None:
        while not stop.is_set():
            try:
                cam.read()
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)
                return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            cam.retry_if_exhausted()
    finally:
        stop.set()
        reader.join(timeout=5.0)

    assert not reader.is_alive()
    assert errors == []
    assert backend.reentrant_hits == 0


# ---------------------------------------------------------------------------
# Concurrent read() (ticket 027-002): a background stream producer
# (tickets 027-003/004) and one-shot RPC reads now call read() on the
# same Camera from different threads. These tests prove Camera._read_lock
# makes that safe: no two threads ever run inside the same FakeBackend
# call at once (FakeBackend.reentrant_hits stays 0), self-heal reopen
# stays correct under concurrent pressure, and nothing deadlocks (every
# join() below is timeout-bounded so a real deadlock fails the test
# instead of hanging the suite).
# ---------------------------------------------------------------------------


def _run_concurrent(worker, thread_count: int, join_timeout: float = 10.0) -> None:
    """Start *thread_count* threads all running *worker*, join them all
    with a bounded timeout, and assert every one actually finished --
    the shared deadlock guard every test below uses."""
    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=join_timeout)
    assert not any(t.is_alive() for t in threads), (
        "a thread did not finish within the timeout -- possible deadlock"
    )


def test_concurrent_reads_never_interleave_backend_calls():
    """Two threads calling ``Camera.read()`` concurrently against the
    same backend must never overlap their calls into it. An artificial
    per-read delay widens the race window so the bug would reliably
    manifest if the lock were missing or scoped wrong."""
    reads_per_thread = 100
    thread_count = 4
    total = reads_per_thread * thread_count
    frames = [_frame() for _ in range(total)]
    backend = FakeBackend(read_queue=frames, read_delay=0.002)
    cam = Camera("cam-a", backend)
    cam.open()

    errors: List[BaseException] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        for _ in range(reads_per_thread):
            try:
                cam.read()
            except BaseException as exc:  # pragma: no cover - failure path
                with errors_lock:
                    errors.append(exc)

    _run_concurrent(worker, thread_count)

    assert errors == []
    assert backend.reentrant_hits == 0  # no two threads were ever inside the backend together
    assert backend.read_calls == total  # every call landed exactly once -- none lost, none duplicated


def test_concurrent_reads_self_heal_reopen_stays_correct_under_pressure():
    """Self-heal reopen (5 consecutive failures -> reopen, capped at 10)
    must stay correct when the failures driving it come from multiple
    threads racing on the same ``Camera``. Because ``read()`` holds the
    lock for its whole body -- including the self-heal reopen it may
    trigger -- every call is fully serialized, so the outcome is exactly
    what a single-threaded run of the same total call count would
    produce, regardless of which thread happens to execute which call.
    """
    reads_per_thread = 25
    thread_count = 4
    total = reads_per_thread * thread_count  # 100 -- well past FAILURE_THRESHOLD * MAX_REOPENS (50)
    backend = FakeBackend(read_queue=[], read_delay=0.002)  # every read fails
    cam = Camera("cam-a", backend)
    cam.open()

    errors: List[BaseException] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        for _ in range(reads_per_thread):
            try:
                cam.read()
            except BaseException as exc:  # pragma: no cover - failure path
                with errors_lock:
                    errors.append(exc)

    _run_concurrent(worker, thread_count)

    assert errors == []
    assert backend.reentrant_hits == 0
    assert backend.read_calls == total
    # Deterministic regardless of thread interleaving: with every read
    # failing, the reopen budget is exhausted after MAX_REOPENS reopens
    # (FAILURE_THRESHOLD * MAX_REOPENS = 50 failed reads), and never
    # exceeds that no matter how the 100 calls are split across threads.
    assert backend.open_calls == 1 + MAX_REOPENS
    assert cam.state.usable is False  # budget spent -- "plugged in and broken"
    assert cam.state.present is True


def test_concurrent_reads_do_not_deadlock():
    """A dedicated deadlock check: many threads reading concurrently
    (including self-heal-triggering failures) must all finish within a
    bounded timeout. If the lock were held across the wrong scope (e.g.
    re-acquired non-reentrantly from within itself), this test would
    hang until the timeout and then fail, rather than hanging the whole
    suite forever."""
    # Alternate success/failure frames so both the plain-read path and
    # the self-heal path are exercised concurrently.
    frames: List[Optional[np.ndarray]] = []
    for i in range(200):
        frames.append(_frame() if i % 7 else None)
    backend = FakeBackend(read_queue=frames, read_delay=0.001)
    cam = Camera("cam-a", backend)
    cam.open()

    def worker() -> None:
        for _ in range(50):
            cam.read()

    _run_concurrent(worker, thread_count=4, join_timeout=15.0)

    assert backend.reentrant_hits == 0


# ---------------------------------------------------------------------------
# effective_config() — introspected facts (AC3)
# ---------------------------------------------------------------------------


def test_effective_config_reports_device_name_and_frame_dims_when_open():
    backend = FakeBackend(size=(1920, 1080))
    cam = Camera("cam-a", backend, device_name="Arducam OV9782 USB Camera")
    cam.open()
    cfg = cam.effective_config()
    assert cfg.name == "cam-a"
    assert cfg.settings["device_name"] == "Arducam OV9782 USB Camera"
    assert cfg.settings["frame_width"] == 1920
    assert cfg.settings["frame_height"] == 1080


def test_effective_config_reports_none_dims_when_not_open():
    cam = Camera("cam-a", FakeBackend(), device_name="Cam A")
    cfg = cam.effective_config()
    assert cfg.settings["frame_width"] is None
    assert cfg.settings["frame_height"] is None


def test_effective_config_reports_every_primary_backend_settable_key_blank():
    """AC1 (ticket 023-002): the template's settable-key set comes from
    the real backend's own ``settable_names()`` -- not the old fixed
    ``device_name``/dimensions placeholder -- and every key beyond the
    two introspected dimensions is blank, no ``control_backend``
    involved (the common case: every non-macOS platform)."""
    backend = FakeBackend(size=(640, 480))
    cam = Camera("cam-a", backend, device_name="Cam A")
    cam.open()

    cfg = cam.effective_config()

    for key in property_map():
        assert key in cfg.settings, f"{key} missing from the template"
    for key in set(property_map()) - {"frame_width", "frame_height"}:
        assert cfg.settings[key] is None, f"{key} should be blank in the template"
    # An unconfigured camera's introspected facts are still real, not
    # fabricated -- the placeholder pair this ticket replaces.
    assert cfg.settings["device_name"] == "Cam A"
    assert cfg.settings["frame_width"] == 640
    assert cfg.settings["frame_height"] == 480


def test_effective_config_settable_keys_come_from_control_backend_when_given():
    """When a narrower ``control_backend`` is wired (ticket 023-001's
    real-hardware case), the template's settable-key set -- and its
    blankness -- comes from *that* backend's own ``settable_names()``,
    not the primary backend's full generic set (``brightness`` is in
    ``FakeBackend``'s set but must not leak into the template once a
    narrower ``UvcUtilBackend``-shaped control backend is wired). A key's
    template value stays blank even when the control backend's own
    ``get_property`` would report a real current hardware value --
    ``effective_config`` never calls it for non-resolution keys (see the
    method's own docstring), so a real value already in force is not
    mistaken for "configured this session"."""
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain", "white_balance_temperature"))
    control.set_property("gain", 42.0)  # a real hardware value -- still blank in the template
    cam = Camera("cam-a", primary, control_backend=control, device_name="Cam A")
    cam.open()

    cfg = cam.effective_config()

    assert "brightness" not in cfg.settings  # primary-only key, not in control's narrower set
    for key in ("exposure", "gain", "white_balance_temperature"):
        assert cfg.settings[key] is None
    # Resolution stays on the primary backend and is real, per AC2 --
    # present in the template even though control_backend knows nothing
    # about it at all.
    assert cfg.settings["frame_width"] == 640
    assert cfg.settings["frame_height"] == 480


def test_camera_config_round_trips_to_dict():
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"k": 1})
    d = cfg.to_dict()
    assert d == {"name": "cam-a", "playfield": None, "settings": {"k": 1}}


def test_apply_config_ignores_camera_position_sibling_field():
    """Ticket 029-001's own regression: ``Camera.apply_config`` needs no
    code change for the new ``camera_position`` field -- it iterates
    ``config.settings`` only, so a config push carrying
    ``camera_position`` (with or without ``settings`` changes) applies
    cleanly to a held-open camera. ``camera_position`` is operator-
    asserted metadata that never touches hardware (config/interfaces.py's
    ``CameraPosition`` docstring)."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cfg = CameraConfig(
        name="cam-a",
        playfield="main-playfield",
        settings={"gain": 3.0},
        camera_position=CameraPosition(nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25),
    )

    cam.apply_config(cfg)  # must not raise

    assert backend.get_property("gain") == 3.0

    # Also applies cleanly with no settings changes at all -- the
    # sibling field is invisible to apply_config by construction.
    cfg_no_settings = CameraConfig(
        name="cam-a",
        playfield="main-playfield",
        settings={},
        camera_position=CameraPosition(nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25),
    )
    cam.apply_config(cfg_no_settings)  # must not raise


# ---------------------------------------------------------------------------
# apply_config() — hard errors, ordering, atomic rollback
# ---------------------------------------------------------------------------


def test_apply_config_requires_open_camera():
    cam = Camera("cam-a", FakeBackend())
    cfg = CameraConfig(name="cam-a", playfield=None, settings={})
    with pytest.raises(RuntimeError):
        cam.apply_config(cfg)


def test_apply_config_unknown_key_raises_unsupported_setting():
    cam = Camera("cam-a", FakeBackend())
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"not_a_real_key": 1})
    with pytest.raises(UnsupportedSetting) as exc_info:
        cam.apply_config(cfg)
    assert exc_info.value.key == "not_a_real_key"
    # ticket 030-005: an unknown key names its own cause -- distinct
    # from a *known* key that a live write rejected.
    assert exc_info.value.reason is not None
    assert "unsupported or rejected setting: 'not_a_real_key'" in str(exc_info.value)


def test_apply_config_applies_gate_keys_before_dependents():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    order: List[str] = []
    original_set = backend.set_property

    def spy_set(name, value):
        order.append(name)
        return original_set(name, value)

    backend.set_property = spy_set  # type: ignore[method-assign]
    cfg = CameraConfig(
        name="cam-a",
        playfield=None,
        settings={"exposure": 100, "auto_exposure": 1},
    )
    cam.apply_config(cfg)
    assert order == ["auto_exposure", "exposure"]


def test_apply_config_blank_settings_are_skipped():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"gain": None})
    cam.apply_config(cfg)  # no error, nothing pushed
    assert backend.get_property("gain") is None


def test_apply_config_rolls_back_on_mid_push_failure():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    backend.set_property("gain", 5.0)  # pre-existing value to roll back to

    def failing_set(name, value):
        if name == "exposure":
            return False
        backend._properties[name] = value
        return True

    backend.set_property = failing_set  # type: ignore[method-assign]
    cfg = CameraConfig(
        name="cam-a", playfield=None, settings={"gain": 9.0, "exposure": 100}
    )
    with pytest.raises(UnsupportedSetting):
        cam.apply_config(cfg)
    assert backend.get_property("gain") == 5.0  # rolled back


# ---------------------------------------------------------------------------
# non-numeric settings values (2026-08-22 hotfix) -- camera 3's real,
# on-disk config held "exposure": "100" (a JSON *string*), which
# survived config/loader.py's own (then-nonexistent) type checking and
# crashed deep inside UvcUtilBackend.describe_set_failure's `minimum <=
# value <= maximum` with a bare, untyped TypeError instead of a clean
# UnsupportedSetting. config/loader.py's parse_camera_config now rejects
# this at load time (tests/aprilcam/config/test_loader.py), but this is
# the one place every settings value is actually used regardless of
# where it came from, so it is also where the last-resort guarantee
# belongs.
# ---------------------------------------------------------------------------


def test_apply_config_non_numeric_setting_raises_typed_error_not_typeerror():
    """A settings value ``float()`` cannot convert (e.g. a config
    document built by hand, bypassing config/loader.py's own
    validation) must raise a clean, typed ``UnsupportedSetting`` naming
    the camera and the key -- never a bare ``TypeError``/``ValueError``
    escaping ``apply_config``."""
    cam = Camera("cam-a", FakeBackend())
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"exposure": "not-a-number"})
    with pytest.raises(UnsupportedSetting) as exc_info:
        cam.apply_config(cfg)
    assert exc_info.value.key == "exposure"
    assert exc_info.value.reason is not None
    assert "not-a-number" in exc_info.value.reason
    assert "cam-a" in exc_info.value.reason


def test_apply_config_numeric_string_setting_still_applies():
    """A numeric-looking string (camera 3's exact real value, "100")
    is coerced and pushed normally -- config/loader.py's own validation
    is what normally catches/normalises this before it ever reaches
    here, but apply_config itself must not choke on it either (defense
    in depth, matching real hardware: ``set_property`` genuinely accepts
    ``float("100")``, only the *diagnostic* path used to choke)."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"exposure": "100"})
    cam.apply_config(cfg)
    assert backend.get_property("exposure") == 100.0


def test_apply_config_non_numeric_setting_rolls_back_previously_applied_keys():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    backend.set_property("gain", 5.0)  # pre-existing value to roll back to

    cfg = CameraConfig(name="cam-a", playfield=None, settings={"gain": 9.0, "exposure": "bogus"})
    with pytest.raises(UnsupportedSetting):
        cam.apply_config(cfg)
    assert backend.get_property("gain") == 5.0  # rolled back


def test_apply_config_describe_set_failure_receives_a_coerced_float_not_the_raw_string():
    """The exact real-hardware bug (camera 3's real traceback):
    ``UvcUtilBackend.describe_set_failure`` compares ``minimum <= value
    <= maximum`` -- an un-coerced string reaching it raises a bare
    ``TypeError``. ``Camera.apply_config`` must pass
    ``_describe_set_failure`` the already-coerced float, never
    ``config.settings[key]`` verbatim."""
    backend = FakeControlBackend(names=("exposure",))
    backend.set_property = lambda name, value: False  # type: ignore[method-assign]
    seen: list = []

    def spy_describe(name, value):
        seen.append(value)
        # Mirrors UvcUtilBackend.describe_set_failure's own comparison,
        # which raises TypeError if *value* is still a str here.
        assert 0.0 <= value <= 1000.0
        return "rejected"

    backend.describe_set_failure = spy_describe  # type: ignore[attr-defined]
    cam = Camera("cam-a", FakeBackend(), control_backend=backend)
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"exposure": "100"})
    with pytest.raises(UnsupportedSetting):
        cam.apply_config(cfg)
    assert seen == [100.0]
    assert isinstance(seen[0], float)


def test_apply_config_gated_key_failure_names_the_missing_gate_when_backend_has_no_diagnostic(
) -> None:
    """Ticket 030-005: ``FakeBackend`` has no ``describe_set_failure``
    (a bare ``CaptureBackend``-shaped double, like ``OpenCVBackend``),
    so ``Camera._describe_set_failure`` falls back to its own
    config-shape check -- ``exposure`` is gated by ``auto_exposure``
    (``_GATED_BY``) and this push never includes it, exactly the
    real-world "test never sets auto_exposure" candidate the ticket's
    source issue named as one of four possibilities."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()

    def failing_set(name, value):
        return False

    backend.set_property = failing_set  # type: ignore[method-assign]
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"exposure": 100})
    with pytest.raises(UnsupportedSetting) as exc_info:
        cam.apply_config(cfg)
    assert exc_info.value.key == "exposure"
    assert exc_info.value.reason is not None
    assert "auto_exposure" in exc_info.value.reason


def test_apply_config_prefers_the_backends_own_diagnostic_over_the_generic_gate_fallback():
    """A backend that *can* say more (e.g. ``UvcUtilBackend``, live
    hardware readback) wins over ``Camera``'s own config-shape-only
    fallback, even for a key ``_GATED_BY`` also knows about."""
    backend = FakeControlBackend(names=("exposure", "auto_exposure"))
    backend.set_property = lambda name, value: False  # type: ignore[method-assign]
    backend.describe_set_failure = lambda name, value: f"backend says: {name} is doomed"  # type: ignore[attr-defined]
    cam = Camera("cam-a", FakeBackend(), control_backend=backend)
    cam.open()

    cfg = CameraConfig(name="cam-a", playfield=None, settings={"exposure": 100, "auto_exposure": 1})
    with pytest.raises(UnsupportedSetting) as exc_info:
        cam.apply_config(cfg)
    assert exc_info.value.reason == "backend says: auto_exposure is doomed"


def test_apply_config_backend_without_settings_surface_rejects_any_push():
    class NoSettingsBackend:
        """A minimal CaptureBackend with no get_property/set_property at
        all -- mirrors V4l2LoopbackBackend, whose stream format is fixed
        by its writer (v1's comment on V4l2CtlCapture.set())."""

        def __init__(self) -> None:
            self._is_open = False

        @property
        def is_open(self) -> bool:
            return self._is_open

        def open(self) -> None:
            self._is_open = True

        def read(self):
            return None

        def close(self) -> None:
            self._is_open = False

        def frame_size(self):
            return None

    backend = NoSettingsBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cfg = CameraConfig(name="cam-a", playfield=None, settings={"gain": 1})
    with pytest.raises(UnsupportedSetting):
        cam.apply_config(cfg)
    # An empty push against such a backend is fine -- nothing to reject.
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={}))


def test_reset_without_config_loader_or_path_falls_back_to_blank_template():
    """No ``config_loader``/``config_path`` wired at all (every
    pre-existing test, and any ``Camera`` built by hand rather than
    through ``DaemonService._camera_for``) -- ticket 023-004's
    Decision 3 fallback: there is nothing to revert to, so ``reset()``
    applies the blank introspected-plus-blank template, an honest
    no-op on hardware rather than an error."""
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    backend.set_property("gain", 42.0)  # a pre-existing "live" value

    cam.reset()  # no error

    assert backend.get_property("gain") == 42.0  # nothing pushed -- unchanged


def test_reset_with_no_installed_file_falls_back_to_blank_template(tmp_path):
    """A ``config_loader``/``config_path`` *were* given, but no file
    exists yet at that path (a camera whose ``config_dir`` entry was
    never populated) -- the same blank-template fallback as no
    injection at all."""
    from aprilcam.config.loader import ConfigLoader as JsonConfigLoader

    backend = FakeBackend()
    path = tmp_path / "cameras" / "cam-a" / "config.json"
    cam = Camera("cam-a", backend, config_loader=JsonConfigLoader(), config_path=path)
    cam.open()
    backend.set_property("gain", 42.0)

    cam.reset()

    assert backend.get_property("gain") == 42.0  # nothing pushed -- unchanged
    assert cam.stored_config().settings["gain"] is None


def test_stored_config_reports_the_blank_template_when_nothing_is_installed():
    cam = Camera("cam-a", FakeBackend(size=(640, 480)), device_name="Cam A")
    cam.open()

    config = cam.stored_config()

    assert config.name == "cam-a"
    assert config.settings["frame_width"] == 640
    assert config.settings["gain"] is None


def test_reset_reads_and_reapplies_the_real_on_disk_document(tmp_path):
    """The round trip that matters (ticket 023-004's Definition of Done
    #2): a document installed on disk is read fresh via a real
    ``ConfigLoader`` and re-applied through ``apply_config``'s normal
    ordering/hard-error path, discarding a live experimental override
    pushed earlier -- verified by backend *readback*, not a setter's
    return value."""
    import json

    from aprilcam.config.loader import ConfigLoader as JsonConfigLoader

    path = tmp_path / "cameras" / "cam-a" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "name": "cam-a",
                "playfield": "main-playfield",
                "settings": {"auto_exposure": 1, "exposure": 50, "gain": 3},
            }
        )
    )

    backend = FakeBackend()
    cam = Camera("cam-a", backend, config_loader=JsonConfigLoader(), config_path=path)
    cam.open()
    # A live experimental override, different from what's on disk.
    cam.apply_config(
        CameraConfig(name="cam-a", playfield=None, settings={"exposure": 999, "gain": 99})
    )
    assert backend.get_property("gain") == 99.0

    cam.reset()

    assert backend.get_property("gain") == 3.0
    assert backend.get_property("exposure") == 50.0
    assert backend.get_property("auto_exposure") == 1.0


def test_stored_config_matches_what_reset_just_applied(tmp_path):
    import json

    from aprilcam.config.loader import ConfigLoader as JsonConfigLoader

    path = tmp_path / "cameras" / "cam-a" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"name": "cam-a", "playfield": "main-playfield", "settings": {"gain": 3}})
    )
    cam = Camera("cam-a", FakeBackend(), config_loader=JsonConfigLoader(), config_path=path)
    cam.open()

    cam.reset()
    stored = cam.stored_config()

    assert stored.playfield == "main-playfield"
    assert stored.settings["gain"] == 3


def test_reset_reads_the_file_fresh_every_call_not_cached(tmp_path):
    """A file edited between two resets is picked up on the second
    one -- ``reset()``/``stored_config()`` never cache a prior read."""
    import json

    from aprilcam.config.loader import ConfigLoader as JsonConfigLoader

    path = tmp_path / "cameras" / "cam-a" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"name": "cam-a", "playfield": None, "settings": {"gain": 1}}))
    backend = FakeBackend()
    cam = Camera("cam-a", backend, config_loader=JsonConfigLoader(), config_path=path)
    cam.open()

    cam.reset()
    assert backend.get_property("gain") == 1.0

    path.write_text(json.dumps({"name": "cam-a", "playfield": None, "settings": {"gain": 2}}))
    cam.reset()
    assert backend.get_property("gain") == 2.0


# ---------------------------------------------------------------------------
# control_backend (ticket 023-001) -- real-hardware finding: cap.set()
# does not work on camera 3, an optional ControlBackend replaces the
# primary backend's settings surface exclusively when given.
# ---------------------------------------------------------------------------


class FakeControlBackend(ControlBackend):
    """A ``ControlBackend`` test double -- mirrors ``FakeBackend``'s
    settings surface but derives from ``ControlBackend`` (not
    ``CaptureBackend``), matching ``UvcUtilBackend``'s real shape:
    control-only, no capture methods at all."""

    def __init__(self, *, names=("exposure", "gain")) -> None:
        self._properties: Dict[str, float] = {}
        self._names = set(names)
        self.set_calls: List[Tuple[str, float]] = []

    def get_property(self, name: str) -> Optional[float]:
        return self._properties.get(name)

    def set_property(self, name: str, value: float) -> bool:
        self.set_calls.append((name, value))
        self._properties[name] = value
        return True

    def settable_names(self):
        return set(self._names)


def test_apply_config_routes_settings_through_control_backend_when_given():
    """When a ``control_backend`` is wired, ``apply_config`` must use
    *it* -- never the primary capture backend -- for get/set/settable,
    the exact routing ticket 023-001's real-hardware finding needs
    (``cv2.VideoCapture.set()`` does not work on camera 3)."""
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain"))
    cam = Camera("cam-a", primary, control_backend=control)
    cam.open()

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 42}))

    assert control.set_calls == [("exposure", 42.0)]
    # The primary backend's own settings surface was never touched.
    assert primary.get_property("exposure") is None


def test_apply_config_control_backend_settable_names_are_exclusive():
    """A key the primary ``FakeBackend`` would happily accept (it
    mirrors ``OpenCVBackend``'s full ``property_map()``) is still
    rejected once a narrower ``control_backend`` is wired -- the
    control backend's ``settable_names()`` fully replaces the primary
    backend's, it does not extend it. ``brightness`` is in
    ``property_map()`` (so the bare-``FakeBackend`` case would accept
    it) but not in this control backend's narrower set."""
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain"))
    cam = Camera("cam-a", primary, control_backend=control)
    cam.open()

    with pytest.raises(UnsupportedSetting) as exc_info:
        cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"brightness": 5}))
    assert exc_info.value.key == "brightness"
    assert control.set_calls == []


def test_apply_config_without_control_backend_still_uses_primary_backend():
    """No regression: a ``Camera`` built with no ``control_backend``
    (every non-macOS platform, every pre-existing test) behaves exactly
    as before this ticket."""
    primary = FakeBackend()
    cam = Camera("cam-a", primary)
    cam.open()

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"gain": 7}))

    assert primary.get_property("gain") == 7.0


def test_apply_config_control_backend_rollback_on_mid_push_failure():
    """The whole-push rollback contract (already proven against the
    primary backend in
    ``test_apply_config_rolls_back_on_mid_push_failure``) holds the same
    way through a ``control_backend``."""
    control = FakeControlBackend(names=("gain", "exposure"))
    control.set_property("gain", 5.0)  # pre-existing value to roll back to

    def failing_set(name, value):
        if name == "exposure":
            return False
        control._properties[name] = value
        control.set_calls.append((name, value))
        return True

    control.set_property = failing_set  # type: ignore[method-assign]
    cam = Camera("cam-a", FakeBackend(), control_backend=control)
    cam.open()

    with pytest.raises(UnsupportedSetting):
        cam.apply_config(
            CameraConfig(name="cam-a", playfield=None, settings={"gain": 9.0, "exposure": 100})
        )
    assert control.get_property("gain") == 5.0  # rolled back


# ---------------------------------------------------------------------------
# Settle-after-open (ticket 030-005, reopened) -- a real-hardware defect:
# pushing UVC controls immediately after a fresh open raced macOS
# AVFoundation's own capture-session initialisation and failed
# intermittently (measured up to ~60% of genuinely cold-process attempts).
# apply_config() now drains _OPEN_SETTLE_READS frames from the primary
# backend before the first real push following a fresh open, whenever a
# control_backend is wired -- mirroring v1's own proven mechanism
# (archive/src/aprilcam/daemon/camera_pipeline.py's frame drain before
# applying UVC controls).
# ---------------------------------------------------------------------------


def test_apply_config_settles_before_the_first_real_push_after_open():
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain"))
    cam = Camera("cam-a", primary, control_backend=control)
    cam.open()
    assert primary.read_calls == 0  # nothing drained yet -- no push has happened

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 6}))

    assert primary.read_calls == _OPEN_SETTLE_READS
    assert control.set_calls == [("exposure", 6.0)]  # the real push still happened


def test_apply_config_settles_only_once_per_open():
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain"))
    cam = Camera("cam-a", primary, control_backend=control)
    cam.open()

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 6}))
    assert primary.read_calls == _OPEN_SETTLE_READS

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"gain": 2}))
    assert primary.read_calls == _OPEN_SETTLE_READS  # unchanged -- no second drain


def test_apply_config_does_not_settle_without_a_control_backend():
    """No regression: a ``Camera`` built with no ``control_backend``
    (every non-macOS platform) never pays this cost -- there is no
    macOS-AVFoundation-session quirk to settle for on that path."""
    primary = FakeBackend()
    cam = Camera("cam-a", primary)
    cam.open()

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"gain": 7}))

    assert primary.read_calls == 0


def test_apply_config_does_not_settle_for_an_empty_or_blank_push():
    """An apply-on-open call with nothing real to write (``DaemonService.
    _camera_for``'s own pattern when no config is stored yet) never
    drains -- the drain is reserved for a push that actually touches
    hardware, so it never disturbs a headless test's frame-queue
    accounting for a push that was never going to write anything."""
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain"))
    cam = Camera("cam-a", primary, control_backend=control)
    cam.open()

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={}))
    assert primary.read_calls == 0

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"gain": None}))
    assert primary.read_calls == 0  # blank value -- still nothing real to push

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"gain": 5}))
    assert primary.read_calls == _OPEN_SETTLE_READS  # the first *real* push settles


def test_apply_config_settles_again_after_a_fresh_reopen():
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain"))
    cam = Camera("cam-a", primary, control_backend=control)
    cam.open()
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 6}))
    assert primary.read_calls == _OPEN_SETTLE_READS

    cam.close()
    cam.open()  # a fresh capture session -- the quirk this settle exists for again applies
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 6}))

    assert primary.read_calls == 2 * _OPEN_SETTLE_READS


# ---------------------------------------------------------------------------
# Resolution key (ticket 023-002) -- frame_width/frame_height always route
# through the primary capture backend, never control_backend.
# ---------------------------------------------------------------------------


def test_apply_config_resolution_routes_through_primary_backend_even_with_control_backend():
    """AC2: ``frame_width``/``frame_height`` must go through the primary
    capture backend's own ``get_property``/``set_property`` (in
    production, ``OpenCVBackend``'s ``CAP_PROP_FRAME_WIDTH``/
    ``CAP_PROP_FRAME_HEIGHT`` path) even when a narrower
    ``control_backend`` is wired that does not know these keys at all --
    ``UvcUtilBackend``'s real ``_CONTROLS`` table has no resolution
    entries. This is the special-case the ticket-001 module docstring
    flagged as a future ticket's job, now built."""
    primary = FakeBackend()
    control = FakeControlBackend(names=("exposure", "gain"))  # no frame_width/frame_height
    cam = Camera("cam-a", primary, control_backend=control)
    cam.open()

    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"frame_width": 1280}))

    assert primary.get_property("frame_width") == 1280.0
    assert control.set_calls == []  # the control backend was never touched


def test_apply_config_resolution_rejected_without_control_backend_support_from_primary():
    """A camera whose primary backend does not advertise resolution
    support at all (e.g. a hypothetical minimal capture backend) still
    hard-errors on a ``frame_width`` push rather than silently routing
    it through ``control_backend`` -- resolution is never a
    ``control_backend`` concern, so there is no fallback to it."""

    class NoResolutionBackend:
        def __init__(self) -> None:
            self._is_open = True

        @property
        def is_open(self) -> bool:
            return self._is_open

        def open(self) -> None:
            self._is_open = True

        def read(self):
            return None

        def close(self) -> None:
            self._is_open = False

        def frame_size(self):
            return None

    control = FakeControlBackend(names=("exposure", "gain", "frame_width"))
    cam = Camera("cam-a", NoResolutionBackend(), control_backend=control)
    cam.open()

    # frame_width is not in _settable_names() at all: the primary
    # backend advertises no settable_names(), so the resolution-key
    # union contributes nothing, regardless of what control_backend
    # (wrongly, for this test) claims to support.
    with pytest.raises(UnsupportedSetting) as exc_info:
        cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"frame_width": 1280}))
    assert exc_info.value.key == "frame_width"
    assert control.set_calls == []


# ---------------------------------------------------------------------------
# @pytest.mark.camera — real hardware (AC1, AC3)
# ---------------------------------------------------------------------------


def _real_camera(name_substring: str) -> Optional[Tuple[int, str]]:
    """``(index, exact device name)`` for the first enumerated camera
    whose name contains *name_substring* (case-insensitive), or ``None``
    if not attached. The exact name is what ``UvcUtilBackend`` needs for
    its ``-N`` target -- the OS-reported descriptor, not the search
    substring (which happens to match for the two fixture cameras below
    but is not guaranteed to in general)."""
    try:
        import cv2 as cv
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError:
        return None
    avf_offset = getattr(cv, "CAP_AVFOUNDATION", 1200)
    for cam in enumerate_cameras():
        name = getattr(cam, "name", "") or ""
        if name_substring.lower() in name.lower():
            raw = cam.index
            index = raw - avf_offset if raw >= avf_offset else raw
            return index, name
    return None


def _real_camera_index(name_substring: str) -> Optional[int]:
    """OpenCV index for the first enumerated camera whose name contains
    *name_substring* (case-insensitive), or ``None`` if not attached."""
    found = _real_camera(name_substring)
    return found[0] if found is not None else None


@pytest.mark.camera
@pytest.mark.parametrize(
    "name_substring,expected_slug",
    [
        ("Arducam OV9782", "arducam-ov9782-usb-camera"),
        ("HD USB", "hd-usb-camera"),
    ],
)
def test_real_camera_open_read_close(name_substring, expected_slug):
    from aprilcam.camera.backends.opencv_backend import OpenCVBackend

    index = _real_camera_index(name_substring)
    if index is None:
        pytest.skip(f"no attached camera matching {name_substring!r}")

    cam = Camera(expected_slug, OpenCVBackend(index), device_name=name_substring)
    # Enumeration (above) is not availability: this process may enumerate
    # the device fine and still be unable to open it (e.g. an agent
    # session with no macOS camera TCC grant -- docs/knowledge/daemon-
    # camera-grant-and-detection-priming.md). Gate on the real open
    # itself, individually, so a genuinely broken Camera.open() (a real
    # RuntimeError with a different message) still fails loudly.
    try:
        cam.open()
    except RuntimeError as exc:
        if "failed to open camera" not in str(exc):
            raise
        pytest.skip(
            f"{expected_slug!r} ({name_substring!r}) is attached but could not "
            f"be opened just now -- {exc}. Expected when this process has no "
            f"camera TCC grant (e.g. an agent session) or the device is "
            f"genuinely busy elsewhere; re-run from an operator Terminal "
            f"session to exercise this test for real."
        )
    try:
        assert cam.state.present is True
        assert cam.state.usable is True

        frame = None
        for _ in range(10):  # a real device may need a few reads to warm up
            frame = cam.read()
            if frame is not None:
                break
        assert frame is not None
        assert isinstance(frame, np.ndarray)
        assert frame.ndim == 3

        cfg = cam.effective_config()
        assert cfg.settings["device_name"] == name_substring
        assert cfg.settings["frame_width"] > 0
        assert cfg.settings["frame_height"] > 0
    finally:
        cam.close()
    assert cam.state.usable is False


# ---------------------------------------------------------------------------
# @pytest.mark.camera — real hardware, with the real control_backend a
# macOS daemon actually wires (ticket 023-002, AC1/AC2/AC3/AC4).
# ---------------------------------------------------------------------------

#: The settable-key set both real macOS cameras are expected to report:
#: UvcUtilBackend's fixed, per-control-not-per-model ``_CONTROLS`` table
#: (the five UVC controls ticket 023-001 validated live) plus
#: frame_width/frame_height (routed through OpenCVBackend regardless,
#: ticket 023-002). Camera 3 and hd-usb-camera are expected to report
#: the *same* set -- AC4's "same set with a documented reason" branch --
#: because uvc_util.py's control table is fixed per-key, not probed per
#: camera model; that fixed table is the documented reason.
_EXPECTED_REAL_SETTABLE_KEYS = frozenset(
    {
        "auto_exposure",
        "exposure",
        "gain",
        "auto_white_balance",
        "white_balance_temperature",
        "frame_width",
        "frame_height",
    }
)


@pytest.mark.camera
@pytest.mark.parametrize(
    "name_substring,expected_slug",
    [
        ("Arducam OV9782", "arducam-ov9782-usb-camera"),
        ("HD USB", "hd-usb-camera"),
    ],
)
def test_real_camera_effective_config_reports_full_settable_key_set_with_control_backend(
    name_substring, expected_slug
):
    """UC-03.S-1, verified live, with the real ``control_backend`` a
    macOS daemon actually wires (ticket 023-001's
    ``_default_control_backend_factory``): the template's settable-key
    set is the union of ``UvcUtilBackend``'s five real UVC controls and
    ``frame_width``/``frame_height`` (AC2 -- routed through the primary
    ``OpenCVBackend`` regardless of ``control_backend`` being wired),
    every one of the five UVC keys blank (AC1), and camera 3 /
    ``hd-usb-camera`` reporting the identical set (AC4).

    **Deliberately does not open the capture backend.** Camera 3 is the
    real, currently-calibrated ``main-playfield`` camera and may already
    be held open by a live production daemon (an AVFoundation capture
    session is exclusive per process -- ``tests/aprilcam/daemon/
    test_service.py``'s real-hardware section documents this
    precondition explicitly). This test's whole point -- the real
    settable-key set and its blankness -- needs only construction
    (``UvcUtilBackend.__init__`` still proves ``uvc-util`` is on
    ``PATH`` and resolves against the real, enumerated device name) and
    ``effective_config()``, neither of which requires the camera to be
    open: ``_settable_names()`` reads static key tables, and
    ``effective_config()`` never calls ``get_property``/``set_property``
    for non-resolution keys at all (see that method's own docstring) --
    so this test cannot disturb camera 3's live hardware settings or
    contend with whoever already holds it open, and needs no baseline
    restore. Real, positive dimensions when open are already covered,
    unchanged, by ``test_real_camera_open_read_close`` above; with the
    backend left closed here, ``frame_width``/``frame_height`` are
    ``None`` -- still correct per ``effective_config``'s "never guessed"
    contract, and itself evidence against fabrication, not for it.
    """
    from aprilcam.camera.backends.opencv_backend import OpenCVBackend
    from aprilcam.camera.backends.uvc_util import UvcUtilBackend

    if sys.platform != "darwin":
        pytest.skip("UvcUtilBackend (the real control_backend) is macOS-only")

    found = _real_camera(name_substring)
    if found is None:
        pytest.skip(f"no attached camera matching {name_substring!r}")
    index, device_name = found

    try:
        control = UvcUtilBackend(device_name)
    except RuntimeError:
        pytest.skip("uvc-util not found on PATH")

    cam = Camera(expected_slug, OpenCVBackend(index), control_backend=control, device_name=device_name)

    cfg = cam.effective_config()

    for key in _EXPECTED_REAL_SETTABLE_KEYS:
        assert key in cfg.settings, f"{key} missing from the live template"
    for key in _EXPECTED_REAL_SETTABLE_KEYS - {"frame_width", "frame_height"}:
        assert cfg.settings[key] is None, f"{key} should be blank in the template"

    assert cfg.settings["device_name"] == device_name


# ---------------------------------------------------------------------------
# Flat-field gain map (FLATFIELD_FILENAME) -- out-of-process change, 2026-09-02
# ---------------------------------------------------------------------------


def _flat_frame(value: int, size=(4, 6), channels: Optional[int] = 3) -> np.ndarray:
    shape = (size[0], size[1]) if channels is None else (size[0], size[1], channels)
    return np.full(shape, value, dtype=np.uint8)


def _write_gain(path, gain: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, gain)


def test_read_applies_flatfield_gain_map_and_saturates(tmp_path):
    from aprilcam.camera.camera import FLATFIELD_FILENAME

    path = tmp_path / FLATFIELD_FILENAME
    gain = np.full((4, 6, 3), 2.0, dtype=np.float32)
    gain[:, :, 1] = 3.0  # green channel: 100 * 3 = 300 -> must clip to 255
    gain[0, 0, :] = 0.5  # one pixel dimmed
    _write_gain(path, gain)
    cam = Camera("cam-a", FakeBackend(read_queue=[_flat_frame(100)]), flatfield_path=path)
    cam.open()
    out = cam.read()
    assert out is not None and out.dtype == np.uint8 and out.shape == (4, 6, 3)
    assert out[1, 1].tolist() == [200, 255, 200]
    assert out[0, 0].tolist() == [50, 50, 50]


def test_read_without_flatfield_file_passes_frames_through(tmp_path):
    from aprilcam.camera.camera import FLATFIELD_FILENAME

    frame = _flat_frame(100)
    cam = Camera("cam-a", FakeBackend(read_queue=[frame]), flatfield_path=tmp_path / FLATFIELD_FILENAME)
    cam.open()
    out = cam.read()
    assert out is not None and np.array_equal(out, frame)


def test_read_with_no_flatfield_path_is_unchanged():
    frame = _flat_frame(100)
    cam = Camera("cam-a", FakeBackend(read_queue=[frame]))
    cam.open()
    out = cam.read()
    assert out is not None and np.array_equal(out, frame)


def test_flatfield_shape_mismatch_is_skipped_with_one_warning(tmp_path, caplog):
    import logging

    from aprilcam.camera.camera import FLATFIELD_FILENAME

    path = tmp_path / FLATFIELD_FILENAME
    _write_gain(path, np.full((8, 8, 3), 2.0, dtype=np.float32))
    frame = _flat_frame(100)
    cam = Camera("cam-a", FakeBackend(read_queue=[frame, frame]), flatfield_path=path)
    cam.open()
    with caplog.at_level(logging.WARNING, logger="aprilcam.camera.camera"):
        assert np.array_equal(cam.read(), frame)
        assert np.array_equal(cam.read(), frame)
    assert sum("does not match frame shape" in r.message for r in caplog.records) == 1


def test_flatfield_three_channel_map_applies_to_mono_frame(tmp_path):
    from aprilcam.camera.camera import FLATFIELD_FILENAME

    path = tmp_path / FLATFIELD_FILENAME
    gain = np.full((4, 6, 3), 1.0, dtype=np.float32)
    gain[:, :, 0] = 2.0  # first channel is what a mono frame uses
    _write_gain(path, gain)
    cam = Camera("cam-a", FakeBackend(read_queue=[_flat_frame(60, channels=None)]), flatfield_path=path)
    cam.open()
    out = cam.read()
    assert out is not None and out.shape == (4, 6) and int(out[2, 2]) == 120


def test_flatfield_two_d_map_broadcasts_over_colour_frame(tmp_path):
    from aprilcam.camera.camera import FLATFIELD_FILENAME

    path = tmp_path / FLATFIELD_FILENAME
    _write_gain(path, np.full((4, 6), 1.5, dtype=np.float32))
    cam = Camera("cam-a", FakeBackend(read_queue=[_flat_frame(100)]), flatfield_path=path)
    cam.open()
    out = cam.read()
    assert out is not None and out[3, 5].tolist() == [150, 150, 150]


def test_malformed_flatfield_file_is_ignored_not_fatal(tmp_path, caplog):
    import logging

    from aprilcam.camera.camera import FLATFIELD_FILENAME

    path = tmp_path / FLATFIELD_FILENAME
    _write_gain(path, np.full((4, 6, 3), 2, dtype=np.uint8))  # integer, not float -> rejected
    frame = _flat_frame(100)
    cam = Camera("cam-a", FakeBackend(read_queue=[frame]), flatfield_path=path)
    with caplog.at_level(logging.WARNING, logger="aprilcam.camera.camera"):
        cam.open()
    assert any("malformed flat-field gain map" in r.message for r in caplog.records)
    assert np.array_equal(cam.read(), frame)

    path.write_bytes(b"not a numpy file")
    cam2 = Camera("cam-b", FakeBackend(read_queue=[frame]), flatfield_path=path)
    cam2.open()  # must not raise
    assert np.array_equal(cam2.read(), frame)


def test_reset_reloads_flatfield_from_disk(tmp_path):
    """Deleting the file + reset() clears the correction; adding it +
    reset() enables it -- the live install/clear path with no restart."""
    from aprilcam.camera.camera import FLATFIELD_FILENAME

    path = tmp_path / FLATFIELD_FILENAME
    _write_gain(path, np.full((4, 6, 3), 2.0, dtype=np.float32))
    frame = _flat_frame(100)
    cam = Camera("cam-a", FakeBackend(read_queue=[frame] * 3), flatfield_path=path)
    cam.open()
    assert int(cam.read()[0, 0, 0]) == 200

    path.unlink()
    cam.reset()
    assert int(cam.read()[0, 0, 0]) == 100

    _write_gain(path, np.full((4, 6, 3), 0.5, dtype=np.float32))
    cam.reset()
    assert int(cam.read()[0, 0, 0]) == 50


def test_read_raw_bypasses_loaded_flatfield_map(tmp_path):
    """OOP fix 2026-09-03: reference captures must see raw sensor frames
    even while a gain map is loaded and applied to ordinary reads."""
    from aprilcam.camera.camera import FLATFIELD_FILENAME

    path = tmp_path / FLATFIELD_FILENAME
    _write_gain(path, np.full((4, 6, 3), 2.0, dtype=np.float32))
    frame = _flat_frame(100)
    cam = Camera("cam-a", FakeBackend(read_queue=[frame, frame, None] * 2), flatfield_path=path)
    cam.open()
    assert int(cam.read()[0, 0, 0]) == 200
    assert np.array_equal(cam.read_raw(), frame)
    assert cam.read_raw() is None  # failure accounting is shared with read()
    assert cam._consecutive_failures == 1


# ---------------------------------------------------------------------------
# UVC control drift: read back + re-assert (out-of-process fix, 2026-09-03)
# ---------------------------------------------------------------------------


def test_reassert_config_restores_drifted_hardware_controls():
    backend = FakeBackend(read_queue=[_flat_frame(100)] * 4)
    cam = Camera("cam-a", backend)
    cam.open()
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 60, "gain": 8}))
    assert cam.applied_settings == {"exposure": 60.0, "gain": 8.0}
    assert cam.reassert_config() == ()
    # the camera silently reverts to its power-on defaults
    backend._properties["exposure"] = 2500.0
    backend._properties["gain"] = 100.0
    drifted = cam.reassert_config()
    assert set(drifted) == {"exposure", "gain"}
    assert backend._properties["exposure"] == 60.0 and backend._properties["gain"] == 8.0
    assert cam.reassert_config() == ()


def test_reassert_config_ignores_unreadable_keys_and_small_jitter():
    backend = FakeBackend(read_queue=[_flat_frame(100)] * 4)
    cam = Camera("cam-a", backend)
    cam.open()
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 60, "gain": 8}))
    backend._properties["exposure"] = 60.3  # within tolerance
    del backend._properties["gain"]  # reads back None
    assert cam.reassert_config() == ()


def test_reassert_config_is_a_noop_before_any_push_or_when_closed():
    backend = FakeBackend(read_queue=[_flat_frame(100)] * 2)
    cam = Camera("cam-a", backend)
    cam.open()
    assert cam.reassert_config() == ()
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 60}))
    cam.close()
    backend._properties["exposure"] = 2500.0
    assert cam.reassert_config() == ()


def test_reset_updates_what_reassert_considers_applied(tmp_path):
    backend = FakeBackend(read_queue=[_flat_frame(100)] * 4)
    cam = Camera("cam-a", backend)
    cam.open()
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 60}))
    cam.reset()  # nothing on disk -> empty push; the last *successful* push is now the empty one
    assert cam.applied_settings == {}
    backend._properties["exposure"] = 2500.0
    assert cam.reassert_config() == ()


# ---------------------------------------------------------------------------
# Lifecycle states, drop/reopen, full-config re-apply (sprint 047 ticket 001)
# ---------------------------------------------------------------------------


class _OrderBackend(FakeBackend):
    """Records set_property order and forgets properties on every open,
    like a real device coming back at its driver defaults."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.set_log: List[str] = []

    def open(self) -> None:
        super().open()
        self._properties = {"frame_width": 640.0, "frame_height": 480.0}

    def set_property(self, name: str, value: float) -> bool:
        self.set_log.append(name)
        return super().set_property(name, value)


def _configured_cam():
    backend = _OrderBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cam.apply_config(
        CameraConfig(
            name="cam-a",
            playfield=None,
            settings={"exposure": 12.0, "auto_exposure": 1.0, "frame_height": 800.0, "frame_width": 1280.0},
        )
    )
    return backend, cam


def test_lifecycle_transitions_absent_and_closed():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    assert cam.lifecycle is CameraLifecycle.CLOSED
    cam.open()
    assert cam.lifecycle is CameraLifecycle.PRESENT
    cam.drop()
    assert cam.lifecycle is CameraLifecycle.ABSENT
    assert backend.is_open is False and cam.state.present is False
    assert cam.reopen() is True
    assert cam.lifecycle is CameraLifecycle.PRESENT and backend.is_open
    cam.close()
    assert cam.lifecycle is CameraLifecycle.CLOSED
    assert cam.reopen() is True
    assert cam.lifecycle is CameraLifecycle.PRESENT


def test_reopen_not_enumerated_stays_absent():
    backend = FakeBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cam.drop()
    backend.fail_open = True
    backend.fail_open_exception = CameraNotEnumeratedError("gone")
    assert cam.reopen() is False
    assert cam.lifecycle is CameraLifecycle.ABSENT


def test_reopen_reapplies_full_config_size_first():
    backend, cam = _configured_cam()
    assert backend.set_log[:4] == ["frame_height", "frame_width", "auto_exposure", "exposure"]
    cam.drop()
    backend.set_log.clear()
    assert cam.reopen() is True
    assert backend.set_log == ["frame_height", "frame_width", "auto_exposure", "exposure"]
    assert backend.get_property("frame_width") == 1280.0
    assert backend.get_property("frame_height") == 800.0
    assert backend.get_property("exposure") == 12.0


def test_self_heal_reapplies_stored_config():
    backend, cam = _configured_cam()
    backend.set_log.clear()
    for _ in range(FAILURE_THRESHOLD):
        cam.read()
    assert backend.set_log == ["frame_height", "frame_width", "auto_exposure", "exposure"]
    assert backend.get_property("frame_width") == 1280.0


def test_reopen_with_nothing_stored_pushes_nothing():
    backend = _OrderBackend()
    cam = Camera("cam-a", backend)
    cam.open()
    cam.drop()
    assert cam.reopen() is True
    assert backend.set_log == []


def test_reassert_and_check_stall_skip_non_present(caplog):
    backend = _OrderBackend()
    backend.heartbeat_age = lambda: 999.0  # type: ignore[attr-defined]
    cam = Camera("cam-a", backend)
    cam.open()
    cam.apply_config(CameraConfig(name="cam-a", playfield=None, settings={"exposure": 12.0}))
    cam.drop()
    backend._properties["exposure"] = -1.0  # what a dead device reads back
    backend.set_log.clear()
    opens = backend.open_calls
    assert cam.reassert_config() == ()
    assert cam.check_stall() is False
    assert backend.set_log == [] and backend.open_calls == opens
    assert "drifted" not in caplog.text
