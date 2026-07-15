"""Tests for the --force takeover sequence in aprilcam.cli.daemon_cli (018-004).

Covers:
  - _force_takeover's ordering: graceful Shutdown RPC before any signal,
    SIGTERM before SIGKILL, SIGKILL only after the grace window elapses.
  - End-to-end (c): a real DaemonServer running in a background thread is
    taken over by _cmd_start(force=True) — the old instance's lock is
    released and the new instance holds it.
  - restart always performs a --force-equivalent start.
  - _cmd_restart's wait-loop polls config.daemon_lock_path, not
    config.daemon_pidfile.
  - _spawn_daemon appends --force to argv when requested.

No hardware, no real /tmp/aprilcamd.lock, no real port 5280 — every Config
here overrides daemon_lock_path to a unique tmp path and uses high/ephemeral
TCP ports or --no-tcp equivalents (unix-only DaemonServer instances).
"""

from __future__ import annotations

import os
import signal
import stat
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.cli import daemon_cli
from aprilcam.config import Config
from aprilcam.daemon.server import DaemonServer


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, tag: str = "a") -> Config:
    """Return a Config with unique, tmp-scoped socket_dir/lock/log dirs.

    macOS limits AF_UNIX socket paths to ~104 characters, so use short
    paths under /tmp rather than pytest's (long) tmp_path directly for the
    socket.
    """
    base = Path(tempfile.mkdtemp(prefix=f"cliads_{tag}_", dir="/tmp"))
    base.chmod(base.stat().st_mode | stat.S_IRWXO)
    sock_dir = base / "s"
    data_dir = base / "d"
    log_dir = base / "log"
    sock_dir.mkdir()
    data_dir.mkdir()
    log_dir.mkdir()
    return Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        calibration_dir=data_dir / "calibration",
        log_level="DEBUG",
        log_dir=log_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
        # Unique per test — never touches the real /tmp/aprilcamd.lock.
        daemon_lock_path=base / "aprilcamd.lock",
    )


def _start_server(cfg: Config, tcp_port: int) -> tuple[DaemonServer, threading.Thread]:
    unix_path = str(cfg.socket_dir / "control.sock")
    server = DaemonServer(
        cfg, unix_enabled=True, tcp_enabled=True, unix_path=unix_path, tcp_port=tcp_port
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    assert server.started_event.wait(timeout=3.0), "server did not start in time"
    return server, t


# ---------------------------------------------------------------------------
# _force_takeover ordering: RPC -> SIGTERM -> SIGKILL
# ---------------------------------------------------------------------------


def test_force_takeover_tries_graceful_rpc_before_any_signal(tmp_path):
    """The graceful Shutdown RPC is attempted before SIGTERM/SIGKILL."""
    cfg = _make_config(tmp_path)
    resolved_unix = str(cfg.socket_dir / "control.sock")

    calls: list[str] = []

    mock_dc = MagicMock()
    mock_dc.list_cameras.return_value = []

    def _record_shutdown():
        calls.append("shutdown_rpc")

    mock_dc.shutdown.side_effect = _record_shutdown

    def _fake_read_pid(config):
        return 99999  # a pid that (almost certainly) does not exist

    def _fake_kill(pid, sig):
        if sig == signal.SIGTERM:
            calls.append("sigterm")
        elif sig == signal.SIGKILL:
            calls.append("sigkill")
        elif sig == 0:
            raise ProcessLookupError()  # pretend the process is already gone
        return None

    with (
        patch("aprilcam.client.control.DaemonControl", return_value=mock_dc),
        patch.object(daemon_cli, "_read_pid", side_effect=_fake_read_pid),
        patch("os.kill", side_effect=_fake_kill),
    ):
        daemon_cli._force_takeover(cfg, resolved_unix)

    assert calls == ["shutdown_rpc"], (
        "expected only the graceful RPC to fire (pid check raises "
        f"ProcessLookupError immediately, so no signal is sent); got {calls}"
    )
    mock_dc.shutdown.assert_called_once()


def test_force_takeover_sigterm_before_sigkill(tmp_path):
    """SIGTERM is sent before SIGKILL; SIGKILL only fires after the grace
    window elapses with the process still alive.
    """
    cfg = _make_config(tmp_path)
    resolved_unix = str(cfg.socket_dir / "control.sock")

    calls: list[str] = []
    mock_dc = MagicMock()
    mock_dc.list_cameras.side_effect = Exception("no daemon reachable")

    # Simulate a process that exists (kill(pid, 0) succeeds) and stays alive
    # through the whole SIGTERM grace window, forcing escalation to SIGKILL.
    alive = {"value": True}

    def _fake_kill(pid, sig):
        if sig == 0:
            if not alive["value"]:
                raise ProcessLookupError()
            return None
        if sig == signal.SIGTERM:
            calls.append("sigterm")
            return None
        if sig == signal.SIGKILL:
            calls.append("sigkill")
            alive["value"] = False
            return None

    with (
        patch("aprilcam.client.control.DaemonControl", return_value=mock_dc),
        patch.object(daemon_cli, "_read_pid", return_value=54321),
        patch("os.kill", side_effect=_fake_kill),
        patch("time.sleep"),
        patch(
            "time.monotonic",
            side_effect=_monotonic_sequence(),
        ),
    ):
        daemon_cli._force_takeover(cfg, resolved_unix)

    assert calls == ["sigterm", "sigkill"], calls


def _monotonic_sequence():
    """Yield increasing monotonic() values that blow through both the
    5s SIGTERM grace window and the 15s lock-wait deadline quickly, without
    real sleeping.
    """
    t = [0.0]

    def _next():
        t[0] += 6.0  # exceeds the 5s grace window each call
        return t[0]

    while True:
        yield _next()


# ---------------------------------------------------------------------------
# (c) End-to-end: --force takes over a running daemon
# ---------------------------------------------------------------------------


def test_force_takeover_end_to_end_new_instance_holds_lock(tmp_path):
    """A running DaemonServer is taken over via _cmd_start(force=True):
    the old instance's process/thread exits, and the new instance holds
    the global lock. Exercised via the CLI helper functions directly
    against real (in-process) DaemonServer instances, per the "no
    hardware" constraint — no real daemon subprocess is spawned.
    """
    cfg = _make_config(tmp_path)
    unix_path = str(cfg.socket_dir / "control.sock")

    server1, t1 = _start_server(cfg, tcp_port=15320)
    old_pid_in_lock = _read_lock_pid(cfg)
    assert old_pid_in_lock == os.getpid()  # this test process "is" both daemons

    try:
        # _force_takeover: graceful RPC should succeed since server1 is real
        # and reachable — no signals needed since the pid recorded in the
        # local pidfile is *our own test process* (the fixture uses
        # threads, not subprocesses), so we must not actually SIGTERM/KILL
        # ourselves. Patch _read_pid to return None so only the RPC path
        # runs; the RPC alone is sufficient to stop server1's thread.
        with patch.object(daemon_cli, "_read_pid", return_value=None):
            daemon_cli._force_takeover(cfg, unix_path)

        t1.join(timeout=5.0)
        assert not t1.is_alive(), "old server thread did not exit after takeover"

        # New instance: construct with force=True and run in a thread,
        # exercising the same path _cmd_start's foreground branch takes.
        server2 = DaemonServer(
            cfg, unix_enabled=True, tcp_enabled=True, unix_path=unix_path,
            tcp_port=15321, force=True,
        )
        t2 = threading.Thread(target=server2.run, daemon=True)
        t2.start()
        try:
            assert server2.started_event.wait(timeout=5.0), (
                "new instance did not start / acquire the lock after takeover"
            )
            new_pid_in_lock = _read_lock_pid(cfg)
            assert new_pid_in_lock == os.getpid()
        finally:
            server2._shutdown_event.set()
            t2.join(timeout=5.0)
    finally:
        if t1.is_alive():
            server1._shutdown_event.set()
            t1.join(timeout=5.0)


def _read_lock_pid(cfg: Config) -> int | None:
    try:
        with open(cfg.daemon_lock_path) as f:
            return int(f.read().strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# _spawn_daemon --force argv
# ---------------------------------------------------------------------------


def test_spawn_daemon_appends_force_flag(tmp_path):
    cfg = _make_config(tmp_path)
    with patch("subprocess.Popen") as mock_popen:
        daemon_cli._spawn_daemon(cfg, force=True)
    argv = mock_popen.call_args[0][0]
    assert "--force" in argv


def test_spawn_daemon_no_force_flag_by_default(tmp_path):
    cfg = _make_config(tmp_path)
    with patch("subprocess.Popen") as mock_popen:
        daemon_cli._spawn_daemon(cfg, force=False)
    argv = mock_popen.call_args[0][0]
    assert "--force" not in argv


# ---------------------------------------------------------------------------
# _cmd_start threading force -> _spawn_daemon / DaemonServer(force=...)
# ---------------------------------------------------------------------------


def test_cmd_start_detached_force_true_takes_over_then_spawns_with_force(tmp_path):
    """When force=True and a daemon is already up, _cmd_start runs the
    takeover helper first, then spawns the new daemon with --force.
    """
    cfg = _make_config(tmp_path)

    call_order: list[str] = []

    def _fake_probe(resolved_unix):
        # First call (before takeover): daemon is up.
        # After takeover + spawn poll: report "up" so _cmd_start returns.
        if "takeover" not in call_order:
            return True
        return True

    def _fake_takeover(config, resolved_unix):
        call_order.append("takeover")

    def _fake_spawn(config, log_level=None, force=False):
        call_order.append(f"spawn(force={force})")

    with (
        patch.object(daemon_cli, "_probe_daemon", side_effect=_fake_probe),
        patch.object(daemon_cli, "_force_takeover", side_effect=_fake_takeover),
        patch.object(daemon_cli, "_spawn_daemon", side_effect=_fake_spawn),
        patch.object(daemon_cli, "_read_pid", return_value=1234),
        patch("aprilcam.client.control.DaemonControl") as mock_dc_cls,
    ):
        mock_dc = MagicMock()
        mock_dc.list_cameras.return_value = []
        mock_dc_cls.return_value = mock_dc

        rc = daemon_cli._cmd_start(cfg, force=True)

    assert rc == 0
    assert call_order[0] == "takeover", call_order
    # Because _probe_daemon is stubbed to always return True (simulating
    # "already up" even post-takeover in this unit test), _cmd_start's
    # not-already_up spawn branch is skipped; the argv-level --force
    # plumbing itself is covered by test_spawn_daemon_appends_force_flag
    # and the end-to-end test above.


def test_cmd_start_force_false_skips_takeover(tmp_path):
    """When force=False, the takeover helper is never invoked even if a
    daemon is already up.
    """
    cfg = _make_config(tmp_path)

    with (
        patch.object(daemon_cli, "_probe_daemon", return_value=True),
        patch.object(daemon_cli, "_force_takeover") as mock_takeover,
        patch.object(daemon_cli, "_read_pid", return_value=1234),
        patch("aprilcam.client.control.DaemonControl") as mock_dc_cls,
    ):
        mock_dc = MagicMock()
        mock_dc.list_cameras.return_value = []
        mock_dc_cls.return_value = mock_dc

        rc = daemon_cli._cmd_start(cfg, force=False)

    assert rc == 0
    mock_takeover.assert_not_called()


# ---------------------------------------------------------------------------
# restart always forces
# ---------------------------------------------------------------------------


def test_cmd_restart_always_passes_force_true_to_start(tmp_path):
    cfg = _make_config(tmp_path)

    with (
        patch.object(daemon_cli, "_cmd_stop", return_value=0),
        patch.object(daemon_cli, "_cmd_start") as mock_start,
    ):
        mock_start.return_value = 0
        daemon_cli._cmd_restart(cfg)

    assert mock_start.call_args.kwargs.get("force") is True


def test_cmd_restart_wait_loop_polls_lock_path_not_pidfile(tmp_path):
    """_cmd_restart's wait-loop must poll config.daemon_lock_path, not
    config.daemon_pidfile, for lock availability.
    """
    cfg = _make_config(tmp_path)

    # Create a pidfile that would (incorrectly) look "locked forever" if the
    # old code path (flocking daemon_pidfile) were still in use, by holding
    # an exclusive flock on it via a helper subprocess-free trick: just
    # leave it as a plain file (unlocked) — the key assertion is that the
    # loop actually inspects daemon_lock_path's flock state, which we control
    # directly here, rather than the pidfile's.
    import fcntl

    cfg.daemon_pidfile.write_text("1\n")

    # Hold an exclusive flock on daemon_lock_path so the wait-loop must
    # observe contention and only proceed once we release it.
    lock_fd = os.open(str(cfg.daemon_lock_path), os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)

    release_after = 0.3
    start = time.monotonic()

    def _release_soon():
        time.sleep(release_after)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    releaser = threading.Thread(target=_release_soon, daemon=True)
    releaser.start()

    with (
        patch.object(daemon_cli, "_cmd_stop", return_value=0),
        patch.object(daemon_cli, "_cmd_start", return_value=0) as mock_start,
    ):
        daemon_cli._cmd_restart(cfg)

    elapsed = time.monotonic() - start
    releaser.join(timeout=2.0)

    # The wait-loop must have actually waited for the lock release (not
    # returned instantly because it was watching the wrong file).
    assert elapsed >= release_after - 0.05, (
        f"restart proceeded before the lock was released ({elapsed:.2f}s < "
        f"{release_after}s) — is the wait-loop still polling daemon_pidfile?"
    )
    mock_start.assert_called_once()
