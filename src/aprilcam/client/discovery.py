"""``Discovery`` -- finding daemons and connecting to one, client side
(ticket 020-005).

**Architecture Decision 2 (sprint 020): mDNS is stubbed to local-only
this sprint.** ``find_all()`` returns **at most one** ``DaemonInfo`` --
the local daemon, probed via the well-known local lock/socket pair
(``daemon.lifecycle.LOCK_FILENAME``/``SOCKET_FILENAME`` under
``Directories.socket_dir``) -- never a real mDNS broadcast/browse. This
is deliberately *not* a stub that lies: it genuinely answers "is a
daemon running on this machine, and does it actually respond," which is
exactly what every one of this sprint's in-scope commands needs
(none of them call ``daemon list``, the one command that would need
real multi-daemon network discovery -- see the sprint architecture's
Decision 2 for the full reasoning). Converting
``archive/src/aprilcam/daemon/mdns.py`` to real advertise/browse is
deferred to the sprint that adds ``daemon list``.

Finding nothing is an **empty result, not an error** (UC-13.S-2): no
lock held means no daemon is running, full stop. Discovery **failing**
-- this module could not even complete its local probe, e.g. a
permission error reading ``socket_dir`` -- is reported distinctly, as
:class:`~aprilcam.errors.DiscoveryFailed` (UC-13.S-5's "empty list
would send someone hunting for a dead daemon instead of a network
problem," restated for the local-only case this sprint actually
implements).

``connect(host)`` never goes through discovery at all -- it is a
**direct connect**: a unix socket for local (``host is None``), plain
TCP for a named remote. This is what lets ``-d HOST`` work without any
discovery machinery. Precedence for an unspecified target follows
``docs/design/configuration.md``'s cascade: an explicit *host* argument
(the CLI's ``-d`` flag, or any other caller-supplied value) beats
``APRILCAM_DAEMON_HOST``, which beats the local default. There is no
"file" tier here this sprint -- no daemon-target configuration document
exists yet to read one from.
"""

from __future__ import annotations

import fcntl
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

import grpc

from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.client.interfaces import Discovery as _DiscoveryBase
from aprilcam.config.directories import Directories
from aprilcam.daemon.interfaces import Daemon
from aprilcam.daemon.lifecycle import LOCK_FILENAME, SOCKET_FILENAME
from aprilcam.errors import ConnectionFailed, DiscoveryFailed
from aprilcam.proto import aprilcam_v2_pb2_grpc

__all__ = [
    "APRILCAM_DAEMON_HOST_ENV",
    "DEFAULT_DAEMON_PORT",
    "Discovery",
    "LocalDaemonInfo",
    "probe_lock_holder",
    "probe_socket_reachable",
]

logger = logging.getLogger(__name__)

#: spec §4.1's TCP:5280 -- the default port for an explicitly named
#: remote host (``-d HOST`` with no ``:port`` suffix, or
#: ``APRILCAM_DAEMON_HOST`` likewise bare). No daemon this sprint
#: actually *listens* on TCP yet (``config/directories.py``'s Open
#: Question 2 resolution) -- this constant only matters to
#: :meth:`Discovery.connect` against an explicitly named host, tested
#: against a second local process bound to an alternate port standing
#: in for "remote" on this one dev machine.
DEFAULT_DAEMON_PORT = 5280

APRILCAM_DAEMON_HOST_ENV = "APRILCAM_DAEMON_HOST"

_DEFAULT_PROBE_TIMEOUT = 1.0
_DEFAULT_CONNECT_TIMEOUT = 5.0


def probe_lock_holder(lock_path: Path) -> Optional[str]:
    """Non-blocking read of *lock_path*: ``None`` if nothing holds it
    right now (including "the file, or its directory, does not exist
    yet" -- no daemon has ever started here), or the holder identity
    string otherwise, formatted exactly like
    ``daemon.lifecycle.DaemonLifecycle._read_holder`` (``"pid
    <N>"``) so a caller that turns this into
    :class:`~aprilcam.errors.AlreadyRunning` reports the same identity
    a server-detected contention would.

    Opens its *own* file descriptor and always releases whatever it
    acquires -- this function only ever *reads* the lock state, never
    takes it from a real daemon (mirrors the read-only half of
    ``_acquire_lock``/``_read_holder``; see that module's docstring for
    why separate ``open()`` calls on the same path never share a flock
    even within one process, which is exactly what makes this probe
    trustworthy against a same-process fake in tests).

    Raises ``OSError`` for anything other than a missing lock file --
    e.g. permission denied on ``socket_dir`` -- so callers can
    distinguish "definitely nothing running" from "could not tell."
    """
    if not lock_path.exists():
        return None
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.lseek(fd, 0, os.SEEK_SET)
            contents = os.read(fd, 64).decode().strip()
            return f"pid {contents}" if contents else "unknown holder"
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    finally:
        os.close(fd)


def probe_socket_reachable(socket_path: Path, timeout: float = _DEFAULT_PROBE_TIMEOUT) -> bool:
    """Whether a gRPC channel to the unix socket at *socket_path*
    becomes ready within *timeout* -- the "responds" half of
    ``DaemonInfo.reachable``: a daemon that advertises (holds the lock)
    but does not answer is listed, not filtered out, so this is a
    separate check from :func:`probe_lock_holder` rather than folded
    into it."""
    if not socket_path.exists():
        return False
    channel = grpc.insecure_channel(f"unix:{socket_path}")
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
        return True
    except grpc.FutureTimeoutError:
        return False
    finally:
        channel.close()


@dataclass(frozen=True)
class LocalDaemonInfo:
    """Concrete ``client.interfaces.DaemonInfo`` for the local-only
    probe :meth:`Discovery.find_all` performs this sprint."""

    host: str
    port: int
    is_local: bool
    reachable: bool


class Discovery(_DiscoveryBase):
    """Concrete ``client.interfaces.Discovery``. See the module
    docstring for Architecture Decision 2 and the connect precedence.

    Derives from ``client.interfaces.Discovery`` (a plain base class,
    not ``Protocol`` -- see ``daemon.interfaces``'s module docstring for
    the design rule).

    Parameters
    ----------
    directories:
        Resolves the local lock/socket paths (``directories.
        socket_dir``). Defaults to a fresh :class:`Directories` (the
        real system/XDG/fallback roots); tests pass one pointed at a
        ``tmp_path``.
    lock_holder / socket_reachable:
        Overridable local probes (default :func:`probe_lock_holder` /
        :func:`probe_socket_reachable`) so tests can simulate "held but
        unreachable" or "probing itself failed" without a real daemon
        process.
    connect_timeout:
        Seconds :meth:`connect` waits for a channel to become ready
        before raising :class:`~aprilcam.errors.ConnectionFailed`.
        Real callers keep the default; tests shrink it so an
        intentionally-unreachable target fails fast.
    """

    def __init__(
        self,
        *,
        directories: Optional[Directories] = None,
        lock_holder: Callable[[Path], Optional[str]] = probe_lock_holder,
        socket_reachable: Callable[[Path], bool] = probe_socket_reachable,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._directories = directories if directories is not None else Directories()
        self._lock_holder = lock_holder
        self._socket_reachable = socket_reachable
        self._connect_timeout = connect_timeout

    @property
    def directories(self) -> Directories:
        return self._directories

    @property
    def lock_path(self) -> Path:
        return self._directories.socket_dir / LOCK_FILENAME

    @property
    def socket_path(self) -> Path:
        return self._directories.socket_dir / SOCKET_FILENAME

    # -- client.interfaces.Discovery --------------------------------------

    def find_all(self) -> Tuple[LocalDaemonInfo, ...]:
        """See ``client.interfaces.Discovery.find_all``. See the module
        docstring for Architecture Decision 2: at most one entry, the
        local daemon, never a network probe."""
        try:
            holder = self._lock_holder(self.lock_path)
        except OSError as exc:
            raise DiscoveryFailed(f"could not probe the local daemon: {exc}") from exc
        if holder is None:
            return ()
        reachable = self._socket_reachable(self.socket_path)
        return (LocalDaemonInfo(host="localhost", port=0, is_local=True, reachable=reachable),)

    def connect(self, host: Optional[str] = None) -> Daemon:
        """See ``client.interfaces.Discovery.connect``. Direct connect,
        bypassing :meth:`find_all` entirely -- see the module docstring
        for the flag > env > default precedence applied to *host*."""
        resolved = host if host is not None else self._daemon_host_from_env()
        if resolved is None:
            return self._connect_local()
        return self._connect_remote(resolved)

    # -- internals -----------------------------------------------------

    @staticmethod
    def _daemon_host_from_env() -> Optional[str]:
        value = os.environ.get(APRILCAM_DAEMON_HOST_ENV, "").strip()
        return value or None

    def _connect_local(self) -> Daemon:
        channel = grpc.insecure_channel(f"unix:{self.socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=self._connect_timeout)
        except grpc.FutureTimeoutError as exc:
            channel.close()
            raise ConnectionFailed("localhost", reason="no local daemon answered") from exc
        return GrpcDaemon(aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel), target="localhost")

    def _connect_remote(self, host: str) -> Daemon:
        target = host if ":" in host else f"{host}:{DEFAULT_DAEMON_PORT}"
        channel = grpc.insecure_channel(target)
        try:
            grpc.channel_ready_future(channel).result(timeout=self._connect_timeout)
        except grpc.FutureTimeoutError as exc:
            channel.close()
            raise ConnectionFailed(host, reason="no daemon answered") from exc
        return GrpcDaemon(aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel), target=host)
