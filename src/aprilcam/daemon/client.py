"""
aprilcam.daemon.client — ControlClient and ensure_running().

ControlClient wraps a connected UNIX socket and exposes a single
``rpc()`` method that sends a newline-delimited JSON request and
returns the parsed JSON response dict.

``ensure_running()`` connects to the daemon's control socket, spawning
a fresh daemon process if one is not already listening.  A spawn-lock
file prevents a race condition where two callers start the daemon
simultaneously.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..config import Config


# ---------------------------------------------------------------------------
# ControlClient
# ---------------------------------------------------------------------------


class ControlClient:
    """RPC client for the aprilcamd control socket.

    Opens a fresh connection for each RPC call because the daemon uses a
    one-request-per-connection protocol (reads one JSON line, writes one
    JSON line, closes).

    Intended use::

        with ensure_running(config) as client:
            result = client.rpc("list_cameras")
    """

    def __init__(self, control_path: Path) -> None:
        self._path = control_path

    def rpc(self, cmd: str, **kwargs) -> dict:
        """Send ``cmd`` with optional keyword arguments, return the response dict.

        Opens a fresh connection, sends one request, reads one response,
        closes.  Raises :class:`RuntimeError` when the server returns
        ``ok: False``.
        """
        request = {"cmd": cmd, **kwargs}
        payload = (json.dumps(request) + "\n").encode("utf-8")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self._path))
            sock.sendall(payload)
            f = sock.makefile("rb")
            line = f.readline()
            f.close()
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if not line:
            raise RuntimeError("Connection closed before receiving a response")

        response: dict = json.loads(line.decode("utf-8"))
        if not response.get("ok", False):
            raise RuntimeError(response.get("error", "unknown error"))
        return response

    def close(self) -> None:
        """No-op — connections are closed after each RPC."""

    def __enter__(self) -> "ControlClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_connect(path: Path) -> Optional[socket.socket]:
    """Try to connect to the UNIX socket at *path*.

    Returns the connected socket on success, or ``None`` on any failure
    (cleans up the socket in that case).
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# ensure_running
# ---------------------------------------------------------------------------


def ensure_running(config: Config, log_level: str | None = None) -> ControlClient:
    """Return a connected :class:`ControlClient`, starting the daemon if needed.

    Algorithm:
    1. Attempt an immediate connection to the control socket.
    2. If connected, return immediately.
    3. Acquire the spawn lock (exclusive flock) so only one caller spawns.
    4. Re-check the socket (another caller may have already spawned the daemon).
    5. If still not running, spawn ``python -m aprilcam.daemon`` as a
       detached background process.
    6. Poll the socket every 50 ms for up to 5 seconds; raise on timeout.
    7. Release the spawn lock and return the connected client.

    *log_level* overrides ``APRILCAM_LOG_LEVEL`` for the spawned process.
    """
    control_path = config.socket_dir / "control.sock"

    # Step 1 & 2: fast path — daemon already running
    if _try_connect(control_path) is not None:
        return ControlClient(control_path)

    # Step 3: acquire spawn lock
    lock_path = config.socket_dir / "aprilcamd.spawn.lock"
    config.socket_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")  # noqa: WPS515  (kept open intentionally)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        # Step 4: re-check now that we hold the lock
        if _try_connect(control_path) is not None:
            return ControlClient(control_path)

        # Step 5: spawn the daemon
        config.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(config.log_dir / "aprilcamd.log", "a")  # noqa: WPS515
        env = os.environ.copy()
        if log_level:
            env["APRILCAM_LOG_LEVEL"] = log_level
        subprocess.Popen(
            [sys.executable, "-m", "aprilcam.daemon"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=env,
        )

        # Step 6: poll until the daemon is ready
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if _try_connect(control_path) is not None:
                return ControlClient(control_path)

        raise RuntimeError("aprilcamd did not start within 5 seconds")

    finally:
        # Step 7: release the spawn lock
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
