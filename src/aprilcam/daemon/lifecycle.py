"""``DaemonLifecycle`` -- server-side process lifecycle (ticket 020-004):
the singleton lock, the control-plane listener's start/stop, and the
local process-view of readiness.

Converted from ``archive/src/aprilcam/daemon/server.py``'s
``_acquire_global_lock``/``_release_global_lock``/``run``/``_shutdown``
machinery (459 lines), narrowed to unix-socket-only per ticket 001's
Open-Question-2 resolution (no TCP:5280 this sprint) and to this
sprint's four-RPC surface -- no camera pipelines, no mDNS, no stream
producers here (out of scope; see the sprint architecture's Decision 2
and Out-of-Scope section).

**Two different ``DaemonLifecycle``\\ s, by design.** This class
structurally satisfies ``daemon.interfaces.DaemonLifecycle``
(``start``/``stop``/``status``) but runs **in the daemon's own
process** -- it is what ``daemon/__main__.py`` constructs and drives
once it *is* the daemon. It is **not** the CLI-facing implementation
``aprilcam daemon start`` calls: that one (ticket 020-005's
``client/lifecycle.py``, per the sprint architecture's module table)
spawns/signals ``daemon/__main__.py`` as a subprocess from the
*caller's* process, and blocks on subprocess/socket readiness rather
than on lock acquisition. Both are legitimate, independent
implementations of the same base class -- one is "the mechanics that run
once you are the daemon process," the other is "how an outside process
starts/stops that process." Conflating them would mean the CLI's
``daemon start`` never returns: this class's ``start()`` returns as
soon as the control plane is up; the block-until-shutdown loop lives in
``daemon/__main__.py``, mirroring v1's ``DaemonServer.run()``.

**Ordering: lock before anything else, always.** ``start()`` acquires
the flock first and only then does any camera-discovery or
control-plane work. This is what makes ``AlreadyRunning`` the failure a
second instance actually sees (UC-01.S-3) rather than a socket-bind-in-
use error that could equally mean a stale, uncleaned socket file from a
crashed prior run -- the two failure modes must never be confusable,
and the second instance must never touch the socket path at all
(UC-01.S-3's "does not disturb the running daemon" promise).

**Ordering: enumeration before the listener (020-007 regression fix).**
``start()`` runs the synchronous half of ``watch`` -- the initial
active camera probe (``daemon/__main__.py``'s ``enumerate_then_watch``)
-- **before** building and starting the control-plane listener, not
after. Earlier this sprint the order was reversed (listener up first,
enumeration second), which opened a real, observed race: the moment
``listener.start()`` returns, a gRPC channel to the socket succeeds and
RPCs are already being dispatched on the listener's own thread pool,
concurrently with this method's calling thread still running the
(synchronous, sometimes multi-second on AVFoundation) initial probe.
``client/lifecycle.py``'s ``start()`` treats "the socket accepts a
connection" as its sole readiness signal, so it could return success --
and a caller-issued ``camera list`` immediately after could reach a
daemon whose registry was still empty. Starting enumeration first means
the control-plane socket does not exist at all until the daemon
genuinely knows its cameras, so *any* readiness signal built on socket
reachability -- this client's included -- is automatically correct with
no client-side change required. See ``client/lifecycle.py``'s module
docstring and ``tests/aprilcam/client/test_lifecycle.py`` for the
client-side half of this fix's verification, and
``test_start_calls_watch_before_building_the_control_plane_listener``
below for the ordering regression test.

**The lock is never unlinked** (matches v1): removing it would reopen a
create+lock race between the unlink and the next daemon's open+flock.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from typing import Callable, Optional

from aprilcam.config.interfaces import Directories
from aprilcam.daemon.interfaces import DaemonLifecycle as _DaemonLifecycleBase
from aprilcam.daemon.interfaces import DaemonStatus
from aprilcam.errors import AlreadyRunning

__all__ = ["ControlPlaneListener", "DaemonLifecycle", "LOCK_FILENAME", "SOCKET_FILENAME"]

logger = logging.getLogger(__name__)

#: Both live under ``Directories.socket_dir`` -- runtime plumbing,
#: alongside each other, per ``docs/design/design.md`` §7
#: ("socket_dir/ # control.sock + per-camera stream sockets"). The lock
#: is not itself a socket, but it is exactly as much "nobody needs to
#: choose where this lives" runtime plumbing as the socket is
#: (``docs/design/configuration.md`` §1), so it shares the directory
#: rather than inventing a third root.
LOCK_FILENAME = "daemon.lock"
SOCKET_FILENAME = "control.sock"


class ControlPlaneListener:
    """The minimal surface this module needs from a control-plane
    server object -- implemented by ``daemon.rpc._GrpcListener``, which
    wraps a ``grpc.Server``.

    A plain base class, not ``Protocol`` -- see ``daemon.interfaces``'s
    module docstring for the design rule -- but the base class lives
    *here*, not in ``daemon/rpc.py``, and ``daemon/rpc.py`` imports it
    (the reverse of ``daemon/rpc.py``'s own gRPC import), so this
    module -- and its headless unit tests -- still carry no gRPC (or
    camera, or cv2) import at all: a bare fake deriving from this class
    with just these two methods exercises every lock/shutdown behaviour
    this ticket's ``test_lifecycle.py`` covers, which is the whole point
    of keeping "camera enumeration, RPC dispatch" outside this module's
    boundary (sprint architecture's module table). Converting this from
    ``Protocol`` to a base class does not add a gRPC import here: it
    only requires that whoever builds a real listener (``daemon.rpc.
    _GrpcListener``) derive from it, which that module already imports
    ``daemon.lifecycle`` to do without creating a cycle (this module
    never imports ``daemon.rpc``).
    """

    def start(self) -> Optional[int]:
        """Start accepting connections. Returns the TCP port this
        listener is reachable on, or ``None`` if it was never asked to
        bind TCP or that bind failed (ticket 028-003) -- unix-socket
        reachability is unconditional and not reported here, since
        every ``ControlPlaneListener`` has always had a unix socket;
        only the *optional* TCP half needs a way to say "not this
        time". ``DaemonLifecycle.start()`` uses this return value to
        decide whether (and with which port) to call its ``advertise``
        hook. A listener that never offers TCP at all (e.g. a test
        double) simply returns ``None`` unconditionally -- a bare
        ``return``/implicit ``None`` already satisfies this signature,
        so no existing fake needs to change for tests that do not care
        about TCP.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlPlaneListener.start")

    def stop(self, grace: float) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlPlaneListener.stop")


class DaemonLifecycle(_DaemonLifecycleBase):
    """See the module docstring for the client-side/server-side split.

    Derives from ``daemon.interfaces.DaemonLifecycle`` (see that
    module's design-rule docstring): a plain base class, not
    ``Protocol``, so ``isinstance(instance, daemon.interfaces.
    DaemonLifecycle)`` is a real inheritance check rather than a
    structural guess.

    Parameters
    ----------
    directories:
        Resolves the default lock/socket paths (both under
        ``directories.socket_dir``) and supplies ``config_dir``/
        ``state_dir`` for ``status()`` while not running.
    build_listener:
        Zero-argument factory returning a fresh, unstarted
        :class:`ControlPlaneListener`. A **factory**, not a pre-built
        object, because a stopped ``grpc.Server`` cannot be restarted --
        this is what makes ``stop()`` then ``start()`` on one
        ``DaemonLifecycle`` (the restart-stability scenario, UC-01.S-1)
        bind a genuinely fresh listener each time rather than reusing a
        dead one. Real callers pass
        ``functools.partial(daemon.rpc.build_server, service,
        socket_path)`` (``daemon/__main__.py``); tests pass a bare fake.
    status_source:
        Zero-argument callable returning the *service* status view
        (``daemon.service.DaemonService.status``) -- this class touches
        no camera or registry state itself; it only reports it while
        running (see ``status()`` below).
    watch / stop_watch:
        Optional hooks into ``CameraDiscovery``'s passive watching
        (UC-01 main flow step 4: "begins watching for cameras arriving
        and disappearing"). ``None`` (the default) skips passive
        watching entirely -- this ticket's headless lock/shutdown tests
        have no camera subsystem wired at all and do not need it.
    advertise / stop_advertise (ticket 028-003):
        Optional hooks into mDNS self-advertisement
        (``daemon.mdns.MdnsAdvertiser.start``/``.stop``, wired by
        ``daemon/__main__.py``). ``advertise`` is called from
        :meth:`start`, after the control-plane listener itself has
        started, and only when :meth:`ControlPlaneListener.start`
        reported a real bound TCP port (never before the control plane
        is genuinely accepting connections, and never when TCP did not
        bind) -- there is no port to announce any earlier than that.
        ``stop_advertise`` is called from :meth:`stop` alongside
        ``stop_watch``, i.e. early, before the listener stops accepting
        RPCs -- matching this method's own "stop passive watching
        first... then the listener" ordering, extended one hook
        further. ``None`` (the default) skips advertisement entirely,
        exactly like ``watch``/``stop_watch``.
    on_stop:
        Optional zero-argument hook run during ``stop()``, after the
        listener has stopped accepting new RPCs and before the lock is
        released -- ``daemon/__main__.py`` wires ``DaemonService.close``
        here so no held-open ``Camera`` outlives a clean shutdown.
    lock_path / socket_path:
        Override the resolved defaults (``directories.socket_dir /
        "daemon.lock"`` / ``"control.sock"``). Tests pass a ``tmp_path``
        pair so they never collide with a real running daemon on this
        machine, per this ticket's Testing section.
    stop_grace:
        Seconds ``stop()`` allows the listener to finish in-flight RPCs
        before it is forced closed (mirrors v1's ``grace=5``).
    """

    def __init__(
        self,
        *,
        directories: Directories,
        build_listener: Callable[[], ControlPlaneListener],
        status_source: Callable[[], DaemonStatus],
        watch: Optional[Callable[[], None]] = None,
        stop_watch: Optional[Callable[[], None]] = None,
        advertise: Optional[Callable[[int], None]] = None,
        stop_advertise: Optional[Callable[[], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
        lock_path: Optional[Path] = None,
        socket_path: Optional[Path] = None,
        stop_grace: float = 5.0,
    ) -> None:
        self._directories = directories
        self._build_listener = build_listener
        self._status_source = status_source
        self._watch = watch
        self._stop_watch = stop_watch
        self._advertise = advertise
        self._stop_advertise = stop_advertise
        self._on_stop = on_stop
        self._lock_path = (
            Path(lock_path) if lock_path is not None else directories.socket_dir / LOCK_FILENAME
        )
        self._socket_path = (
            Path(socket_path) if socket_path is not None else directories.socket_dir / SOCKET_FILENAME
        )
        self._stop_grace = stop_grace

        self._lock_fd: Optional[int] = None
        self._listener: Optional[ControlPlaneListener] = None
        self._running = False

    @property
    def socket_path(self) -> Path:
        """Where the control-plane unix socket is (will be) bound."""
        return self._socket_path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    # -- daemon.interfaces.DaemonLifecycle ---------------------------------

    def start(self) -> None:
        """See ``daemon.interfaces.DaemonLifecycle.start``.

        Order: acquire the singleton lock (raising :class:`AlreadyRunning`
        naming the existing holder on contention, per the module
        docstring); only then run the initial camera enumeration and
        start passive watching, if wired; only then build and start the
        control-plane listener -- see the module docstring's "Ordering:
        enumeration before the listener" section for why this order
        (not "listener, then enumerate") is what makes the control
        plane's own reachability a trustworthy readiness signal. Starting
        with **no cameras and no configuration is success** (UC-01.S-2)
        -- nothing here inspects camera count at all, so an empty-field
        start is neither slower nor different in kind from a populated
        one.

        **Ticket 028-003**: once the listener has started, if it
        reports a real bound TCP port (``listener.start()``'s return
        value, not ``None``) and an ``advertise`` hook was wired, that
        hook is called with the port. There is no port to announce
        before the listener has genuinely started, and nothing to
        announce at all if TCP never bound -- both conditions are
        checked here so ``advertise`` never fires for a listener that
        only ever offered a unix socket.
        """
        self._acquire_lock()
        if self._watch is not None:
            self._watch()
        listener = self._build_listener()
        tcp_port = listener.start()
        self._listener = listener
        self._running = True
        if self._advertise is not None and tcp_port is not None:
            self._advertise(tcp_port)
        logger.info(
            "daemon lifecycle started: lock=%s socket=%s", self._lock_path, self._socket_path
        )

    def stop(self) -> None:
        """See ``daemon.interfaces.DaemonLifecycle.stop``.

        Shutdown order (independent of ``start()``'s own ordering, per
        020-007): stop passive watching first, so no further discovery
        updates land mid-shutdown; **then stop mDNS advertisement
        (ticket 028-003)**, alongside it, before the listener stops
        accepting RPCs -- so a browsing client never sees this daemon
        advertised a moment after it has already stopped announcing
        readiness for new work; then the listener (grace period for
        in-flight RPCs), so no new RPC can be dispatched afterward; then
        any held-camera cleanup (``on_stop``); then the lock last -- so
        a racing second ``start()`` can never observe a released lock
        while the old listener is still mid-shutdown. Safe to call when
        not running (a no-op), matching UC-13's "stop when not running:
        succeed quietly."
        """
        if not self._running:
            return
        if self._stop_watch is not None:
            self._stop_watch()
        if self._stop_advertise is not None:
            self._stop_advertise()
        if self._listener is not None:
            self._listener.stop(self._stop_grace)
            self._listener = None
        if self._on_stop is not None:
            self._on_stop()
        self._release_lock()
        self._running = False
        logger.info("daemon lifecycle stopped")

    def status(self) -> DaemonStatus:
        """See ``daemon.interfaces.DaemonLifecycle.status``.

        The **local process view**: while running, delegates to the
        service's own status (the same values ``GetStatus`` would
        answer, since this process *is* the daemon); while not running,
        reports ``running=False`` with an empty camera list and the
        directories this instance would use if started -- never a stale
        snapshot left over from before a ``stop()``.
        """
        if self._running:
            return self._status_source()
        return DaemonStatus(
            running=False,
            cameras=(),
            config_dir=str(self._directories.config_dir),
            state_dir=str(self._directories.state_dir),
        )

    # -- singleton lock (converted from archive/.../daemon/server.py) -----

    def _acquire_lock(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = self._read_holder(fd)
            os.close(fd)
            raise AlreadyRunning(holder) from None
        # Lock acquired -- rewrite the pid so a contending instance (or
        # `daemon status`, later tickets) can name us.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd

    @staticmethod
    def _read_holder(fd: int) -> str:
        """Best-effort identity of whoever holds the lock, for
        :class:`AlreadyRunning`'s message (UC-01.S-3: "naming the
        existing instance's identity"). Falls back to the lock path
        itself if the pid cannot be read -- still names *something*
        rather than a bare "already running".
        """
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            contents = os.read(fd, 64).decode().strip()
            if contents:
                return f"pid {contents}"
        except OSError:  # pragma: no cover - defensive
            pass
        return "unknown holder"

    def _release_lock(self) -> None:
        """``LOCK_UN`` + close, no unlink -- see the module docstring."""
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - defensive
            pass
        try:
            os.close(self._lock_fd)
        except OSError:  # pragma: no cover - defensive
            pass
        self._lock_fd = None
