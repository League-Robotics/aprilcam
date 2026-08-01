"""Tests for ``aprilcam.client.discovery`` (ticket 020-005).

Fully headless and local-only, matching Architecture Decision 2 (no
mDNS this sprint): every test drives ``Discovery`` against a
``tmp_path``-derived ``socket_dir`` it never shares with a real running
daemon on the machine executing the suite. "A daemon is running" is
simulated by genuinely holding an ``flock`` on a lock file in *this*
test process on a separate file descriptor from the one ``Discovery``
probes with -- ``fcntl.flock`` locks are per open-file-description, not
per-process, so two independent ``open()`` calls on the same path
really do contend, exactly as ``tests/aprilcam/daemon/test_lifecycle.
py`` documents for the server-side equivalent. "Responds" is simulated
with a bare ``grpc.server()`` bound to the unix socket -- connectivity
readiness does not require the ``AprilCamV2`` service to be registered,
so this needs no camera/cv2 machinery at all.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
from concurrent import futures
from pathlib import Path
from typing import Iterator, Tuple

import grpc
import pytest

from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.client.discovery import Discovery
from aprilcam.client.interfaces import DaemonInfo
from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import LOCK_FILENAME, SOCKET_FILENAME
from aprilcam.errors import ConnectionFailed, DiscoveryFailed


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests deeply enough to routinely exceed it (see
    ``tests/aprilcam/daemon/test_service.py``'s ``stub`` fixture for
    the same workaround)."""
    d = Path(tempfile.mkdtemp(prefix="ac-disc-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def directories(tmp_path: Path, short_socket_dir: Path, monkeypatch) -> Directories:
    """A ``Directories`` whose ``socket_dir`` is *short_socket_dir* --
    achieved by making the hard-coded system socket root
    (``/run/aprilcam``) always look unavailable, which forces
    resolution down to the ``XDG_RUNTIME_DIR`` env tier
    ``config/directories.py``'s module docstring calls out as
    monkeypatch-friendly for exactly this purpose."""
    monkeypatch.setattr("aprilcam.config.directories._writable_or_creatable", lambda path: False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_socket_dir))
    dirs = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    assert dirs.socket_dir == short_socket_dir / "aprilcam"
    return dirs


def _hold_lock(lock_path: Path) -> int:
    """Genuinely take the daemon lock, as if a daemon were running --
    returns the fd so the caller can release it. See the module
    docstring for why a second, independent ``open()`` in this same
    process still contends with this one."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _bind_bare_server(socket_path: Path) -> grpc.Server:
    """A ``grpc.Server`` bound to *socket_path* with no service
    registered -- connectivity becomes READY as soon as something is
    listening, regardless of which (if any) service answers, so this
    is sufficient to exercise "responds" without any daemon/camera
    machinery."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_insecure_port(f"unix:{socket_path}")
    server.start()
    return server


# ---------------------------------------------------------------------------
# find_all() -- AC3 (empty vs. one entry, reachable vs. not)
# ---------------------------------------------------------------------------


def test_find_all_returns_empty_tuple_when_no_daemon_running(directories: Directories):
    discovery = Discovery(directories=directories)

    assert discovery.find_all() == ()


def test_find_all_lists_a_held_but_unreachable_daemon(directories: Directories):
    """A daemon that advertises (holds the lock) but does not answer is
    listed, not filtered out -- ``DaemonInfo.reachable`` says which."""
    lock_fd = _hold_lock(directories.socket_dir / LOCK_FILENAME)
    try:
        discovery = Discovery(directories=directories)

        results = discovery.find_all()

        assert results == (DaemonInfo(host="localhost", port=0, is_local=True, reachable=False),)
    finally:
        _release_lock(lock_fd)


def test_find_all_lists_a_reachable_local_daemon(directories: Directories):
    lock_fd = _hold_lock(directories.socket_dir / LOCK_FILENAME)
    server = _bind_bare_server(directories.socket_dir / SOCKET_FILENAME)
    try:
        discovery = Discovery(directories=directories)

        results = discovery.find_all()

        assert len(results) == 1
        assert results[0].is_local is True
        assert results[0].reachable is True
    finally:
        server.stop(None)
        _release_lock(lock_fd)


def test_find_all_raises_discovery_failed_distinctly_from_empty(directories: Directories):
    """Discovery *failing* (e.g. permission denied probing socket_dir)
    must never look like "found nothing" (UC-13.S-5)."""

    def _broken_lock_holder(_path: Path):
        raise PermissionError("simulated: cannot read socket_dir")

    discovery = Discovery(directories=directories, lock_holder=_broken_lock_holder)

    with pytest.raises(DiscoveryFailed):
        discovery.find_all()


# ---------------------------------------------------------------------------
# connect() -- AC1/AC2 (local unix socket, remote TCP failure)
# ---------------------------------------------------------------------------


def test_connect_none_connects_to_the_local_daemon_over_the_unix_socket(directories: Directories):
    server = _bind_bare_server(directories.socket_dir / SOCKET_FILENAME)
    try:
        discovery = Discovery(directories=directories, connect_timeout=2.0)

        daemon = discovery.connect(None)

        assert isinstance(daemon, GrpcDaemon)
    finally:
        server.stop(None)


def test_connect_none_raises_connection_failed_when_nothing_is_listening(directories: Directories):
    discovery = Discovery(directories=directories, connect_timeout=0.5)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect(None)

    assert excinfo.value.host == "localhost"


def test_connect_remote_host_raises_connection_failed_when_nothing_answers(directories: Directories):
    discovery = Discovery(directories=directories, connect_timeout=0.5)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect("127.0.0.1:1")

    # Distinct in type from a successful-but-empty discovery result --
    # and names the host that did not answer.
    assert excinfo.value.host == "127.0.0.1:1"
    assert excinfo.value.reason is not None


def test_connect_prefers_explicit_host_over_env_var(directories: Directories, monkeypatch):
    """Precedence: an explicit ``host`` argument beats
    ``APRILCAM_DAEMON_HOST`` (docs/design/configuration.md's cascade)."""
    monkeypatch.setenv("APRILCAM_DAEMON_HOST", "env-host-should-be-ignored:1")
    discovery = Discovery(directories=directories, connect_timeout=0.3)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect("127.0.0.1:1")

    assert excinfo.value.host == "127.0.0.1:1"


def test_connect_falls_back_to_env_var_when_host_is_none(directories: Directories, monkeypatch):
    monkeypatch.setenv("APRILCAM_DAEMON_HOST", "127.0.0.1:1")
    discovery = Discovery(directories=directories, connect_timeout=0.3)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect(None)

    assert excinfo.value.host == "127.0.0.1:1"
