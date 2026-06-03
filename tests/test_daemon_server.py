"""Tests for aprilcam.daemon.server — DaemonServer gRPC startup and pidfile."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import grpc
import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.config import Config
from aprilcam.daemon.server import DaemonServer
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


# ── Pidfile locking ───────────────────────────────────────────────────────────


def test_pidfile_lock_prevents_duplicate(tmp_config: Config, caplog):
    """A second DaemonServer with the same config cannot acquire the pidfile."""
    import logging

    unix_path = str(tmp_config.socket_dir / "ctrl.sock")
    server1 = DaemonServer(
        tmp_config, unix_enabled=True, tcp_enabled=False, unix_path=unix_path
    )
    t1 = threading.Thread(target=server1.run, daemon=True)
    t1.start()

    assert server1.started_event.wait(timeout=3.0), "server1 did not start"

    # server2 should detect "already running" and return without blocking
    with caplog.at_level(logging.ERROR, logger="aprilcam.daemon.server"):
        server2 = DaemonServer(
            tmp_config, unix_enabled=True, tcp_enabled=False, unix_path=unix_path + "2"
        )
        server2.run()  # should return quickly — pidfile is held by server1

    assert any(
        "already running" in record.message
        for record in caplog.records
    ), f"Expected 'already running' in log, got: {[r.message for r in caplog.records]}"

    # Shut down server1
    server1._shutdown_event.set()
    t1.join(timeout=5.0)


# ── __main__ argument parsing ─────────────────────────────────────────────────


def test_main_parse_args_defaults():
    """Default args have both transports enabled and standard port/path."""
    from aprilcam.daemon.__main__ import _parse_args
    from aprilcam.daemon.server import _DEFAULT_TCP_PORT, _DEFAULT_UNIX_PATH

    args = _parse_args([])
    assert args.unix_enabled is True
    assert args.tcp_enabled is True
    assert args.tcp_port == _DEFAULT_TCP_PORT
    assert args.unix_path == _DEFAULT_UNIX_PATH


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
