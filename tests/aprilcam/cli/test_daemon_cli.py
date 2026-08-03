"""Tests for ``aprilcam.cli.daemon`` (ticket 020-006).

Headless: every test drives the ``cmd_start``/``cmd_stop``/``cmd_status``
functions directly against fake ``DaemonLifecycle``/``Discovery``
objects injected through their ``lifecycle_factory``/``discovery_factory``
keyword seams, exactly like ``tests/aprilcam/client/test_lifecycle.py``'s
own fake-driven style. No real daemon process or camera hardware is
touched here -- the ``@pytest.mark.camera`` acceptance pass against a
real local daemon lives in ``test_cli_integration.py``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Optional, Tuple

import pytest

from aprilcam.cli import daemon as daemon_cli
from aprilcam.errors import AlreadyRunning, ConnectionFailed
from aprilcam.types import CameraState


def _args(*, daemon: Optional[str] = None) -> argparse.Namespace:
    return argparse.Namespace(daemon=daemon)


def _camera(name: str = "arducam-ov9782-usb-camera", number: int = 1) -> CameraState:
    return CameraState(
        name=name,
        present=True,
        usable=True,
        playfield=None,
        calibrated=False,
        number=number,
    )


@dataclass
class _FakeStatus:
    running: bool
    cameras: Tuple[CameraState, ...] = ()
    config_dir: str = "/config"
    state_dir: str = "/state"


@dataclass
class _FakeLifecycle:
    """Stands in for ``client.lifecycle.DaemonLifecycle``: scripted
    ``start``/``stop`` outcomes, and a ``status`` that reflects whatever
    happened so far -- close enough to the real class's observable
    behaviour for these argument-parsing/formatting tests."""

    _running: bool = False
    _cameras: Tuple[CameraState, ...] = ()
    start_raises: Optional[Exception] = None
    stop_raises: Optional[Exception] = None
    start_calls: int = field(default=0)
    stop_calls: int = field(default=0)

    def start(self) -> None:
        self.start_calls += 1
        if self.start_raises is not None:
            raise self.start_raises
        self._running = True

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_raises is not None:
            raise self.stop_raises
        self._running = False

    def status(self) -> _FakeStatus:
        return _FakeStatus(running=self._running, cameras=self._cameras if self._running else ())


@dataclass
class _FakeDaemon:
    _status: _FakeStatus

    def status(self) -> _FakeStatus:
        return self._status


@dataclass
class _FakeDiscovery:
    daemon: Optional[_FakeDaemon] = None
    connect_raises: Optional[Exception] = None
    connected_with: Optional[str] = None

    def connect(self, host: Optional[str] = None):
        self.connected_with = host
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.daemon


# ---------------------------------------------------------------------------
# start -- AC: UC-13.S-3 round trip, UC-13.S-4 remote refusal
# ---------------------------------------------------------------------------


def test_start_refuses_a_remote_daemon_flag_without_touching_lifecycle(capsys):
    lifecycle = _FakeLifecycle()

    code = daemon_cli.cmd_start(_args(daemon="pi-field.local"), lifecycle_factory=lambda: lifecycle)

    assert code == 1
    assert lifecycle.start_calls == 0
    err = capsys.readouterr().err
    assert "pi-field.local" in err
    assert "SSH" in err
    assert "local-only" in err


def test_start_starts_the_local_daemon_and_reports_camera_count(capsys):
    lifecycle = _FakeLifecycle(_cameras=(_camera(), _camera("hd-usb-camera", 2)))

    code = daemon_cli.cmd_start(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    assert lifecycle.start_calls == 1
    out = capsys.readouterr().out
    assert "daemon started" in out
    assert "2 camera(s)" in out


def test_start_reports_already_running_naming_the_holder(capsys):
    lifecycle = _FakeLifecycle(start_raises=AlreadyRunning("pid 4242"))

    code = daemon_cli.cmd_start(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 1
    err = capsys.readouterr().err
    assert "pid 4242" in err
    assert "already running" in err


# ---------------------------------------------------------------------------
# stop -- AC: UC-13.S-3, remote refusal, quiet no-op vs. real stop
# ---------------------------------------------------------------------------


def test_stop_refuses_a_remote_daemon_flag_without_touching_lifecycle(capsys):
    lifecycle = _FakeLifecycle(_running=True)

    code = daemon_cli.cmd_stop(_args(daemon="pi-field.local"), lifecycle_factory=lambda: lifecycle)

    assert code == 1
    assert lifecycle.stop_calls == 0
    err = capsys.readouterr().err
    assert "pi-field.local" in err
    assert "SSH" in err


def test_stop_stops_a_running_daemon(capsys):
    lifecycle = _FakeLifecycle(_running=True)

    code = daemon_cli.cmd_stop(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    assert lifecycle.stop_calls == 1
    assert "daemon stopped" in capsys.readouterr().out


def test_stop_reports_not_running_when_nothing_was_running(capsys):
    lifecycle = _FakeLifecycle(_running=False)

    code = daemon_cli.cmd_stop(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    assert lifecycle.stop_calls == 1  # still called -- DaemonLifecycle.stop() is itself a no-op
    assert "daemon not running" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# restart -- stop-then-start, local-only for the same reason as its parts
# ---------------------------------------------------------------------------


def test_restart_refuses_a_remote_daemon_flag_without_touching_lifecycle(capsys):
    lifecycle = _FakeLifecycle(_running=True)

    code = daemon_cli.cmd_restart(
        _args(daemon="pi-field.local"), lifecycle_factory=lambda: lifecycle
    )

    assert code == 1
    assert lifecycle.stop_calls == 0
    assert lifecycle.start_calls == 0
    err = capsys.readouterr().err
    assert "pi-field.local" in err
    assert "SSH" in err


def test_restart_stops_then_starts_a_running_daemon(capsys):
    lifecycle = _FakeLifecycle(_running=True, _cameras=(_camera(),))

    code = daemon_cli.cmd_restart(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    assert (lifecycle.stop_calls, lifecycle.start_calls) == (1, 1)
    out = capsys.readouterr().out
    assert "daemon restarted" in out
    assert "1 camera(s)" in out


def test_restart_on_a_stopped_daemon_still_starts_it(capsys):
    """``stop()`` is a quiet no-op when nothing is running (UC-13), so
    ``restart`` doubles as "start it, whatever state it was in" -- but
    it says *started*, not *restarted*, so the operator is not told a
    process was replaced when none existed."""
    lifecycle = _FakeLifecycle(_running=False, _cameras=(_camera(),))

    code = daemon_cli.cmd_restart(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    assert (lifecycle.stop_calls, lifecycle.start_calls) == (1, 1)
    out = capsys.readouterr().out
    assert "daemon started" in out
    assert "restarted" not in out


def test_restart_reports_a_failed_start_without_claiming_success(capsys):
    """A restart whose start leg fails must surface the typed error --
    reporting "restarted" after the daemon failed to come back up is
    exactly the blind success reporting UC-03's sibling rule exists to
    kill."""
    lifecycle = _FakeLifecycle(
        _running=True, start_raises=AlreadyRunning("pid 4242")
    )

    code = daemon_cli.cmd_restart(_args(), lifecycle_factory=lambda: lifecycle)

    assert code != 0
    captured = capsys.readouterr()
    assert "restarted" not in captured.out
    assert "4242" in captured.err


# ---------------------------------------------------------------------------
# status -- AC: local view vs. -d remote RPC view (UC-13's drafted answer)
# ---------------------------------------------------------------------------


def test_status_with_no_flag_uses_the_local_lifecycle_view(capsys):
    lifecycle = _FakeLifecycle(_running=True, _cameras=(_camera(),))

    code = daemon_cli.cmd_status(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    out = capsys.readouterr().out
    assert "local: running" in out
    assert "cameras: 1" in out
    assert "arducam-ov9782-usb-camera" in out


def test_status_not_running_before_any_start(capsys):
    lifecycle = _FakeLifecycle(_running=False)

    code = daemon_cli.cmd_status(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    out = capsys.readouterr().out
    assert "local: not running" in out
    assert "cameras: 0" in out


def test_status_with_daemon_flag_connects_via_discovery_not_lifecycle(capsys):
    remote_status = _FakeStatus(running=True, cameras=(_camera(),), config_dir="/etc/aprilcam", state_dir="/var/lib/aprilcam")
    discovery = _FakeDiscovery(daemon=_FakeDaemon(remote_status))
    lifecycle = _FakeLifecycle()  # must never be touched

    code = daemon_cli.cmd_status(
        _args(daemon="pi-field.local"),
        lifecycle_factory=lambda: lifecycle,
        discovery_factory=lambda: discovery,
    )

    assert code == 0
    assert discovery.connected_with == "pi-field.local"
    assert lifecycle.start_calls == 0 and lifecycle.stop_calls == 0
    out = capsys.readouterr().out
    assert "pi-field.local: running" in out
    assert "/etc/aprilcam" in out
    assert "/var/lib/aprilcam" in out


def test_status_with_daemon_flag_reports_connection_failure(capsys):
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="no answer"))

    code = daemon_cli.cmd_status(
        _args(daemon="pi-field.local"), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err
    assert "no answer" in err


# ---------------------------------------------------------------------------
# argparse wiring -- flags exist, -h stays help, incomplete subcommand fails
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="aprilcam")
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    daemon_cli.add_subparser(subparsers)
    return top


def test_daemon_alone_prints_help_and_fails(capsys):
    parser = _build_parser()
    args = parser.parse_args(["daemon"])

    code = args.func(args)

    assert code == 1
    assert "usage: aprilcam daemon" in capsys.readouterr().out


def test_daemon_start_accepts_the_daemon_flag_without_erroring_at_parse_time():
    parser = _build_parser()
    args = parser.parse_args(["daemon", "start", "-d", "pi-field.local"])
    assert args.daemon == "pi-field.local"
    assert args.func is daemon_cli.cmd_start


def test_daemon_list_help_flag_is_still_help_not_daemon_target():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["daemon", "status", "-h"])
    assert excinfo.value.code == 0
