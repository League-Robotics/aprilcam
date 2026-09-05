"""Tests for ``WorkerBackend`` and ``capture_worker.py`` (sprint 038
ticket 004) -- the process boundary that fixes the AVFoundation
capture-session wedge (``camera-DESIGN.md`` §3).

Every test here spawns ``python -m aprilcam.camera.capture_worker`` as a
**real subprocess** (``capture_worker.main()`` runs for real, not just
in-process), against the ``--synthetic WxH@fps`` fake frame source --
never a real camera, per sprint.md's Test Strategy. This exercises the
real process boundary, the shared-memory ring hand-off, and
control-channel proxying end to end with no camera hardware and no
``cv2`` dependency for the synthetic path.

One ``@pytest.mark.camera`` test (at the bottom) additionally exercises
``WorkerBackend`` against a real camera, mirroring ``test_camera.py``'s
own real-hardware convention -- it skips cleanly whenever no matching
camera is enumerated, which is always true in an agent session (agents
never open real cameras, per project rules).
"""

from __future__ import annotations

import os
import signal
import time
from multiprocessing.shared_memory import SharedMemory
from typing import Optional, Tuple

import numpy as np
import pytest

from aprilcam.camera.backends import CaptureBackend
from aprilcam.camera.backends.worker_backend import (
    WorkerBackend,
    WorkerStartupError,
)

#: Kept short so tests run fast: every synthetic worker in this file
#: publishes at a high enough rate that "wait for the next frame"
#: resolves in well under a second.
_FAST_SYNTHETIC = "160x120@60"


def _read_until(backend: CaptureBackend, *, timeout: float = 3.0) -> Optional[np.ndarray]:
    """Poll ``backend.read()`` until it returns a frame or *timeout*
    elapses -- ``WorkerBackend.read()`` already bounds a single call to
    its own (shorter) read_timeout, but the very first frame can take a
    little longer while the worker process is still starting up its
    read loop after the ready handshake."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = backend.read()
        if frame is not None:
            return frame
    return None


# ---------------------------------------------------------------------------
# basic lifecycle: spawn, publish, read, close
# ---------------------------------------------------------------------------


def test_workerbackend_is_a_real_capturebackend_subclass():
    backend = WorkerBackend(synthetic=_FAST_SYNTHETIC)
    assert isinstance(backend, CaptureBackend)


def test_requires_index_or_synthetic():
    with pytest.raises(ValueError):
        WorkerBackend()


def test_is_open_false_before_open_and_frame_size_none():
    backend = WorkerBackend(synthetic=_FAST_SYNTHETIC)
    assert backend.is_open is False
    assert backend.frame_size() is None
    assert backend.read() is None


def test_heartbeat_age_is_none_before_open():
    """Camera.check_stall's no-op contract (sprint 038 ticket 005): no
    ring attached yet -- nothing to report."""
    backend = WorkerBackend(synthetic=_FAST_SYNTHETIC)
    assert backend.heartbeat_age() is None


def test_heartbeat_age_reports_a_small_age_while_the_worker_is_publishing():
    """A live synthetic worker keeps publishing -- heartbeat_age() should
    stay small (well under the read timeout) and never negative."""
    backend = WorkerBackend(synthetic="160x120@60", read_timeout=0.5)
    try:
        backend.open()
        assert _read_until(backend) is not None  # worker is genuinely publishing

        age = backend.heartbeat_age()

        assert age is not None
        assert 0.0 <= age < 2.0
    finally:
        backend.close()


def test_heartbeat_age_none_again_after_close():
    backend = WorkerBackend(synthetic=_FAST_SYNTHETIC, read_timeout=0.5)
    backend.open()
    assert _read_until(backend) is not None
    backend.close()

    assert backend.heartbeat_age() is None


def test_heartbeat_age_grows_stale_after_the_worker_is_killed():
    """Kill the child out-of-band (mirrors
    test_read_returns_none_after_out_of_band_kill_never_raises below) --
    the ring's last-published timestamp stops advancing, so heartbeat_age
    keeps growing rather than resetting or erroring, exactly the signal
    Camera.check_stall needs to notice a wedged/dead worker with nobody
    reading."""
    backend = WorkerBackend(synthetic=_FAST_SYNTHETIC, read_timeout=0.5)
    backend.open()
    assert _read_until(backend) is not None
    pid = backend.pid
    assert pid is not None

    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 3.0
    while backend.is_open and time.monotonic() < deadline:
        time.sleep(0.02)
    assert backend.is_open is False

    first = backend.heartbeat_age()
    time.sleep(0.3)
    second = backend.heartbeat_age()

    assert first is not None
    assert second is not None
    assert second > first  # still frozen at the last real publish -- age keeps climbing
    backend.close()


def test_open_spawns_worker_and_publishes_real_frames():
    backend = WorkerBackend(synthetic="160x120@60", read_timeout=0.5)
    try:
        backend.open()
        assert backend.is_open is True
        assert backend.pid is not None

        frame = _read_until(backend)
        assert frame is not None
        assert frame.shape == (120, 160, 3)
        assert frame.dtype == np.uint8

        # Frames keep changing -- the synthetic source encodes an
        # incrementing counter into every pixel.
        second = _read_until(backend)
        assert second is not None
    finally:
        backend.close()
    assert backend.is_open is False


def test_close_terminates_process_and_unlinks_ring_no_leak():
    backend = WorkerBackend(synthetic="64x48@60", read_timeout=0.5, close_grace=2.0)
    backend.open()
    assert _read_until(backend) is not None
    pid = backend.pid
    ring_name = backend.ring_name
    assert pid is not None
    assert ring_name is not None

    backend.close()

    assert backend.is_open is False
    # Verify via real process exit, not just "we sent a signal".
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # Verify no leaked shared-memory segment.
    with pytest.raises(FileNotFoundError):
        SharedMemory(name=ring_name, create=False)


def test_close_is_idempotent_and_safe_when_never_opened():
    backend = WorkerBackend(synthetic=_FAST_SYNTHETIC)
    backend.close()  # never opened -- must be a safe no-op
    assert backend.is_open is False

    backend.open()
    assert _read_until(backend) is not None
    backend.close()
    backend.close()  # closing twice must also be safe
    assert backend.is_open is False


def test_read_returns_none_when_no_new_frame_within_bound_but_worker_alive():
    """Distinct from the out-of-band-kill test below: this exercises
    "the child is alive but hasn't published a new frame within the
    bound" (AC3's "heartbeat has gone stale" branch) rather than "the
    child is dead"."""
    backend = WorkerBackend(synthetic="64x48@2", read_timeout=0.1)  # a new frame roughly every 0.5s
    backend.open()
    try:
        first = _read_until(backend, timeout=3.0)
        assert first is not None

        # Immediately asking again, well inside the ~0.5s inter-frame
        # gap, must report "nothing new yet" rather than blocking past
        # its own bound -- and the worker is still very much alive.
        assert backend.read() is None
        assert backend.is_open is True
    finally:
        backend.close()


def test_kill_and_respawn_via_close_then_open():
    """``Camera._attempt_self_heal`` only ever calls
    ``backend.close()`` then ``backend.open()`` -- kill-and-respawn
    must fall out of that with no special-casing (camera-DESIGN.md's
    "Reworking Camera._attempt_self_heal..." rejected alternative)."""
    backend = WorkerBackend(synthetic="64x48@60", read_timeout=0.5, close_grace=2.0)
    backend.open()
    first_pid = backend.pid
    assert _read_until(backend) is not None

    backend.close()
    backend.open()
    second_pid = backend.pid
    try:
        assert second_pid is not None
        assert second_pid != first_pid
        assert _read_until(backend) is not None
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# SUC-002: a crashed/killed worker is an ordinary read failure
# ---------------------------------------------------------------------------


def test_read_returns_none_after_out_of_band_kill_never_raises():
    backend = WorkerBackend(synthetic="64x48@60", read_timeout=0.3)
    backend.open()
    try:
        assert _read_until(backend) is not None
        pid = backend.pid
        assert pid is not None

        os.kill(pid, signal.SIGKILL)
        # Give the OS a moment to reap/mark the process dead.
        deadline = time.monotonic() + 3.0
        while backend.is_open and time.monotonic() < deadline:
            time.sleep(0.02)

        assert backend.is_open is False
        assert backend.read() is None  # never raises
    finally:
        backend.close()


def test_two_independent_workers_one_kill_does_not_affect_other():
    """SUC-002's multi-camera independence criterion."""
    worker_a = WorkerBackend(synthetic="64x48@60", read_timeout=0.3)
    worker_b = WorkerBackend(synthetic="64x48@60", read_timeout=0.3)
    worker_a.open()
    worker_b.open()
    try:
        assert _read_until(worker_a) is not None
        assert _read_until(worker_b) is not None

        pid_a = worker_a.pid
        assert pid_a is not None
        os.kill(pid_a, signal.SIGKILL)

        deadline = time.monotonic() + 3.0
        while worker_a.is_open and time.monotonic() < deadline:
            time.sleep(0.02)
        assert worker_a.is_open is False
        assert worker_a.read() is None

        # worker_b is completely unaffected: still alive, still
        # delivering frames, still answering control-channel requests.
        assert worker_b.is_open is True
        assert _read_until(worker_b) is not None
        assert worker_b.get_property("gain") == 1.0
    finally:
        worker_a.close()
        worker_b.close()


# ---------------------------------------------------------------------------
# control-channel proxying: get_property/set_property/settable_names/
# frame_size
# ---------------------------------------------------------------------------


def test_control_channel_round_trips():
    backend = WorkerBackend(synthetic=_FAST_SYNTHETIC, read_timeout=0.5)
    backend.open()
    try:
        assert _read_until(backend) is not None

        assert backend.get_property("brightness") == 50.0
        assert backend.get_property("not_a_real_property") is None

        assert backend.set_property("gain", 3.5) is True
        assert backend.get_property("gain") == 3.5
        assert backend.set_property("not_a_real_property", 1.0) is False

        assert backend.settable_names() == {"brightness", "gain"}

        size = backend.frame_size()
        assert size == (160, 120)

        # Not part of the control channel's wire protocol (see
        # WorkerBackend.describe_set_failure's own docstring) -- best
        # effort, never raises.
        assert backend.describe_set_failure("gain", 1.0) is None
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# open() failure paths
# ---------------------------------------------------------------------------


def test_open_raises_worker_startup_error_on_first_frame_timeout():
    # fps=1 means the synthetic source's first frame (and therefore the
    # ready handshake) is not available for ~1s -- comfortably past a
    # 0.2s ready_timeout, so this deterministically exercises the
    # "first frame not reported in time" path. The connect timeout is
    # left generous: the child connects long before its first frame.
    backend = WorkerBackend(synthetic="64x48@1", startup_timeout=10.0, ready_timeout=0.2)
    with pytest.raises(WorkerStartupError, match="did not report its first frame"):
        backend.open()
    assert backend.is_open is False


def test_open_raises_worker_startup_error_on_connect_timeout():
    # A connect timeout no interpreter can beat exercises the "never
    # connected" branch on its own, separately from the first-frame
    # bound above (the two are distinct timeouts with distinct meanings).
    backend = WorkerBackend(synthetic="64x48@30", startup_timeout=0.001)
    with pytest.raises(WorkerStartupError, match="did not connect"):
        backend.open()
    assert backend.is_open is False


def test_open_reports_a_backend_construction_failure_through_the_handshake():
    # A malformed --synthetic spec makes capture_worker.main() raise
    # while building the backend. Because the worker connects *before*
    # that, the failure arrives as a ready-error carrying the exception
    # text -- not as a bare "did not connect".
    backend = WorkerBackend(synthetic="not-a-valid-spec", startup_timeout=5.0)
    with pytest.raises(WorkerStartupError, match="ValueError"):
        backend.open()
    assert backend.is_open is False


def test_worker_is_spawned_without_forking_the_daemon_on_macos(monkeypatch):
    """On macOS the spawn must go through posix_spawn (close_fds=False
    is what selects it in CPython), never fork+exec of the multi-threaded,
    AVFoundation-loaded daemon -- see ``_popen_spawn_kwargs``. Elsewhere
    the default close_fds=True is kept."""
    from aprilcam.camera.backends import worker_backend as wb

    monkeypatch.setattr(wb.sys, "platform", "darwin")
    assert wb._popen_spawn_kwargs() == {"close_fds": False}
    monkeypatch.setattr(wb.sys, "platform", "linux")
    assert wb._popen_spawn_kwargs() == {}


def test_open_reports_exit_code_and_stderr_when_child_dies_before_connecting():
    # An argparse failure exits (code 2) with usage text on stderr before
    # the worker ever connects -- the one shape of failure the parent can
    # only diagnose from the child's exit code and captured stderr. The
    # message must carry both, and open() must return promptly rather
    # than sit out the whole connect timeout.
    backend = WorkerBackend(synthetic="64x48@30", startup_timeout=10.0)
    real_build = backend._build_command
    backend._build_command = lambda address: real_build(address) + ["--ring-slots", "x"]  # type: ignore[method-assign]
    started = time.monotonic()
    with pytest.raises(WorkerStartupError) as excinfo:
        backend.open()
    elapsed = time.monotonic() - started
    message = str(excinfo.value)
    assert "exited with code 2" in message
    assert "invalid int value" in message
    assert elapsed < 5.0, f"open() waited {elapsed:.1f}s instead of failing on the child's exit"
    assert backend.is_open is False


# ---------------------------------------------------------------------------
# @pytest.mark.camera -- real hardware, skips cleanly with none present
# ---------------------------------------------------------------------------


def _real_camera_index() -> Optional[int]:
    try:
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError:
        return None
    for cam in enumerate_cameras():
        return int(cam.index)
    return None


@pytest.mark.camera
def test_real_camera_open_read_close():
    index = _real_camera_index()
    if index is None:
        pytest.skip("no attached camera enumerated")

    backend = WorkerBackend(index, read_timeout=2.0)
    try:
        backend.open()
    except Exception as exc:  # noqa: BLE001 -- enumeration is not availability
        pytest.skip(f"real camera at index {index} could not be opened by a worker: {exc}")
    try:
        frame = _read_until(backend, timeout=5.0)
        assert frame is not None
        assert isinstance(frame, np.ndarray)
    finally:
        backend.close()
