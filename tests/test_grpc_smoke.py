"""Integration smoke test for the gRPC daemon stack.

Starts a real DaemonServer in a background thread using a Unix socket at a
temp path.  Exercises the end-to-end protocol roundtrip via DaemonControl
without requiring real camera hardware.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.client.control import DaemonControl
from aprilcam.config import Config
from aprilcam.daemon.server import DaemonServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_tmp() -> Path:
    """Return a short /tmp path to stay within Unix socket path limits (104 chars)."""
    d = Path(tempfile.mkdtemp(prefix="acsmk_", dir="/tmp"))
    return d


def _make_config(base: Path) -> tuple[Config, str]:
    """Build a Config and return (config, unix_sock_path_str)."""
    sock_dir = base / "s"
    data_dir = base / "d"
    sock_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    unix_sock_path = str(sock_dir / "ctrl.sock")

    config = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )
    return config, unix_sock_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_cameras_via_daemon_control() -> None:
    """list_cameras() returns an empty list via the full gRPC stack."""
    import shutil

    base = _short_tmp()
    try:
        config, unix_sock_path = _make_config(base)

        server = DaemonServer(
            config,
            unix_enabled=True,
            tcp_enabled=False,
            unix_path=unix_sock_path,
        )

        # Run the server in a background thread
        t = threading.Thread(target=server.run, daemon=True)
        t.start()

        # Wait for the server to be ready
        assert server.started_event.wait(timeout=5.0), "Server did not start in time"

        try:
            dc = DaemonControl(unix_path=unix_sock_path)
            dc.connect()
            try:
                cameras = dc.list_cameras()
                assert cameras == []
            finally:
                dc.close()
        finally:
            # Trigger shutdown via the event directly (avoids needing the RPC)
            server._shutdown_event.set()
            t.join(timeout=10.0)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_shutdown_via_daemon_control() -> None:
    """DaemonControl.shutdown() causes the server to terminate cleanly."""
    import shutil

    base = _short_tmp()
    try:
        config, unix_sock_path = _make_config(base)

        server = DaemonServer(
            config,
            unix_enabled=True,
            tcp_enabled=False,
            unix_path=unix_sock_path,
        )

        t = threading.Thread(target=server.run, daemon=True)
        t.start()

        assert server.started_event.wait(timeout=5.0), "Server did not start in time"

        with DaemonControl(unix_path=unix_sock_path) as dc:
            dc.shutdown()

        t.join(timeout=10.0)
        assert not t.is_alive(), "Server thread did not terminate after shutdown"
    finally:
        shutil.rmtree(base, ignore_errors=True)
