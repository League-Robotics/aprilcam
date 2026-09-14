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

**Resolved by sprint 028 (ticket 004).** That future sprint arrived:
``find_all()`` now merges the local lock-probe above -- kept
**unchanged**, still the first thing this method does -- with a real
``zeroconf.ServiceBrowser`` scan of every ``_aprilcam._tcp.local.``
advertisement (``daemon/mdns.py``'s ``MdnsAdvertiser``, ticket 003) on
the network. The lock-probe stays because it is the more precise,
instant, network-independent answer for "is a daemon running on *this*
machine" (sprint 028's Design Rationale, Step 6): making local detection
depend on the local machine's own multicast stack working at all would
regress ``test_find_all_lists_a_reachable_local_daemon``'s "no network
needed" property for no benefit. Browsed entries are **self-filtered**:
each advertisement's TXT ``host`` property (set by ``MdnsAdvertiser`` to
``socket.gethostname()`` on the daemon side) is compared against this
process's own ``socket.gethostname()``, and a match is dropped -- it is
the local daemon's own advertisement, already accounted for by the
lock-probe, and must never appear a second time. A genuine browse
*failure* (the ``self._browse()`` call itself raising ``OSError`` --
e.g. the local multicast stack is blocked) raises
:class:`~aprilcam.errors.DiscoveryFailed`, via the identical
``except OSError: raise DiscoveryFailed(...) from exc`` pattern the
lock-probe already used -- distinct from "browsed and found nothing,"
which is this method's ordinary empty-result path.

**Load-bearing formatting: a browsed (non-local) ``DaemonInfo.host``
carries its port, as ``"ip:port"``.** ``cli/_common.py``'s ``fan_out()``
(and, by extension, ``aprilcam daemon list``, ticket 005) connects to a
discovered remote daemon via ``discovery.connect(info.host)`` alone --
it never reads ``DaemonInfo.port`` separately. For that to keep working
completely unmodified against a daemon discovered on a non-default port
(the two-local-daemon test scenario's whole point), a browsed entry's
``host`` field must itself embed the port, in exactly the format
:meth:`Discovery._connect_remote` already parses (its own ``":" in
host`` check). ``DaemonInfo.port`` is still populated too (the field
exists on every entry, local or not), but the *connect-ability*
contract runs through ``host`` -- matching this codebase's existing
convention rather than inventing a new one. Do not "simplify" ``host``
back to a bare IP for a remote entry; that silently breaks remote
fan-out to any daemon not on ``DEFAULT_DAEMON_PORT``.

Finding nothing is an **empty result, not an error** (UC-13.S-2): no
lock held and no browse hits means no daemon is running (or reachable),
full stop. Discovery **failing** -- this module could not even complete
its local probe or its browse, e.g. a permission error reading
``socket_dir``, or the local machine's multicast stack rejecting a
socket open -- is reported distinctly, as
:class:`~aprilcam.errors.DiscoveryFailed` (UC-13.S-5's "empty list
would send someone hunting for a dead daemon instead of a network
problem").

``connect(host)`` never goes through discovery at all -- it is a
**direct connect**: a unix socket for local (``host is None``), plain
TCP for a named remote. This is what lets ``-d HOST`` work without any
discovery machinery, and it is unaffected by everything above: it never
calls :meth:`Discovery.find_all` or ``self._browse``, so it costs
nothing extra and cannot be slowed or failed by a broken browse
(UC-13.S-5's own explicit callout). Precedence for an unspecified target
follows ``docs/design/configuration.md``'s cascade: an explicit *host*
argument (the CLI's ``-d`` flag, or any other caller-supplied value)
beats ``APRILCAM_DAEMON_HOST``, which beats the local default. There is
no "file" tier here this sprint -- no daemon-target configuration
document exists yet to read one from.
"""

from __future__ import annotations

import fcntl
import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import grpc
from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.client.interfaces import DaemonInfo
from aprilcam.client.interfaces import Discovery as _DiscoveryBase
from aprilcam.config.directories import DEFAULT_DAEMON_PORT, Directories
from aprilcam.daemon.interfaces import Daemon
from aprilcam.daemon.lifecycle import LOCK_FILENAME, SOCKET_FILENAME
from aprilcam.errors import ConnectionFailed, DiscoveryFailed
from aprilcam.proto import aprilcam_v2_pb2_grpc

__all__ = [
    "APRILCAM_DAEMON_HOST_ENV",
    "DEFAULT_DAEMON_PORT",
    "Discovery",
    "probe_lock_holder",
    "probe_socket_reachable",
    "resolve_ipv4_preferred",
    "split_host_port",
]

logger = logging.getLogger(__name__)

# DEFAULT_DAEMON_PORT itself: re-exported from ``config.directories``
# (moved there in sprint 028, ticket 003, so ``client/`` and
# ``daemon/`` share one definition without either importing the
# other) via the module-level import above, rather than redefined
# here -- ``from aprilcam.client.discovery import DEFAULT_DAEMON_PORT``
# keeps working unchanged for every existing importer, this module's
# own :meth:`Discovery._connect_remote` included, and stays listed in
# __all__ above. Originally the *only* default port for an explicitly
# named remote host (``-d HOST`` with no ``:port`` suffix, or
# ``APRILCAM_DAEMON_HOST`` likewise bare) because no daemon actually
# *listened* on TCP yet; as of ticket 003 it is also the port
# ``daemon/rpc.py``'s ``build_server`` binds by default, so this
# constant's two meanings ("what a client assumes" and "what the
# daemon opens") now agree by construction rather than by coincidence.

APRILCAM_DAEMON_HOST_ENV = "APRILCAM_DAEMON_HOST"

_DEFAULT_PROBE_TIMEOUT = 1.0
_DEFAULT_CONNECT_TIMEOUT = 5.0

#: The service type every AprilCam daemon advertises under (ticket
#: 028-003). Duplicated verbatim from ``daemon/mdns.py``'s own
#: ``_SERVICE_TYPE`` -- not imported from there, deliberately: that name
#: is module-private, and this sprint's dependency-direction diagram
#: (sprint.md Step 4) draws ``client/`` and ``daemon/`` as two
#: independent consumers of ``zeroconf``, not of each other, mirroring
#: the same "duplicate a two-line constant rather than add a coupling
#: edge" call already made for ``tags/generator.py`` vs.
#: ``vision/detector.py`` in this same sprint (sprint.md Design
#: Rationale). A future change to either string must update both --
#: cross-referenced here and there for exactly that reason.
_SERVICE_TYPE = "_aprilcam._tcp.local."

#: How long the real (default) browse implementation listens before
#: returning whatever it has found. mDNS announcements/responses on a
#: local network are ordinarily near-instant (well under a second,
#: including the loopback-multicast round trip this sprint's own
#: two-local-daemon integration test measured at ~0.01s of actual
#: propagation time); 2 seconds is a deliberately generous bound on top
#: of that -- enough margin for a slow interface or a loaded CI runner
#: -- while still keeping ``find_all()``/``aprilcam daemon list``
#: bounded rather than hanging indefinitely on a machine with no
#: responders (this value's own test:
#: ``test_find_all_with_the_real_browse_default_returns_promptly_when_
#: nothing_answers`` in ``tests/aprilcam/client/test_discovery.py``,
#: using a shrunk timeout so the *bound itself* is verified quickly).
_DEFAULT_BROWSE_TIMEOUT = 2.0


@dataclass(frozen=True, kw_only=True)
class _BrowseResult:
    """One daemon a ``browse`` callable found -- the small, local,
    easily-faked shape ``Discovery``'s new ``browse=`` DI seam speaks in
    (ticket 028-004), deliberately **not** a raw zeroconf
    ``ServiceInfo``/``Zeroconf`` object, so every test but the one real
    integration test can inject plain data with no zeroconf import at
    all -- mirroring how ``lock_holder``/``socket_reachable`` take plain
    ``Path``s rather than daemon-internal types.

    ``address``/``port`` are the browsed daemon's real reachable
    TCP endpoint (an IPv4 dotted string and its advertised control-plane
    port); ``hostname`` is the advertisement's TXT ``host`` property
    (``daemon/mdns.py``'s ``MdnsAdvertiser`` sets this to
    ``socket.gethostname()``, or a test-only override) -- what
    :meth:`Discovery.find_all`'s self-filter compares against this
    process's own ``socket.gethostname()``.
    """

    address: str
    port: int
    hostname: str


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


def _probe_remote_reachable(target: str, timeout: float = _DEFAULT_PROBE_TIMEOUT) -> bool:
    """Whether a gRPC channel to *target* (``"ip:port"``) becomes ready
    within *timeout* -- the TCP-target counterpart of
    :func:`probe_socket_reachable`, used by :meth:`Discovery.find_all`
    for each browsed (non-local) entry that survives the self-filter
    (ticket 028-004). Not a constructor DI seam of its own (unlike
    ``lock_holder``/``socket_reachable``/``browse``) -- a browsed
    entry's reachability is always this same real connect/channel-ready
    probe, never overridden independently; tests exercise it by
    pointing a fake ``browse`` entry at either a real bound TCP listener
    (reachable) or a refusing port (unreachable, mirroring how
    ``test_connect_remote_host_raises_connection_failed_when_nothing_
    answers`` already proves a refused TCP connect fails fast rather
    than hanging for the full timeout)."""
    channel = grpc.insecure_channel(target)
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
        return True
    except grpc.FutureTimeoutError:
        return False
    finally:
        channel.close()


def split_host_port(host: str) -> Tuple[str, int]:
    """Split a ``-d``-style *host* value into ``(name_or_literal, port)``,
    honouring gRPC's own bracketed-IPv6 target syntax (``"[::1]:5280"``,
    ``"[fe80::1]"``) so a literal IPv6 address's own colons are never
    mistaken for a ``:port`` separator. Falls back to
    :data:`DEFAULT_DAEMON_PORT` when *host* carries no port at all --
    including a bare, unbracketed IPv6 literal (``"fe80::1"``), which is
    indistinguishable from "no port" by colon-counting alone and was
    already treated that way by the pre-040-007 ``":" in host`` check
    this function replaces the reasoning for, not the behaviour of."""
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            literal = host[1:end]
            rest = host[end + 1 :]
            if rest.startswith(":") and rest[1:].isdigit():
                return literal, int(rest[1:])
            return literal, DEFAULT_DAEMON_PORT
    if host.count(":") == 1:
        name, _, port_str = host.partition(":")
        if port_str.isdigit():
            return name, int(port_str)
    return host, DEFAULT_DAEMON_PORT


def resolve_ipv4_preferred(host: str, port: int) -> Optional[Tuple[int, str]]:
    """Resolve *host* (a bare hostname -- never a ``host:port`` string;
    split that with :func:`split_host_port` first), preferring an
    ``AF_INET`` (IPv4) candidate over any ``AF_INET6`` one -- ticket
    040-007's fix for the root cause found live against ``vidar.local``:
    the Mac's resolver lists vidar's IPv6 link-local addresses
    (``dscacheutil -q host -a name vidar.local``) before its IPv4
    address, and a raw hostname handed to ``grpc.insecure_channel``
    lets gRPC's own C-core resolver pick the unreachable link-local
    candidate and wait out the full connect timeout instead of falling
    back to IPv4 itself.

    Returns ``(family, address_literal)`` for the chosen candidate, or
    ``None`` in the two cases where there is nothing useful for a
    caller to do differently from before this ticket:

    - *host* is already an IP literal (v4 or v6) -- nothing to
      resolve; a caller should keep using it exactly as typed.
    - resolution genuinely fails (``socket.gaierror``) or returns no
      candidates at all -- a caller falls back to its own prior
      behaviour (handing the raw hostname to gRPC/``socket.connect``,
      which fails with the same clean, typed error as before this
      ticket; see ``docs/design`` for why the daemon's own bind is not
      the thing this ticket changes).

    When *host* resolves to IPv6 addresses only (no ``AF_INET``
    candidate -- e.g. an AAAA-only hostname), the *first* IPv6
    candidate the resolver returned is used, preserving whatever
    ordering/reachability the OS itself already provides for that
    case -- this ticket only reorders IPv4 ahead of IPv6, it does not
    re-rank IPv6 candidates against each other.

    Never raises: every failure mode above is a plain ``None`` return,
    so a caller can use this unconditionally ahead of its existing
    connect logic without adding a new exception path.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None  # already a literal -- nothing to resolve

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None
    if not infos:
        return None

    # Stable sort: AF_INET (0) before AF_INET6 (1), otherwise preserving
    # the resolver's own relative ordering within each family -- e.g.
    # multiple IPv6 candidates keep the order the OS returned them in.
    infos = sorted(infos, key=lambda info: 0 if info[0] == socket.AF_INET else 1)
    family, _socktype, _proto, _canonname, sockaddr = infos[0]
    # sockaddr's typeshed type is a union of the AF_INET (2-tuple) and
    # AF_INET6 (4-tuple) shapes, so mypy widens sockaddr[0] to `str | int`
    # even though the first element is always the address string for
    # both -- an explicit str() satisfies the declared `Tuple[int, str]`
    # return type without changing the runtime value.
    return int(family), str(sockaddr[0])


def _browse_zeroconf(timeout: float = _DEFAULT_BROWSE_TIMEOUT) -> Tuple[_BrowseResult, ...]:
    """The real (default) ``browse`` implementation: a bounded
    ``zeroconf.ServiceBrowser`` scan of ``_SERVICE_TYPE`` (ticket
    028-004). Converted from v1's ``client/discovery.py``'s
    ``discover_daemons`` -- earned knowledge kept: browse, then
    ``time.sleep(timeout)`` for a bounded collection window, then tear
    down -- rewritten to return this module's own :class:`_BrowseResult`
    shape instead of v1's differently-shaped ``DaemonInfo``, and to
    require ``zeroconf`` rather than degrade when it is absent (this
    sprint's architecture: ``zeroconf`` is now an unconditional base
    dependency, see ``daemon/mdns.py``'s module docstring for the same
    change on the advertising side).

    Any ``OSError`` opening the multicast socket (``Zeroconf()`` itself,
    e.g. no usable network interface, or multicast blocked) propagates
    to the caller **unhandled** -- unlike ``daemon/mdns.py``'s
    ``MdnsAdvertiser`` (where a failure to advertise is deliberately
    never load-bearing), a *client* that cannot browse at all has
    genuinely failed to discover, and :meth:`Discovery.find_all` is
    exactly what turns that into :class:`~aprilcam.errors.
    DiscoveryFailed`, distinct from "browsed and found nothing"
    (UC-13.S-5). See this module's docstring for the full rationale.
    """
    zc = Zeroconf()
    found: Dict[str, _BrowseResult] = {}

    def _on_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
        **_kwargs: object,
    ) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        info = zeroconf.get_service_info(service_type, name)
        if info is None or info.port is None:
            return
        addresses = info.parsed_addresses()
        if not addresses:
            return
        raw_host = (info.properties or {}).get(b"host")
        hostname = raw_host.decode() if raw_host else name
        found[name] = _BrowseResult(address=addresses[0], port=info.port, hostname=hostname)

    # *browser* is constructed inside the try/finally (not alongside
    # `zc = Zeroconf()` above) so that if ServiceBrowser's own
    # constructor raises, zc.close() still runs below rather than
    # leaking the just-opened multicast socket -- the browse-side
    # analogue of MdnsAdvertiser.start's own "close a successfully
    # constructed Zeroconf even when the next step fails" guarantee.
    browser: Optional[ServiceBrowser] = None
    try:
        browser = ServiceBrowser(zc, _SERVICE_TYPE, handlers=[_on_change])
        time.sleep(timeout)
    finally:
        if browser is not None:
            browser.cancel()
        zc.close()
    return tuple(found.values())


class Discovery(_DiscoveryBase):
    """Concrete ``client.interfaces.Discovery``. See the module
    docstring for the local-probe/mDNS-browse split (sprint 028, ticket
    004) and the connect precedence.

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
    browse:
        Overridable mDNS browse (ticket 028-004), returning a tuple of
        :class:`_BrowseResult` -- same DI style, same place in the
        constructor as ``lock_holder``/``socket_reachable`` above.
        ``None`` (the default) resolves to :func:`_browse_zeroconf`, the
        real bounded ``zeroconf.ServiceBrowser`` scan, at construction
        time rather than being bound directly as the parameter's default
        value -- deliberately, so a test fixture shared by every test in
        ``test_discovery.py`` can blanket-replace the module-level
        :func:`_browse_zeroconf` name itself (``monkeypatch.setattr``)
        and have it take effect for every ``Discovery()`` construction
        that does not pass its own ``browse=``, including the pre-028-004
        tests that construct a plain ``Discovery(directories=...)`` with
        no opinion about mDNS at all. A real caller never notices the
        difference: production code never passes ``browse=`` either way,
        so it always gets the real implementation.
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
        browse: Optional[Callable[[], Tuple[_BrowseResult, ...]]] = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._directories = directories if directories is not None else Directories()
        self._lock_holder = lock_holder
        self._socket_reachable = socket_reachable
        self._browse: Callable[[], Tuple[_BrowseResult, ...]] = (
            browse if browse is not None else _browse_zeroconf
        )
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

    def find_all(self) -> Tuple[DaemonInfo, ...]:
        """See ``client.interfaces.Discovery.find_all``. See the module
        docstring for the full local-probe/mDNS-browse/self-filter
        design (ticket 028-004).

        Order: the local lock-probe runs first, exactly as it always
        has -- unchanged code, unchanged behaviour, unaffected by
        whether the browse step below succeeds, finds nothing, or is
        never reached. Then the mDNS browse runs, wrapped in the same
        ``except OSError: raise DiscoveryFailed`` pattern the lock-probe
        already uses just above. Browsed entries whose advertised
        hostname matches this process's own ``socket.gethostname()``
        are dropped (the local daemon's own advertisement -- already
        accounted for by the lock-probe). Every remaining entry is
        reachability-probed for real (never assumed reachable merely
        because it advertised) and returned as a non-local
        :class:`~aprilcam.client.interfaces.DaemonInfo` whose ``host``
        carries the port (``"ip:port"``, see the module docstring's
        "Load-bearing formatting" note). The local entry, if any, comes
        first.
        """
        try:
            holder = self._lock_holder(self.lock_path)
        except OSError as exc:
            raise DiscoveryFailed(f"could not probe the local daemon: {exc}") from exc
        local: Tuple[DaemonInfo, ...] = ()
        if holder is not None:
            reachable = self._socket_reachable(self.socket_path)
            local = (DaemonInfo(host="localhost", port=0, is_local=True, reachable=reachable),)

        try:
            browsed = self._browse()
        except OSError as exc:
            raise DiscoveryFailed(f"could not browse for daemons: {exc}") from exc

        own_hostname = socket.gethostname()
        remote = tuple(
            DaemonInfo(
                host=f"{entry.address}:{entry.port}",
                port=entry.port,
                is_local=False,
                reachable=_probe_remote_reachable(f"{entry.address}:{entry.port}"),
            )
            for entry in browsed
            if entry.hostname != own_hostname
        )
        return local + remote

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
        target = self._resolve_grpc_target(host)
        channel = grpc.insecure_channel(target)
        try:
            grpc.channel_ready_future(channel).result(timeout=self._connect_timeout)
        except grpc.FutureTimeoutError as exc:
            channel.close()
            raise ConnectionFailed(host, reason="no daemon answered") from exc
        return GrpcDaemon(aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel), target=host)

    @staticmethod
    def _resolve_grpc_target(host: str) -> str:
        """The literal string actually handed to
        ``grpc.insecure_channel`` for a remote *host* -- ticket 040-007.
        *host* itself (the ``-d`` value, or ``DaemonInfo.host`` from a
        browse/env lookup) is never touched: it stays exactly what the
        stakeholder typed for :class:`~aprilcam.errors.ConnectionFailed`
        and ``GrpcDaemon``'s own ``target`` (display, error messages,
        host-qualified camera codes -- sprint 017) -- only the address
        gRPC dials is resolved ahead of time here, preferring IPv4.

        A bare hostname resolves via :func:`resolve_ipv4_preferred`; an
        IP literal (v4 or v6, bracketed or not) or a hostname that
        cannot be resolved at all falls through to the exact
        pre-040-007 formatting (``host`` unchanged, default port
        appended only when *host* carries no colon), so gRPC's own
        resolver/error path runs unchanged for those cases -- including
        the unresolvable-hostname case, which still ends up as the same
        :class:`~aprilcam.errors.ConnectionFailed` this method's caller
        already raises on a connect timeout.
        """
        name, port = split_host_port(host)
        resolved = resolve_ipv4_preferred(name, port)
        if resolved is None:
            return host if ":" in host else f"{host}:{DEFAULT_DAEMON_PORT}"
        family, address = resolved
        return f"[{address}]:{port}" if family == socket.AF_INET6 else f"{address}:{port}"
