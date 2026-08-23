"""Tests for aprilcam.daemon.server — DaemonServer gRPC startup and locking."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path

import grpc
import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.config import Config
from aprilcam.daemon.server import DaemonServer
from aprilcam.errors import DaemonAlreadyRunningError, DaemonBindError, DaemonPortInUseError
from aprilcam.proto import aprilcam_pb2, aprilcam_pb2_grpc


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_config(tmp_path: Path):
    """Return a Config backed by /tmp directories with short paths.

    macOS limits AF_UNIX socket paths to ~104 characters.  Use short paths
    under /tmp to stay within that limit.
    """
    import stat
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="ads_", dir="/tmp"))
    base.chmod(base.stat().st_mode | stat.S_IRWXO)

    sock_dir = base / "s"
    data_dir = base / "d"
    sock_dir.mkdir()
    data_dir.mkdir()

    cfg = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        calibration_dir=data_dir / "calibration",
        log_level="DEBUG",
        daemon_pidfile=sock_dir / "aprilcamd.pid",
        # Never touch the real /tmp/aprilcamd.lock — unique per test.
        daemon_lock_path=base / "aprilcamd.lock",
    )
    yield cfg

    import shutil
    shutil.rmtree(base, ignore_errors=True)


def _make_server(cfg: Config, *, unix_enabled=True, tcp_enabled=True,
                 unix_path: str | None = None, tcp_port: int = 15280) -> DaemonServer:
    """Build a DaemonServer pointed at a unique TCP port to avoid conflicts."""
    kw = dict(unix_enabled=unix_enabled, tcp_enabled=tcp_enabled, tcp_port=tcp_port)
    if unix_path is not None:
        kw["unix_path"] = unix_path
    else:
        kw["unix_path"] = str(cfg.socket_dir / "control.sock")
    return DaemonServer(cfg, **kw)


@pytest.fixture()
def running_server(tmp_config: Config):
    """Start DaemonServer in a background thread; yield (server, config); then shut down."""
    unix_path = str(tmp_config.socket_dir / "control.sock")
    server = _make_server(tmp_config, unix_path=unix_path, tcp_port=15281)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    # Wait for the gRPC server to be ready (up to 3 s)
    assert server.started_event.wait(timeout=3.0), "DaemonServer did not start in time"

    yield server, tmp_config

    # Trigger shutdown and wait for the thread to finish
    server._shutdown_event.set()
    t.join(timeout=5.0)


# ── Constructor validation ─────────────────────────────────────────────────────


def test_both_transports_disabled_raises():
    """DaemonServer raises ValueError when both unix_enabled and tcp_enabled are False."""
    from aprilcam.config import Config

    cfg = Config()
    with pytest.raises(ValueError, match="at least one transport"):
        DaemonServer(cfg, unix_enabled=False, tcp_enabled=False)


# ── Startup and readiness ─────────────────────────────────────────────────────


def test_server_starts_and_sets_started_event(running_server):
    """DaemonServer sets started_event once the gRPC server is accepting."""
    server, _ = running_server
    assert server.started_event.is_set()


def test_server_binds_unix_socket(running_server):
    """The Unix socket path exists after the server starts."""
    server, cfg = running_server
    ctrl_path = cfg.socket_dir / "control.sock"
    assert ctrl_path.exists()


def test_grpc_list_cameras_empty(running_server):
    """ListCameras via gRPC returns an empty list when no cameras are open."""
    _, cfg = running_server
    ctrl_path = str(cfg.socket_dir / "control.sock")

    with grpc.insecure_channel(f"unix:{ctrl_path}") as channel:
        stub = aprilcam_pb2_grpc.AprilCamStub(channel)
        response = stub.ListCameras(aprilcam_pb2.Empty(), timeout=5)

    assert list(response.cameras) == []


def test_grpc_close_unknown_camera(running_server):
    """CloseCamera for an unknown camera returns NOT_FOUND status."""
    _, cfg = running_server
    ctrl_path = str(cfg.socket_dir / "control.sock")

    with grpc.insecure_channel(f"unix:{ctrl_path}") as channel:
        stub = aprilcam_pb2_grpc.AprilCamStub(channel)
        try:
            stub.CloseCamera(
                aprilcam_pb2.CameraRequest(cam_name="cam_99"), timeout=5
            )
            pytest.fail("Expected gRPC NOT_FOUND error")
        except grpc.RpcError as exc:
            assert exc.code() == grpc.StatusCode.NOT_FOUND


def test_grpc_shutdown_rpc(tmp_config: Config):
    """The Shutdown RPC causes the daemon to exit cleanly."""
    unix_path = str(tmp_config.socket_dir / "ctrl.sock")
    server = DaemonServer(
        tmp_config,
        unix_enabled=True,
        tcp_enabled=False,
        unix_path=unix_path,
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    assert server.started_event.wait(timeout=3.0), "Server did not start"

    with grpc.insecure_channel(f"unix:{unix_path}") as channel:
        stub = aprilcam_pb2_grpc.AprilCamStub(channel)
        stub.Shutdown(aprilcam_pb2.Empty(), timeout=5)

    t.join(timeout=5.0)
    assert not t.is_alive(), "Server thread did not exit after Shutdown RPC"


# ── Machine-global lock ───────────────────────────────────────────────────────


def test_global_lock_prevents_duplicate_different_socket_dir(tmp_path: Path):
    """(a) A second DaemonServer with a DIFFERENT socket_dir but the SAME
    daemon_lock_path as a running first instance is refused.

    This reproduces and closes the confirmed today's-gap scenario: two
    daemons with different socket_dir values used to both flock their own
    (different) pidfile successfully and coexist.
    """
    import stat
    import tempfile

    shared_lock = Path(tempfile.mkdtemp(prefix="lock_", dir="/tmp")) / "aprilcamd.lock"

    def _cfg(suffix: str) -> Config:
        base = Path(tempfile.mkdtemp(prefix=f"ads_{suffix}_", dir="/tmp"))
        base.chmod(base.stat().st_mode | stat.S_IRWXO)
        sock_dir = base / "s"
        data_dir = base / "d"
        sock_dir.mkdir()
        data_dir.mkdir()
        return Config(
            data_dir=data_dir,
            socket_dir=sock_dir,
            calibration_dir=data_dir / "calibration",
            log_level="DEBUG",
            daemon_pidfile=sock_dir / "aprilcamd.pid",
            daemon_lock_path=shared_lock,
        )

    cfg1 = _cfg("a")
    cfg2 = _cfg("b")  # different socket_dir entirely

    server1 = _make_server(
        cfg1, unix_path=str(cfg1.socket_dir / "control.sock"), tcp_port=15290
    )
    t1 = threading.Thread(target=server1.run, daemon=True)
    t1.start()
    try:
        assert server1.started_event.wait(timeout=3.0), "server1 did not start"

        server2 = _make_server(
            cfg2, unix_path=str(cfg2.socket_dir / "control.sock"), tcp_port=15291
        )
        with pytest.raises(DaemonAlreadyRunningError):
            server2.run()
    finally:
        server1._shutdown_event.set()
        t1.join(timeout=5.0)

    import shutil

    shutil.rmtree(cfg1.data_dir.parent, ignore_errors=True)
    shutil.rmtree(cfg2.data_dir.parent, ignore_errors=True)
    shutil.rmtree(shared_lock.parent, ignore_errors=True)


def test_global_lock_refuse_by_default_carries_pid(tmp_config: Config):
    """(d) Refuse-by-default: run() raises DaemonAlreadyRunningError carrying
    the running daemon's pid when the lock is held and force=False.
    """
    unix_path = str(tmp_config.socket_dir / "ctrl.sock")
    server1 = _make_server(tmp_config, unix_path=unix_path, tcp_port=15292)
    t1 = threading.Thread(target=server1.run, daemon=True)
    t1.start()
    try:
        assert server1.started_event.wait(timeout=3.0), "server1 did not start"

        server2 = _make_server(
            tmp_config, unix_path=unix_path + "2", tcp_port=15293
        )
        with pytest.raises(DaemonAlreadyRunningError) as exc_info:
            server2.run()

        assert exc_info.value.existing_pid == os.getpid()
    finally:
        server1._shutdown_event.set()
        t1.join(timeout=5.0)


def _hold_lock_forever(lock_path: str) -> None:
    """Helper for a subprocess: open+flock the lock file, write our pid,
    then block until killed.  Used to simulate a stale lock via SIGKILL.
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    os.fchmod(fd, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    while True:
        time.sleep(1)


def test_stale_lock_reclaimed_after_kill9(tmp_config: Config):
    """(e) A stale lock file left behind by a process killed via SIGKILL is
    reclaimed cleanly by the next run() without unlinking the file and
    without raising.
    """
    lock_path = str(tmp_config.daemon_lock_path)

    proc = multiprocessing.Process(target=_hold_lock_forever, args=(lock_path,))
    proc.start()
    try:
        # Wait for the subprocess to actually hold the lock.
        deadline = time.monotonic() + 5.0
        held = False
        while time.monotonic() < deadline:
            if os.path.exists(lock_path) and os.path.getsize(lock_path) > 0:
                held = True
                break
            time.sleep(0.05)
        assert held, "subprocess did not acquire the lock in time"
        time.sleep(0.2)  # let the write settle

        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=5.0)
        assert not proc.is_alive()

        # The lock file must still exist (never unlinked) but its flock is
        # now released by the kernel since the holder is dead.
        assert os.path.exists(lock_path)

        unix_path = str(tmp_config.socket_dir / "ctrl.sock")
        server = _make_server(tmp_config, unix_path=unix_path, tcp_port=15294)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        try:
            assert server.started_event.wait(timeout=3.0), (
                "server did not reclaim the stale lock in time"
            )
            with open(lock_path) as f:
                pid_in_lock = int(f.read().strip())
            assert pid_in_lock == os.getpid()
        finally:
            server._shutdown_event.set()
            t.join(timeout=5.0)
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)


def test_global_lock_not_unlinked_on_shutdown_and_restart(tmp_config: Config):
    """The lock file is never unlinked by _shutdown()/the release path —
    start, gracefully stop, and restart a DaemonServer reusing the same
    lock path; the second run() succeeds without needing lock-file
    recreation.
    """
    lock_path = tmp_config.daemon_lock_path

    unix_path = str(tmp_config.socket_dir / "ctrl.sock")
    server1 = _make_server(tmp_config, unix_path=unix_path, tcp_port=15295)
    t1 = threading.Thread(target=server1.run, daemon=True)
    t1.start()
    assert server1.started_event.wait(timeout=3.0), "server1 did not start"

    inode_before = os.stat(lock_path).st_ino

    server1._shutdown_event.set()
    t1.join(timeout=5.0)
    assert not t1.is_alive()

    # Lock file must still exist — never unlinked.
    assert lock_path.exists()
    assert os.stat(lock_path).st_ino == inode_before

    server2 = _make_server(tmp_config, unix_path=unix_path, tcp_port=15296)
    t2 = threading.Thread(target=server2.run, daemon=True)
    t2.start()
    try:
        assert server2.started_event.wait(timeout=3.0), (
            "server2 did not restart and reacquire the lock"
        )
        # Still the same inode — reused, not recreated.
        assert os.stat(lock_path).st_ino == inode_before
    finally:
        server2._shutdown_event.set()
        t2.join(timeout=5.0)


def test_local_pidfile_still_written_after_global_lock(tmp_config: Config):
    """The local daemon_pidfile is still written after global-lock
    acquisition, unchanged in format/location from today.
    """
    unix_path = str(tmp_config.socket_dir / "ctrl.sock")
    server = _make_server(tmp_config, unix_path=unix_path, tcp_port=15297)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        assert server.started_event.wait(timeout=3.0), "server did not start"
        assert tmp_config.daemon_pidfile.exists()
        with open(tmp_config.daemon_pidfile) as f:
            pid = int(f.read().strip())
        assert pid == os.getpid()
    finally:
        server._shutdown_event.set()
        t.join(timeout=5.0)


# ── __main__ argument parsing ─────────────────────────────────────────────────


def test_main_parse_args_defaults():
    """Default args have both transports enabled and standard port.

    ``unix_path`` defaults to ``None`` so that ``main()`` can resolve it from
    ``config.socket_dir`` at runtime (the daemon, its client probe, and the
    pidfile must all agree on the socket path — see daemon/__main__.py).
    """
    from aprilcam.daemon.__main__ import _parse_args
    from aprilcam.daemon.server import _DEFAULT_TCP_PORT

    args = _parse_args([])
    assert args.unix_enabled is True
    assert args.tcp_enabled is True
    assert args.tcp_port == _DEFAULT_TCP_PORT
    assert args.unix_path is None


def test_main_parse_args_no_unix():
    """--no-unix disables the unix transport."""
    from aprilcam.daemon.__main__ import _parse_args

    args = _parse_args(["--no-unix"])
    assert args.unix_enabled is False
    assert args.tcp_enabled is True


def test_main_parse_args_no_tcp():
    """--no-tcp disables the tcp transport."""
    from aprilcam.daemon.__main__ import _parse_args

    args = _parse_args(["--no-tcp"])
    assert args.tcp_enabled is False
    assert args.unix_enabled is True


def test_main_parse_args_custom_port_and_path():
    """--tcp-port and --unix-path override the defaults."""
    from aprilcam.daemon.__main__ import _parse_args

    args = _parse_args(["--tcp-port", "9999", "--unix-path", "/tmp/my.sock"])
    assert args.tcp_port == 9999
    assert args.unix_path == "/tmp/my.sock"


def test_main_both_disabled_exits(capsys):
    """--no-unix --no-tcp causes sys.exit(1) with a clear error message."""
    from aprilcam.daemon.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--no-unix", "--no-tcp"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "at least one transport" in captured.err


def test_main_parse_args_force_default_false():
    """--force defaults to False."""
    from aprilcam.daemon.__main__ import _parse_args

    args = _parse_args([])
    assert args.force is False


def test_main_parse_args_force_flag():
    """--force is a valid, parseable flag."""
    from aprilcam.daemon.__main__ import _parse_args

    args = _parse_args(["--force"])
    assert args.force is True


# ── __main__ refusal exit code (018-004) ───────────────────────────────────────


def test_main_exits_3_on_already_running(monkeypatch, capsys, tmp_path):
    """main() catches DaemonAlreadyRunningError, prints the exact message,
    and exits with code 3 — not the old silent exit 0.
    """
    from aprilcam.daemon import __main__ as main_mod

    def _fake_run(self):
        raise DaemonAlreadyRunningError(existing_pid=4321)

    monkeypatch.setattr(main_mod.DaemonServer, "run", _fake_run)
    monkeypatch.setattr(
        main_mod.Config, "load", classmethod(lambda cls: _fake_config(tmp_path))
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main([])

    assert exc_info.value.code == 3
    captured = capsys.readouterr()
    assert "aprilcamd already running (pid 4321)" in captured.err


def test_main_exits_3_on_port_in_use(monkeypatch, capsys, tmp_path):
    """main() catches DaemonPortInUseError with a clear message and exit 3."""
    from aprilcam.daemon import __main__ as main_mod

    def _fake_run(self):
        raise DaemonPortInUseError("port 5280 already bound")

    monkeypatch.setattr(main_mod.DaemonServer, "run", _fake_run)
    monkeypatch.setattr(
        main_mod.Config, "load", classmethod(lambda cls: _fake_config(tmp_path))
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main([])

    assert exc_info.value.code == 3
    captured = capsys.readouterr()
    assert "port" in captured.err.lower()


def test_main_exits_3_on_bind_error(monkeypatch, capsys, tmp_path):
    """main() catches DaemonBindError with a clear message and exit 3."""
    from aprilcam.daemon import __main__ as main_mod

    def _fake_run(self):
        raise DaemonBindError("unix socket bind failed")

    monkeypatch.setattr(main_mod.DaemonServer, "run", _fake_run)
    monkeypatch.setattr(
        main_mod.Config, "load", classmethod(lambda cls: _fake_config(tmp_path))
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main([])

    assert exc_info.value.code == 3
    captured = capsys.readouterr()
    assert "unix socket" in captured.err.lower()


def _fake_config(tmp_path: Path) -> Config:
    """Minimal Config for __main__ refusal-path tests (never touches real
    /tmp/aprilcamd.lock or binds real port 5280 — main()'s DaemonServer.run
    is monkeypatched before any of that would happen).
    """
    import stat
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="main_ads_", dir="/tmp"))
    base.chmod(base.stat().st_mode | stat.S_IRWXO)
    sock_dir = base / "s"
    data_dir = base / "d"
    sock_dir.mkdir()
    data_dir.mkdir()
    return Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        calibration_dir=data_dir / "calibration",
        log_level="DEBUG",
        daemon_pidfile=sock_dir / "aprilcamd.pid",
        daemon_lock_path=base / "aprilcamd.lock",
    )
