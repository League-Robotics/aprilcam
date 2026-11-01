"""Tests for ``aprilcam.cli.daemon`` (ticket 020-006; ``list`` coverage
added by ticket 028-005).

Headless: every test drives the ``cmd_start``/``cmd_stop``/``cmd_status``/
``cmd_list`` functions directly against fake ``DaemonLifecycle``/
``Discovery`` objects injected through their
``lifecycle_factory``/``discovery_factory`` keyword seams, exactly like
``tests/aprilcam/client/test_lifecycle.py``'s own fake-driven style. No
real daemon process or camera hardware is touched here -- the
``@pytest.mark.camera`` acceptance pass against a real local daemon
lives in ``test_cli_integration.py``, and this module's own
``@pytest.mark.integration`` test at the bottom (ticket 028-005) proves
``cmd_list`` against two genuinely running local daemon processes on
alternate ports, mirroring ``tests/aprilcam/client/test_discovery.py``'s
own real two-daemon fixture technique (ticket 028-004) rather than
inventing a second one.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Tuple

import pytest

from aprilcam.cli import daemon as daemon_cli
from aprilcam.errors import AlreadyRunning, ConnectionFailed, DiscoveryFailed
from aprilcam.types import CameraState
from aprilcam.cli._common import add_daemon_option


def _args(*, daemon: Optional[str] = None) -> argparse.Namespace:
    return argparse.Namespace(daemon=daemon)


def _camera(
    name: str = "arducam-ov9782-usb-camera",
    number: int = 1,
    *,
    playfield: Optional[str] = None,
) -> CameraState:
    return CameraState(
        name=name,
        present=True,
        usable=True,
        playfield=playfield,
        calibrated=False,
        number=number,
    )


@dataclass
class _FakeStatus:
    running: bool
    cameras: Tuple[CameraState, ...] = ()
    config_dir: str = "/config"
    state_dir: str = "/state"
    other_daemon_processes: Tuple[int, ...] = ()


@dataclass
class _FakeLifecycle:
    """Stands in for ``client.lifecycle.DaemonLifecycle``: scripted
    ``start``/``stop`` outcomes, and a ``status`` that reflects whatever
    happened so far -- close enough to the real class's observable
    behaviour for these argument-parsing/formatting tests."""

    _running: bool = False
    _cameras: Tuple[CameraState, ...] = ()
    # Ticket 031-002: the local orphan-process report -- unlike `_cameras`
    # above, not gated on `_running` (SUC-002's main flow: a candidate
    # orphan can exist whether or not the real daemon is currently up).
    _other_daemon_processes: Tuple[int, ...] = ()
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
        return _FakeStatus(
            running=self._running,
            cameras=self._cameras if self._running else (),
            other_daemon_processes=self._other_daemon_processes,
        )


@dataclass
class _FakeDaemon:
    _status: _FakeStatus

    def status(self) -> _FakeStatus:
        return self._status


@dataclass
class _FakeDiscovery:
    """Extended for ``cmd_list`` coverage (ticket 028-005) with the same
    ``infos``/``daemons_by_host``/``find_all_raises`` shape
    ``tests/aprilcam/cli/test_camera_cli.py``'s own ``_FakeDiscovery``
    already uses for ``camera list --all`` -- same pattern, not a new
    one. Every pre-existing ``status``-only test still constructs this
    with only ``daemon=``/``connect_raises=``, which keeps working
    unchanged: ``daemons_by_host`` defaults empty, so ``connect()``
    falls through to the original ``connect_raises``/``daemon``
    behaviour exactly as before.
    """

    daemon: Optional[_FakeDaemon] = None
    connect_raises: Optional[Exception] = None
    connected_with: Optional[str] = None
    daemons_by_host: dict = field(default_factory=dict)
    infos: Tuple[object, ...] = ()
    find_all_raises: Optional[Exception] = None

    def connect(self, host: Optional[str] = None):
        self.connected_with = host
        if host in self.daemons_by_host:
            target = self.daemons_by_host[host]
            if isinstance(target, Exception):
                raise target
            return target
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.daemon

    def find_all(self):
        if self.find_all_raises is not None:
            raise self.find_all_raises
        return self.infos


@dataclass(frozen=True)
class _Info:
    """A ``DaemonInfo``-shaped stand-in -- ``fan_out()`` (and therefore
    ``cmd_list``) only reads ``.host``/``.is_local``, so this is the
    same minimal shape ``test_camera_cli.py``'s own ``_Info`` uses."""

    host: str
    is_local: bool
    name: str = ""


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


def test_status_prints_orphan_processes_distinctly_when_present(capsys):
    """Ticket 031-002, SUC-002: an orphan candidate is reported
    distinctly from the lock holder, not conflated with "running"."""
    lifecycle = _FakeLifecycle(_running=True, _other_daemon_processes=(4242,))

    code = daemon_cli.cmd_status(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    out = capsys.readouterr().out
    assert "local: running" in out
    assert "orphan processes" in out
    assert "4242" in out


def test_status_omits_orphan_line_when_none_found(capsys):
    """AC2/AC3: an empty scan is not itself reported as anything --
    output matches today's (pre-ticket) shape exactly."""
    lifecycle = _FakeLifecycle(_running=True, _cameras=(_camera(),))

    code = daemon_cli.cmd_status(_args(), lifecycle_factory=lambda: lifecycle)

    assert code == 0
    out = capsys.readouterr().out
    assert "orphan" not in out


def test_status_with_daemon_flag_never_prints_an_orphan_line(capsys):
    """AC6: the orphan report is local-only -- a ``-d HOST`` RPC-answered
    status never carries one, even if (hypothetically) fabricated on the
    fake, matching real ``daemon_status_from_proto``'s field-for-field
    reconstruction which never sets it either."""
    remote_status = _FakeStatus(running=True, other_daemon_processes=())
    discovery = _FakeDiscovery(daemon=_FakeDaemon(remote_status))

    code = daemon_cli.cmd_status(
        _args(daemon="pi-field.local"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert "orphan" not in capsys.readouterr().out


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
# list -- AC: SUC-004 (both appear, camera count + playfields, unreachable
# flagged not dropped), SUC-005 (empty), SUC-006 (DiscoveryFailed distinct
# from empty). ``cmd_list`` is a thin call into ``fan_out()`` (unmodified),
# so these tests exercise the same fake-``Discovery`` seam
# ``test_camera_cli.py``'s own ``--all`` tests use, not a new pattern.
# ---------------------------------------------------------------------------


def test_list_prints_every_daemon_with_camera_count_and_playfields(capsys):
    local_status = _FakeStatus(
        running=True,
        cameras=(_camera(playfield="main-playfield"), _camera("hd-usb-camera", 2)),
    )
    remote_status = _FakeStatus(running=True, cameras=(_camera("remote-cam", 1),))
    discovery = _FakeDiscovery(
        infos=(
            _Info(host="localhost", is_local=True),
            _Info(host="192.0.2.5:5280", is_local=False),
        ),
        daemons_by_host={
            None: _FakeDaemon(local_status),
            "192.0.2.5:5280": _FakeDaemon(remote_status),
        },
    )

    code = daemon_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "localhost (local):" in out
    assert "cameras: 2" in out
    assert "playfields: main-playfield" in out
    assert "192.0.2.5:5280:" in out
    assert "cameras: 1" in out


def test_list_shows_none_marker_for_a_reachable_daemon_with_no_linked_playfields(capsys):
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True),),
        daemons_by_host={None: _FakeDaemon(_FakeStatus(running=True, cameras=(_camera(),)))},
    )

    code = daemon_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert "playfields: (none)" in capsys.readouterr().out


def test_list_deduplicates_and_sorts_linked_playfields(capsys):
    cameras = (
        _camera("cam-a", 1, playfield="zeta"),
        _camera("cam-b", 2, playfield="alpha"),
        _camera("cam-c", 3, playfield="alpha"),
    )
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True),),
        daemons_by_host={None: _FakeDaemon(_FakeStatus(running=True, cameras=cameras))},
    )

    code = daemon_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert "playfields: alpha, zeta" in capsys.readouterr().out


def test_list_flags_an_advertising_but_unreachable_daemon_without_dropping_it(capsys):
    """SUC-004: a daemon that advertises but does not answer RPCs is
    listed and flagged, never silently filtered out -- ``fan_out()``'s
    existing per-entry try/except, exercised unmodified here."""
    discovery = _FakeDiscovery(
        infos=(
            _Info(host="localhost", is_local=True),
            _Info(host="dead-host:5280", is_local=False),
        ),
        daemons_by_host={
            None: _FakeDaemon(_FakeStatus(running=True, cameras=(_camera(),))),
            "dead-host:5280": ConnectionFailed("dead-host:5280", reason="no answer"),
        },
    )

    code = daemon_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 1  # partial failure is visible in the exit code
    out, err = capsys.readouterr()
    assert "localhost (local):" in out
    assert "cameras: 1" in out
    assert "dead-host:5280: unreachable" in err
    assert "no answer" in err


def test_list_with_no_daemons_anywhere_prints_explicit_marker_and_exits_ok(capsys):
    """SUC-005: empty is success, not error, and never blank output."""
    discovery = _FakeDiscovery(infos=())

    code = daemon_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no daemons found)" in capsys.readouterr().out


def test_list_reports_discovery_failed_visibly_distinct_from_empty(capsys):
    """SUC-006: a broken browse must never look like SUC-005's ordinary
    empty result -- distinct message, non-zero exit."""
    discovery = _FakeDiscovery(find_all_raises=DiscoveryFailed("multicast blocked"))

    code = daemon_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    out, err = capsys.readouterr()
    assert "(no daemons found)" not in out
    assert "multicast blocked" in err


# ---------------------------------------------------------------------------
# argparse wiring -- flags exist, -h stays help, incomplete subcommand fails
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="aprilcam")
    add_daemon_option(top)
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


def test_daemon_start_accepts_the_root_daemon_flag_without_erroring_at_parse_time():
    parser = _build_parser()
    args = parser.parse_args(["-d", "pi-field.local", "daemon", "start"])
    assert args.daemon == "pi-field.local"
    assert args.func is daemon_cli.cmd_start


def test_daemon_list_help_flag_is_still_help_not_daemon_target():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["daemon", "status", "-h"])
    assert excinfo.value.code == 0


def test_daemon_list_subcommand_is_wired_with_no_daemon_or_all_attribute():
    """``daemon list`` takes no ``--all`` of its own; ``.daemon`` exists
    only because ``-d`` is a root option, and ``cmd_list`` ignores it."""
    parser = _build_parser()
    args = parser.parse_args(["daemon", "list"])
    assert args.func is daemon_cli.cmd_list
    assert args.daemon is None
    assert not hasattr(args, "all")


def test_daemon_list_rejects_the_daemon_flag_at_parse_time():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["daemon", "list", "-d", "pi-field.local"])
    assert excinfo.value.code == 2


def test_daemon_list_rejects_the_all_flag_at_parse_time():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["daemon", "list", "--all"])
    assert excinfo.value.code == 2


def test_daemon_list_help_shows_no_daemon_or_all_options(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["daemon", "list", "-h"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--daemon" not in out
    assert "--all" not in out


# ---------------------------------------------------------------------------
# ticket 028-005: one real, two-local-daemon-alternate-port integration test
# for ``daemon list`` itself (SUC-004's own AC: "verified via two local
# processes on alternate ports"). Mirrors
# ``tests/aprilcam/client/test_discovery.py``'s real two-daemon fixture
# technique exactly (ticket 028-004: real ``daemon.__main__.build()``, real
# TCP bind, real ``MdnsAdvertiser`` with a synthetic second hostname, real
# default ``Discovery`` browse) -- reusing the *approach* that ticket 004
# already proved works, rather than inventing a second one, while driving
# ``daemon_cli.cmd_list`` (not raw ``Discovery.find_all()``) so this ticket's
# own CLI-formatting half is what gets proven end to end. ``start_watch=
# False`` (matching that fixture) means neither stack ever runs a real
# camera probe, so both report zero cameras regardless of what hardware, if
# any, is attached to the machine running this suite -- no ``@pytest.mark.
# camera`` needed.
# ---------------------------------------------------------------------------


class _SecondHostAdvertiser:
    """See ``test_discovery.py``'s identical class for the full
    rationale: delegates to a real ``MdnsAdvertiser``, but bakes in a
    synthetic ``hostname=`` override so the second real daemon process
    advertises under a name distinct from this machine's own --
    otherwise ticket 004's client-side self-filter would collapse it
    into "that's just my own local daemon"."""

    def __init__(self) -> None:
        from aprilcam.daemon.mdns import MdnsAdvertiser

        self._real = MdnsAdvertiser()

    def start(self, tcp_port, *, hostname=None):
        self._real.start(tcp_port, hostname="test-second-host")

    def stop(self) -> None:
        self._real.stop()


@pytest.fixture
def _two_short_socket_dirs() -> Iterator[Tuple[Path, Path]]:
    d1 = Path(tempfile.mkdtemp(prefix="ac-daemonlist-int1-", dir="/tmp"))
    d2 = Path(tempfile.mkdtemp(prefix="ac-daemonlist-int2-", dir="/tmp"))
    try:
        yield d1, d2
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)


@pytest.mark.integration
def test_daemon_list_reports_two_real_local_daemons_on_alternate_ports(
    tmp_path: Path,
    _two_short_socket_dirs: Tuple[Path, Path],
    monkeypatch,
    capsys,
):
    """SUC-004, proven end to end for ``aprilcam daemon list`` itself:
    a real local daemon (found via the lock-probe) plus a second real
    daemon process standing in for a second host (found via real mDNS
    browse, alternate OS-assigned TCP port, synthetic hostname) both
    appear, the local one flagged ``(local)``, each with its camera
    count and linked playfields shown."""
    from aprilcam.client.discovery import Discovery
    from aprilcam.config.directories import Directories
    from aprilcam.daemon import __main__ as daemon_main

    short1, short2 = _two_short_socket_dirs

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short1))
    directories1 = Directories(config_dir=tmp_path / "config1", state_dir=tmp_path / "state1")
    assert directories1.socket_dir == short1 / "aprilcam"

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short2))
    directories2 = Directories(config_dir=tmp_path / "config2", state_dir=tmp_path / "state2")
    assert directories2.socket_dir == short2 / "aprilcam"

    lifecycle1 = daemon_main.build(directories1, start_watch=False, tcp_port=0)
    lifecycle1.start()
    try:
        monkeypatch.setattr(daemon_main, "MdnsAdvertiser", _SecondHostAdvertiser)
        lifecycle2 = daemon_main.build(directories2, start_watch=False, tcp_port=0)
        lifecycle2.start()
        try:
            discovery = Discovery(directories=directories1)

            code = daemon_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

            assert code == 0
            out = capsys.readouterr().out
            assert " (local):" in out
            assert "cameras: 0" in out
            assert "playfields: (none)" in out
            # The remote entry's label is "ip:port" (Discovery.find_all's
            # "Load-bearing formatting" note) -- assert structurally
            # rather than pin an exact address.
            assert out.count("cameras: 0") == 2
            assert out.count("playfields: (none)") == 2
        finally:
            lifecycle2.stop()
    finally:
        lifecycle1.stop()
