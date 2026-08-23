"""Unit tests for ensure_running() spawn-guard logic (T009).

These tests verify:
  - When two callers race, subprocess.Popen is called exactly once.
  - When the daemon never starts, ensure_running() raises RuntimeError.

No real daemon processes are started.  All subprocess and socket
connection calls are mocked.
"""

from __future__ import annotations

import fcntl
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.config import Config
from aprilcam.daemon import client as client_module
from aprilcam.daemon.client import ensure_running


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> Config:
    socket_dir = tmp_path / "sockets"
    socket_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        data_dir=tmp_path / "data",
        socket_dir=socket_dir,
        calibration_dir=tmp_path / "calibration",
        log_level="INFO",
        daemon_pidfile=tmp_path / "aprilcamd.pid",
    )


# ---------------------------------------------------------------------------
# Spawn-once guard test
# ---------------------------------------------------------------------------


def test_spawn_called_once_on_race(tmp_path):
    """Two concurrent ensure_running() calls must spawn the daemon exactly once.

    Strategy:
      - _try_connect returns None for the first 3 calls (two fast-path checks
        + one re-check inside the lock for the first lock-holder), then returns
        a mock socket so all subsequent calls (second thread's re-check,
        polling) succeed.
      - subprocess.Popen is mocked to do nothing.
      - Two threads call ensure_running() concurrently.
      - Assert Popen called exactly once across both threads.

    Call sequence (approximate — thread ordering is non-deterministic):
      call 0: T1 fast-path    → None
      call 1: T2 fast-path    → None
      call 2: T1 re-check (in lock, no daemon yet) → None  → Popen fires
      call 3: T2 re-check (in lock, daemon "up")   → mock_sock → no Popen
      call 4+: T1 poll loop                         → mock_sock
    """
    config = _make_config(tmp_path)

    mock_sock = MagicMock()
    _call_count = [0]
    _count_lock = threading.Lock()

    def fake_try_connect(path: Path):
        with _count_lock:
            n = _call_count[0]
            _call_count[0] += 1
        # First 4 calls: None
        #   - up to 2 fast-path checks (one per thread)
        #   - up to 2 re-checks in lock (one per thread)
        # Everything from call 4 onwards: mock_sock (daemon is "up" for polling)
        return None if n < 4 else mock_sock

    errors: list = []
    clients: list = []

    def _run():
        try:
            c = ensure_running(config)
            clients.append(c)
        except Exception as exc:
            errors.append(exc)

    with (
        patch.object(client_module, "_try_connect", side_effect=fake_try_connect),
        patch("subprocess.Popen") as mock_popen,
        patch("time.sleep"),  # skip real sleeps in polling loop
    ):
        t1 = threading.Thread(target=_run, daemon=True)
        t2 = threading.Thread(target=_run, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

    assert not errors, f"ensure_running raised: {errors}"
    assert mock_popen.call_count == 1, (
        f"Expected Popen called once, got {mock_popen.call_count}"
    )


# ---------------------------------------------------------------------------
# Timeout test
# ---------------------------------------------------------------------------


def test_raises_on_timeout(tmp_path):
    """ensure_running() raises RuntimeError when daemon never starts.

    We mock _try_connect to always return None and patch time.monotonic
    so the deadline expires immediately after Popen is called.
    """
    config = _make_config(tmp_path)

    # Make monotonic advance past the deadline on every call after the first
    _mono_calls = [0]
    _start = time.monotonic()

    def fake_monotonic():
        _mono_calls[0] += 1
        # First call (deadline = now + 5.0): return normal time
        # All subsequent calls: return a time well past the deadline
        if _mono_calls[0] <= 1:
            return _start
        return _start + 10.0  # well past 5s deadline

    with (
        patch.object(client_module, "_try_connect", return_value=None),
        patch("subprocess.Popen"),
        patch("time.sleep"),
        patch("time.monotonic", side_effect=fake_monotonic),
    ):
        with pytest.raises(RuntimeError, match="did not start within"):
            ensure_running(config)


# ---------------------------------------------------------------------------
# Fast-path test (daemon already running)
# ---------------------------------------------------------------------------


def test_fast_path_skips_spawn(tmp_path):
    """ensure_running() returns immediately without spawning if already running."""
    config = _make_config(tmp_path)
    mock_sock = MagicMock()

    with (
        patch.object(client_module, "_try_connect", return_value=mock_sock),
        patch("subprocess.Popen") as mock_popen,
    ):
        client = ensure_running(config)

    assert mock_popen.call_count == 0
    assert client is not None
