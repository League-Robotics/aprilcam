"""Tests for ``aprilcam.daemon.lifecycle`` (ticket 020-004).

Fully headless: :class:`_FakeListener` stands in for a real
``grpc.Server`` (``daemon.lifecycle.ControlPlaneListener``, the base
class it structurally mirrors, is exactly two methods for this reason),
so these tests exercise the flock/``AlreadyRunning``/shutdown-ordering
mechanics without any gRPC or camera import at all. Every test uses
``tmp_path``-derived lock and socket paths so it can never collide with
a real daemon running on the machine executing the suite (this ticket's
Testing section requirement).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import pytest

from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import DaemonLifecycle
from aprilcam.errors import AlreadyRunning


class _FakeListener:
    """A ``ControlPlaneListener`` double: records start/stop calls,
    nothing more."""

    def __init__(self) -> None:
        self.started = False
        self.stop_grace_calls: List[float] = []

    def start(self) -> None:
        self.started = True

    def stop(self, grace: float) -> None:
        self.stop_grace_calls.append(grace)
        self.started = False


def _make_lifecycle(
    tmp_path: Path,
    *,
    subdir: str = "",
    events: Optional[List[str]] = None,
    listeners: Optional[List[_FakeListener]] = None,
    status_source=None,
    lock_path: Optional[Path] = None,
    socket_path: Optional[Path] = None,
    with_hooks: bool = True,
) -> DaemonLifecycle:
    """Build a ``DaemonLifecycle`` wired to fakes, recording every
    hook invocation (in call order) into *events* when supplied."""
    events = events if events is not None else []
    listeners = listeners if listeners is not None else []
    root = tmp_path / subdir if subdir else tmp_path

    def build_listener() -> _FakeListener:
        events.append("build_listener")
        listener = _FakeListener()
        listeners.append(listener)
        return listener

    kwargs = {}
    if with_hooks:
        kwargs["watch"] = lambda: events.append("watch")
        kwargs["stop_watch"] = lambda: events.append("stop_watch")
        kwargs["on_stop"] = lambda: events.append("on_stop")

    return DaemonLifecycle(
        directories=Directories(config_dir=root / "config", state_dir=root / "state"),
        build_listener=build_listener,
        status_source=status_source or (lambda: "running-status"),
        lock_path=lock_path if lock_path is not None else root / "daemon.lock",
        socket_path=socket_path if socket_path is not None else root / "control.sock",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# start() -- zero cameras / zero config is success (UC-01.S-2)
# ---------------------------------------------------------------------------


def test_start_with_no_cameras_and_no_config_succeeds(tmp_path):
    """AC1: nothing here inspects camera count or configuration at
    all -- start() only ever fails on lock contention."""
    events: List[str] = []
    lifecycle = _make_lifecycle(tmp_path, events=events)

    lifecycle.start()

    # "watch" (the initial active enumeration, synchronous, plus handing
    # off to passive watching) runs *before* "build_listener" -- see
    # test_start_calls_watch_before_building_the_control_plane_listener
    # below for why this order is load-bearing, not incidental.
    assert events == ["watch", "build_listener"]
    assert lifecycle.status() == "running-status"


def test_start_calls_watch_before_building_the_control_plane_listener(tmp_path):
    """Regression test (ticket 020-007): the client-side readiness
    signal (``client/lifecycle.py``) treats "the control-plane socket
    accepts a connection" as "the daemon is ready" -- so if the listener
    were built/started *before* the initial camera enumeration
    completes, a caller could see the socket become reachable, issue a
    ``camera list``/``daemon status`` RPC, and observe an empty or
    partial registry even though the daemon's own hardware is fully
    enumerable. This test locks in the fix: the listener (and therefore
    the socket becoming reachable at all) is built strictly after
    ``watch`` -- which runs the synchronous enumeration -- has returned,
    for every ``start()`` call, not just the first.
    """
    events: List[str] = []
    lifecycle = _make_lifecycle(tmp_path, events=events)

    lifecycle.start()
    assert events.index("watch") < events.index("build_listener")

    lifecycle.stop()
    events.clear()
    lifecycle.start()
    assert events.index("watch") < events.index("build_listener")


def test_start_marks_the_listener_started(tmp_path):
    listeners: List[_FakeListener] = []
    lifecycle = _make_lifecycle(tmp_path, listeners=listeners)

    lifecycle.start()

    assert len(listeners) == 1
    assert listeners[0].started is True


def test_status_delegates_to_status_source_while_running(tmp_path):
    calls = {"n": 0}

    def status_source():
        calls["n"] += 1
        return "running-status"

    lifecycle = _make_lifecycle(tmp_path, status_source=status_source)
    lifecycle.start()

    assert lifecycle.status() == "running-status"
    assert calls["n"] == 1


def test_status_before_start_reports_not_running_with_resolved_directories(tmp_path):
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    lifecycle = DaemonLifecycle(
        directories=directories,
        build_listener=lambda: _FakeListener(),
        status_source=lambda: "unused",
        lock_path=tmp_path / "daemon.lock",
        socket_path=tmp_path / "control.sock",
    )

    status = lifecycle.status()

    assert status.running is False
    assert status.cameras == ()
    assert status.config_dir == str(directories.config_dir)
    assert status.state_dir == str(directories.state_dir)


# ---------------------------------------------------------------------------
# AlreadyRunning (UC-01.S-3)
# ---------------------------------------------------------------------------


def test_second_start_at_the_same_lock_path_raises_already_running_naming_the_holder(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    socket_path = tmp_path / "control.sock"
    events_a: List[str] = []
    events_b: List[str] = []
    listeners_a: List[_FakeListener] = []

    first = _make_lifecycle(
        tmp_path, subdir="a", events=events_a, listeners=listeners_a,
        lock_path=lock_path, socket_path=socket_path,
    )
    first.start()

    second = _make_lifecycle(tmp_path, subdir="b", events=events_b, lock_path=lock_path, socket_path=socket_path)

    with pytest.raises(AlreadyRunning) as excinfo:
        second.start()

    # Names the existing instance's identity -- this process's own pid,
    # since both "instances" run in this one test process (flock treats
    # separate open() calls independently even within one process, so
    # this genuinely exercises contention, not a mocked shortcut).
    assert str(os.getpid()) in excinfo.value.holder
    # The second instance never touched the control-plane listener --
    # lock acquisition failed before build_listener was ever called
    # (UC-01.S-3: "does not disturb the running daemon").
    assert events_b == []

    # The first daemon is undisturbed.
    assert listeners_a[0].started is True


def test_a_correctly_ordered_instance_can_start_after_the_holder_stops(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    socket_path = tmp_path / "control.sock"

    first = _make_lifecycle(tmp_path, subdir="a", lock_path=lock_path, socket_path=socket_path)
    first.start()

    second = _make_lifecycle(tmp_path, subdir="b", lock_path=lock_path, socket_path=socket_path)
    with pytest.raises(AlreadyRunning):
        second.start()

    first.stop()

    third = _make_lifecycle(tmp_path, subdir="c", lock_path=lock_path, socket_path=socket_path)
    third.start()  # does not raise
    assert third.status() == "running-status"


# ---------------------------------------------------------------------------
# stop() -- clean shutdown ordering
# ---------------------------------------------------------------------------


def test_stop_releases_the_lock_so_a_new_instance_can_start(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    socket_path = tmp_path / "control.sock"

    first = _make_lifecycle(tmp_path, subdir="a", lock_path=lock_path, socket_path=socket_path)
    first.start()
    first.stop()

    second = _make_lifecycle(tmp_path, subdir="b", lock_path=lock_path, socket_path=socket_path)
    second.start()  # would raise AlreadyRunning if the lock were still held


def test_stop_calls_hooks_in_the_documented_order(tmp_path):
    events: List[str] = []
    lifecycle = _make_lifecycle(tmp_path, events=events)
    lifecycle.start()
    events.clear()

    lifecycle.stop()

    assert events == ["stop_watch", "on_stop"]


def test_stop_when_not_running_is_a_quiet_no_op(tmp_path):
    events: List[str] = []
    lifecycle = _make_lifecycle(tmp_path, events=events)

    lifecycle.stop()  # never started

    assert events == []


def test_stop_marks_status_not_running(tmp_path):
    lifecycle = _make_lifecycle(tmp_path)
    lifecycle.start()
    lifecycle.stop()

    status = lifecycle.status()
    assert status.running is False


def test_stop_passes_the_default_grace_period_to_the_listener(tmp_path):
    listeners: List[_FakeListener] = []
    lifecycle = _make_lifecycle(tmp_path, listeners=listeners)
    lifecycle.start()
    lifecycle.stop()

    assert listeners[0].stop_grace_calls == [5.0]


def test_stop_honours_a_custom_grace_period(tmp_path):
    listeners: List[_FakeListener] = []

    def build_listener() -> _FakeListener:
        listener = _FakeListener()
        listeners.append(listener)
        return listener

    lifecycle = DaemonLifecycle(
        directories=Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state"),
        build_listener=build_listener,
        status_source=lambda: "running-status",
        lock_path=tmp_path / "daemon.lock",
        socket_path=tmp_path / "control.sock",
        stop_grace=1.5,
    )
    lifecycle.start()
    lifecycle.stop()

    assert listeners[0].stop_grace_calls == [1.5]


# ---------------------------------------------------------------------------
# restart mechanics (UC-01.S-1's restart-stability, mechanism level --
# the real registry-numbers-persist check is
# test_lifecycle_integration.py, against real hardware)
# ---------------------------------------------------------------------------


def test_restart_builds_a_fresh_listener_and_reacquires_the_lock(tmp_path):
    events: List[str] = []
    listeners: List[_FakeListener] = []
    lifecycle = _make_lifecycle(tmp_path, events=events, listeners=listeners)

    lifecycle.start()
    lifecycle.stop()
    lifecycle.start()

    assert len(listeners) == 2
    assert listeners[0] is not listeners[1]
    assert listeners[1].started is True
    assert lifecycle.status() == "running-status"  # delegates while running again


def test_restart_refuses_a_contender_during_the_running_window(tmp_path):
    """A second instance cannot start while the first is up, restarted
    or not -- the lock, not process identity, is what is enforced."""
    lock_path = tmp_path / "daemon.lock"
    first = _make_lifecycle(tmp_path, subdir="a", lock_path=lock_path, socket_path=tmp_path / "control.sock")
    first.start()
    first.stop()
    first.start()  # restarted, still holding the lock

    contender = _make_lifecycle(
        tmp_path, subdir="b", lock_path=lock_path, socket_path=tmp_path / "control2.sock"
    )
    with pytest.raises(AlreadyRunning):
        contender.start()


# ---------------------------------------------------------------------------
# path resolution defaults
# ---------------------------------------------------------------------------


def test_default_lock_and_socket_paths_live_under_socket_dir(tmp_path):
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    lifecycle = DaemonLifecycle(
        directories=directories,
        build_listener=lambda: _FakeListener(),
        status_source=lambda: "unused",
    )

    assert lifecycle.lock_path == directories.socket_dir / "daemon.lock"
    assert lifecycle.socket_path == directories.socket_dir / "control.sock"
