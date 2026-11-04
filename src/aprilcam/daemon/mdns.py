"""``MdnsAdvertiser`` -- mDNS/Bonjour self-advertisement of the daemon's
control plane (ticket 028-003).

Converted from ``archive/src/aprilcam/daemon/mdns.py``'s
``MDNSAdvertiser``, mined for two pieces of genuinely earned knowledge
and rewritten otherwise -- see :func:`_primary_routable_ipv4` and the
TXT property keys in :meth:`MdnsAdvertiser.start` below for what was
kept, and why.

**No base class, deliberately** (sprint 028 architecture, Design
Rationale): there is exactly one implementation of "advertise this
daemon over mDNS" in this codebase and no anticipated second one, so
this is a plain concrete class -- not a ``daemon.interfaces``-style
behavioral base with `raise NotImplementedError` methods, and (per this
project's standing rule, see ``daemon/interfaces.py``'s module
docstring) never a ``typing.Protocol`` either. Interchangeability is
provided at the constructor's *zeroconf_factory* seam instead (see
below), mirroring ``client/discovery.py``'s ``Discovery``'s
``lock_holder``/``socket_reachable`` dependency-injection pattern --
tests substitute a fake there rather than substituting a fake
``MdnsAdvertiser`` subclass.

**``zeroconf`` is an unconditional base dependency in v2** (``pyproject
.toml``'s base ``dependencies``, confirmed during this ticket's
planning) -- unlike v1, which had to tolerate the package being absent
and therefore imported it lazily, inside ``start()``'s own ``try``
block, with every field typed ``Optional[object]``. Neither is needed
here: ``ServiceInfo``/``Zeroconf`` import at module scope like any
other dependency, and :class:`MdnsAdvertiser`'s fields are typed
``Optional[Zeroconf]``/``Optional[ServiceInfo]``.

**Advertising is never load-bearing.** Every zeroconf call this module
makes is wrapped so a registration or teardown failure is logged at
WARNING and never propagates -- mirrors the "TCP bind failure is
non-fatal" contract ``daemon/rpc.py``'s ``build_server`` documents for
the same reason: the daemon must never fail to start, or fail to shut
down cleanly, because the local network's multicast stack is
unavailable or misbehaving. Unlike v1's ``except Exception:  #
noqa: BLE001``, the exceptions caught here are specific: ``OSError``
(socket-level failures -- opening the multicast socket, resolving an
address) and ``zeroconf.Error`` (the base of every exception type the
``zeroconf`` package itself documents as raisable from
``Zeroconf()``/``register_service``/``unregister_service``/``close``,
e.g. ``NonUniqueNameException``, ``NotRunningException``).

**Never advertises loopback** (sprint 049, ticket 003). A daemon started
before the network is up (angst booted on Wi-Fi only; aprilcamd started
3 s before ``wlo1`` had carrier) used to publish ``127.0.0.1``, and
remote clients then dialed *their own* loopback. Now
:func:`_primary_routable_ipv4` returns ``None`` when there is no routable
address, :meth:`MdnsAdvertiser.start` defers (never blocks) and a
background thread registers once an address appears and re-registers if
it later changes.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, Optional

from zeroconf import Error as ZeroconfError
from zeroconf import ServiceInfo, Zeroconf

import aprilcam

__all__ = ["MdnsAdvertiser"]

logger = logging.getLogger(__name__)

#: The service type every AprilCam daemon advertises under. Matches v1
#: exactly -- ticket 004's client-side browse filters on this string.
_SERVICE_TYPE = "_aprilcam._tcp.local."

#: A non-routable TEST-NET-1 address (RFC 5737) -- see
#: :func:`_primary_routable_ipv4` for why a UDP socket is "connected"
#: to it without ever sending a packet.
_PROBE_ADDRESS = ("192.0.2.1", 53)


def _primary_routable_ipv4() -> Optional[str]:
    """Return this host's primary routable IPv4 address, or ``None``.

    **Earned knowledge, kept from v1 in spirit.** Uses the UDP-socket
    trick: connect a ``SOCK_DGRAM`` socket to a non-routable TEST-NET-1
    address. No packet is ever sent (UDP ``connect()`` only picks a
    route and a local source address); reading ``getsockname()``
    afterward gives the OS's chosen outbound interface address. This
    deliberately avoids resolving ``socket.gethostname()`` via
    ``/etc/hosts``, which on Ubuntu commonly maps the hostname to
    ``127.0.1.1``.

    Returns ``None`` -- never a loopback address -- if the trick fails
    (no route yet, e.g. the network is still coming up) or resolves to
    a loopback address anyway. Callers must treat ``None`` as "not
    advertisable yet", not substitute ``127.0.0.1``.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(_PROBE_ADDRESS)
            addr = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return None
    if addr and not addr.startswith("127.") and addr != "0.0.0.0":
        return addr
    return None


class MdnsAdvertiser:
    """Advertises the daemon's control plane on the local network via
    mDNS/Bonjour once it has a real TCP port and a routable address.

    Usage (mirrors ``daemon/__main__.py``'s wiring of this into
    ``DaemonLifecycle``'s ``advertise``/``stop_advertise`` hooks)::

        advertiser = MdnsAdvertiser()
        advertiser.start(tcp_port)
        # ... daemon runs ...
        advertiser.stop()

    Parameters
    ----------
    zeroconf_factory:
        Zero-argument callable returning a fresh ``Zeroconf`` instance.
        Defaults to the real ``Zeroconf`` class itself. Tests substitute
        a fake here.
    address_lookup:
        Zero-argument callable returning the routable IPv4 address or
        ``None``. Defaults to :func:`_primary_routable_ipv4`.
    retry_interval:
        Seconds between background address polls (default 5.0).
    wait:
        ``wait(seconds) -> bool`` returning ``True`` when the advertiser
        is stopping. Defaults to the stop event's ``wait``; tests inject
        a fake to drive time without sleeping.
    """

    def __init__(
        self,
        *,
        zeroconf_factory: Callable[[], Zeroconf] = Zeroconf,
        address_lookup: Callable[[], Optional[str]] = _primary_routable_ipv4,
        retry_interval: float = 5.0,
        wait: Optional[Callable[[float], bool]] = None,
    ) -> None:
        self._zeroconf_factory = zeroconf_factory
        self._address_lookup = address_lookup
        self._retry_interval = retry_interval
        self._stop_event = threading.Event()
        self._wait = wait if wait is not None else self._stop_event.wait
        self._lock = threading.RLock()
        self._zeroconf: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None
        self._address: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._tcp_port = 0
        self._hostname = ""
        self._service_name = ""

    def start(self, tcp_port: int, *, hostname: Optional[str] = None) -> None:
        """Register a ``_aprilcam._tcp.local.`` record advertising
        *tcp_port*, or defer until a routable address exists.

        Never blocks waiting for a network: if no routable IPv4 address
        is available, a daemon thread polls at ``retry_interval`` and
        registers when one appears, then re-registers if it changes.

        *hostname* overrides ``socket.gethostname()`` for the TXT
        ``host`` property (used by the two-local-daemon integration
        test so the client self-filter can tell the processes apart).
        TXT keys are exactly ``version`` and ``host`` -- do not rename
        or add to them; the client self-filter keys on ``host``.

        Every zeroconf failure is logged at WARNING and never
        propagates (see the module docstring).
        """
        with self._lock:
            self._tcp_port = tcp_port
            self._hostname = hostname if hostname is not None else socket.gethostname()
            self._service_name = f"aprilcam-{self._hostname}.{_SERVICE_TYPE}"
            self._stop_event.clear()
            addr = self._safe_lookup()
            if addr is None:
                logger.info(
                    "mdns: no routable IPv4 yet; deferring advertisement of %s on port %d "
                    "(retrying every %.1fs)",
                    self._service_name,
                    tcp_port,
                    self._retry_interval,
                )
            else:
                self._register(addr)
            self._thread = threading.Thread(
                target=self._run, name="aprilcam-mdns-retry", daemon=True
            )
            self._thread.start()

    def _safe_lookup(self) -> Optional[str]:
        try:
            addr = self._address_lookup()
        except OSError:
            return None
        # Defence in depth: never let a loopback address through.
        if not addr or addr.startswith("127."):
            return None
        return addr

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._wait(self._retry_interval):
                return
            if self._stop_event.is_set():
                return
            addr = self._safe_lookup()
            with self._lock:
                if self._stop_event.is_set() or addr is None or addr == self._address:
                    continue
                if self._info is not None:
                    logger.info(
                        "mdns: address changed %s -> %s; re-registering", self._address, addr
                    )
                    self._unregister()
                    self._close_zeroconf()
                self._register(addr)

    def _register(self, addr: str) -> None:
        """Register (or re-register) with *addr*. Caller holds the lock."""
        zeroconf = self._zeroconf
        created = False
        try:
            info = ServiceInfo(
                type_=_SERVICE_TYPE,
                name=self._service_name,
                port=self._tcp_port,
                properties={
                    b"version": aprilcam.__version__.encode(),
                    b"host": self._hostname.encode(),
                },
                addresses=[socket.inet_aton(addr)],
            )
            if zeroconf is None:
                zeroconf = self._zeroconf_factory()
                created = True
            zeroconf.register_service(info)
        except (OSError, ZeroconfError) as exc:
            logger.warning(
                "mdns: could not advertise %s on port %d (%s) -- daemon continues "
                "without mDNS advertisement",
                self._service_name,
                self._tcp_port,
                exc,
            )
            if created and zeroconf is not None:
                try:
                    zeroconf.close()
                except (OSError, ZeroconfError):
                    pass
            return
        self._zeroconf = zeroconf
        self._info = info
        self._address = addr
        logger.info(
            "mdns: advertising %s at %s:%d", self._service_name, addr, self._tcp_port
        )

    def _unregister(self) -> None:
        """Unregister the current record (keeps Zeroconf open). Caller holds the lock."""
        if self._zeroconf is not None and self._info is not None:
            try:
                self._zeroconf.unregister_service(self._info)
            except (OSError, ZeroconfError) as exc:
                logger.warning("mdns: error while unregistering advertisement: %s", exc)
        self._info = None
        self._address = None

    def _close_zeroconf(self) -> None:
        """Close and drop the Zeroconf instance so the next registration
        builds a fresh one that re-enumerates interfaces (its sockets are
        bound at construction). Caller holds the lock."""
        if self._zeroconf is not None:
            try:
                self._zeroconf.close()
            except (OSError, ZeroconfError) as exc:
                logger.warning("mdns: error while closing zeroconf: %s", exc)
        self._zeroconf = None

    def stop(self) -> None:
        """Stop the retry thread, unregister and close. Safe to call
        when never started, still deferred, or already stopped."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None
        with self._lock:
            if self._zeroconf is None:
                return
            try:
                if self._info is not None:
                    self._zeroconf.unregister_service(self._info)
                self._zeroconf.close()
            except (OSError, ZeroconfError) as exc:
                logger.warning("mdns: error while stopping advertisement: %s", exc)
            finally:
                self._zeroconf = None
                self._info = None
                self._address = None
