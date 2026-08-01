"""Tests for ``aprilcam.client.lifecycle`` (ticket 020-005).

Two tiers, matching the module's own fakeable-dependency design (see
``client/lifecycle.py``'s docstring):

* **Headless, fake-driven** -- the polling/race/error-mapping logic in
  ``start``/``stop``/``status`` exercised via injected ``spawn``/
  ``lock_holder``/``socket_reachable``/``kill``/``discovery`` fakes, no
  real process ever launched. Fast, and safe: a fabricated holder pid
  is never actually signalled (see ``_NeverCalled`` below).
* **``@pytest.mark.integration``** -- a genuine
  ``subprocess.Popen([sys.executable, "-m", "aprilcam.daemon"], ...)``
  round trip (start/status/stop, plus a second instance's
  ``AlreadyRunning``). No camera hardware needed (zero cameras is a
  valid start, UC-01.S-2), but real process spawn/signal/IPC makes it
  slower and environment-sensitive, hence the lighter ``integration``
  marker (deselect with ``-m 'not integration'``) rather than
  ``camera``. Isolated from any real daemon on the machine by forcing
  ``socket_dir`` down to a short, private ``/tmp`` root via
  ``XDG_RUNTIME_DIR`` -- the same technique
  ``tests/aprilcam/client/test_discovery.py`` uses, needed here because
  the *subprocess* re-resolves ``Directories`` fresh from its inherited
  environment and cannot be handed a Python object directly.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import pytest

from aprilcam.client.discovery import Discovery
from aprilcam.client.lifecycle import DaemonLifecycle
from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import LOCK_FILENAME, SOCKET_FILENAME
from aprilcam.errors import AlreadyRunning, ConnectionFailed


# ---------------------------------------------------------------------------
# Headless fakes
# ---------------------------------------------------------------------------


class _FakeProcess:
    """A ``subprocess.Popen`` double: scripted ``poll()`` results,
    recorded ``kill``/``wait`` calls, and a fixed ``stderr`` tail."""

    def __init__(self, *, poll_results: List[Optional[int]], stderr: bytes = b"") -> None:
        self._poll_results = list(poll_results)
        self._last = self._poll_results[-1] if self._poll_results else None
        self.stderr = _FakeStderr(stderr)
        self.killed = False
        self.waited = False

    def poll(self) -> Optional[int]:
        if self._poll_results:
            self._last = self._poll_results.pop(0)
        return self._last

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: Optional[float] = None) -> None:
        self.waited = True


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _never_called(*args, **kwargs):  # pragma: no cover - only hit on a real bug
    raise AssertionError(f"unexpected call: args={args!r} kwargs={kwargs!r}")


def _make_lifecycle(tmp_path: Path, **overrides) -> DaemonLifecycle:
    directories = overrides.pop(
        "directories", Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    )
    kwargs = dict(
        directories=directories,
        discovery=overrides.pop("discovery", None),
        spawn=_never_called,
        lock_holder=lambda path: None,
        socket_reachable=lambda path: False,
        kill=_never_called,
        start_timeout=0.3,
        stop_timeout=0.3,
        poll_interval=0.01,
    )
    kwargs.update(overrides)
    return DaemonLifecycle(**kwargs)


# ---------------------------------------------------------------------------
# start() -- AC5/AC6: pre-flight probe, never spawns when contended
# ---------------------------------------------------------------------------


def test_start_raises_already_running_without_spawning_when_lock_is_held(tmp_path):
    lifecycle = _make_lifecycle(tmp_path, lock_holder=lambda path: "pid 4242")

    with pytest.raises(AlreadyRunning) as excinfo:
        lifecycle.start()

    # Unchanged holder identity -- no re-wrapping (this ticket's AC6).
    assert excinfo.value.holder == "pid 4242"


def test_start_spawns_and_returns_once_the_socket_is_reachable(tmp_path):
    spawn_calls = []

    def spawn(argv, **kwargs):
        spawn_calls.append(argv)
        return _FakeProcess(poll_results=[None, None, None])

    reachable_calls = {"n": 0}

    def socket_reachable(path):
        reachable_calls["n"] += 1
        return reachable_calls["n"] >= 3

    lifecycle = _make_lifecycle(tmp_path, spawn=spawn, socket_reachable=socket_reachable)

    lifecycle.start()  # does not raise

    assert len(spawn_calls) == 1
    assert spawn_calls[0][1:] == ["-m", "aprilcam.daemon"]
    assert spawn_calls[0][0] == sys.executable


def test_start_raises_connection_failed_when_process_exits_before_ready(tmp_path):
    def spawn(argv, **kwargs):
        return _FakeProcess(poll_results=[1], stderr=b"simulated startup failure")

    lifecycle = _make_lifecycle(tmp_path, spawn=spawn)

    with pytest.raises(ConnectionFailed) as excinfo:
        lifecycle.start()

    assert excinfo.value.host == "localhost"
    assert "simulated startup failure" in excinfo.value.reason


def test_start_raises_already_running_when_the_exit_was_a_lost_race(tmp_path):
    """The process exited before becoming ready *because* a concurrent
    start won the lock first -- re-probing after the exit must report
    that as AlreadyRunning, not a bare startup failure."""
    holder_calls = {"n": 0}

    def lock_holder(path):
        holder_calls["n"] += 1
        return None if holder_calls["n"] == 1 else "pid 555"

    def spawn(argv, **kwargs):
        return _FakeProcess(poll_results=[1], stderr=b"")

    lifecycle = _make_lifecycle(tmp_path, spawn=spawn, lock_holder=lock_holder)

    with pytest.raises(AlreadyRunning) as excinfo:
        lifecycle.start()

    assert excinfo.value.holder == "pid 555"


def test_start_kills_the_process_and_raises_when_it_never_becomes_ready(tmp_path):
    process = _FakeProcess(poll_results=[None] * 100)  # never exits on its own

    def spawn(argv, **kwargs):
        return process

    lifecycle = _make_lifecycle(tmp_path, spawn=spawn, socket_reachable=lambda path: False)

    with pytest.raises(ConnectionFailed):
        lifecycle.start()

    assert process.killed is True


# ---------------------------------------------------------------------------
# stop() -- quiet no-op vs. signal-and-wait
# ---------------------------------------------------------------------------


def test_stop_is_a_quiet_no_op_when_nothing_is_running(tmp_path):
    lifecycle = _make_lifecycle(tmp_path)  # lock_holder / kill both _never_called-safe

    lifecycle.stop()  # must not raise, must not call kill (lock_holder returns None)


def test_stop_sends_sigterm_to_the_holder_pid_and_waits_for_release(tmp_path):
    holder_calls = {"n": 0}

    def lock_holder(path):
        holder_calls["n"] += 1
        return "pid 777" if holder_calls["n"] <= 2 else None

    kill_calls = []

    def kill(pid, sig):
        kill_calls.append((pid, sig))

    lifecycle = _make_lifecycle(tmp_path, lock_holder=lock_holder, kill=kill)

    lifecycle.stop()

    assert kill_calls == [(777, 15)]  # signal.SIGTERM == 15 on POSIX


def test_stop_does_not_signal_when_the_holder_pid_is_unparseable(tmp_path):
    lifecycle = _make_lifecycle(tmp_path, lock_holder=lambda path: "unknown holder", kill=_never_called)

    lifecycle.stop()  # must not raise, must not attempt to signal anything


# ---------------------------------------------------------------------------
# status() -- local process view, with/without a reachable control plane
# ---------------------------------------------------------------------------


def test_status_reports_not_running_with_resolved_directories(tmp_path):
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    lifecycle = _make_lifecycle(tmp_path, directories=directories)

    status = lifecycle.status()

    assert status.running is False
    assert status.cameras == ()
    assert status.config_dir == str(directories.config_dir)
    assert status.state_dir == str(directories.state_dir)


class _FakeDaemon:
    def __init__(self, status) -> None:
        self._status = status

    def status(self):
        return self._status


class _FakeDiscovery:
    def __init__(self, daemon=None, error: Optional[Exception] = None) -> None:
        self._daemon = daemon
        self._error = error

    def connect(self, host=None):
        if self._error is not None:
            raise self._error
        return self._daemon


def test_status_delegates_to_the_rpc_view_when_the_lock_is_held_and_reachable(tmp_path):
    sentinel_status = object()
    discovery = _FakeDiscovery(daemon=_FakeDaemon(sentinel_status))
    lifecycle = _make_lifecycle(
        tmp_path, lock_holder=lambda path: "pid 1", discovery=discovery
    )

    assert lifecycle.status() is sentinel_status


def test_status_reports_running_with_empty_cameras_when_held_but_unreachable(tmp_path):
    discovery = _FakeDiscovery(error=ConnectionFailed("localhost", reason="not answering yet"))
    lifecycle = _make_lifecycle(
        tmp_path, lock_holder=lambda path: "pid 1", discovery=discovery
    )

    status = lifecycle.status()

    assert status.running is True
    assert status.cameras == ()


# ---------------------------------------------------------------------------
# The local-only decision this ticket recorded
# ---------------------------------------------------------------------------


def test_daemon_lifecycle_has_no_host_parameter_anywhere():
    """Structural check for the module docstring's claim: refusing
    ``-d remote`` is entirely the CLI's job because this class has
    nothing here that could even accept a remote target."""
    import inspect

    for name in ("__init__", "start", "stop", "status"):
        params = inspect.signature(getattr(DaemonLifecycle, name)).parameters
        assert "host" not in params


# ---------------------------------------------------------------------------
# Real subprocess round trip
# ---------------------------------------------------------------------------


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="ac-lc-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.integration
def test_start_stop_status_round_trip_against_a_real_daemon_process(
    tmp_path, short_runtime_dir, monkeypatch
):
    # Deliberately *no* monkeypatch of ``_writable_or_creatable`` here:
    # the spawned subprocess re-resolves ``Directories`` fresh in its
    # own interpreter and cannot see a monkeypatch applied in this
    # (parent) process, so parent and child must reach the same answer
    # via the *real*, unpatched cascade -- only the env vars below are
    # shared (Popen inherits this process's environment by default).
    monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_runtime_dir))

    directories = Directories()
    if directories.socket_dir != short_runtime_dir / "aprilcam":
        pytest.skip(
            "this sandbox's /run/aprilcam is available and writable, so "
            "socket_dir is not configurable down to a private tmp root "
            "(configuration.md: sockets are not configurable) -- cannot "
            "safely isolate a real-subprocess test from a real daemon here"
        )

    lifecycle = DaemonLifecycle(directories=directories, start_timeout=15.0, stop_timeout=15.0)

    assert lifecycle.status().running is False

    lifecycle.start()
    try:
        status = lifecycle.status()
        assert status.running is True
        # No fixture cameras *required* for this check -- but this
        # spawns a genuine subprocess against real ``Directories()``, so
        # on a machine that happens to have cameras physically attached
        # (this Mac does), the daemon's initial enumeration legitimately
        # finds them. Since 020-007's readiness-race fix, that
        # enumeration always completes *before* the control-plane socket
        # becomes reachable, so ``status.cameras`` here is never a
        # mid-enumeration partial read -- it is either genuinely empty
        # (headless CI) or the real, complete attached set (this Mac).
        # This test's purpose is the start/status/stop process round
        # trip, not a camera census (that is
        # test_lifecycle_integration.py's job, marked
        # ``@pytest.mark.camera``), so it asserts shape rather than a
        # hard-coded empty/non-empty expectation either way.
        assert isinstance(status.cameras, tuple)
        assert all(c.name for c in status.cameras)

        # A second instance, same directories/env, is refused and names
        # the holder -- AlreadyRunning surviving unchanged through a
        # genuine subprocess, not a fake (this ticket's AC6).
        contender = DaemonLifecycle(directories=directories, start_timeout=2.0)
        with pytest.raises(AlreadyRunning):
            contender.start()
        assert lifecycle.status().running is True
    finally:
        lifecycle.stop()

    assert lifecycle.status().running is False
