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
"""

from __future__ import annotations

import logging
import socket
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


def _primary_routable_ipv4() -> str:
    """Return this host's primary routable IPv4 address.

    **Earned knowledge, kept from v1 verbatim in spirit.** Uses the
    UDP-socket trick: connect a ``SOCK_DGRAM`` socket to a non-routable
    TEST-NET-1 address. No packet is ever sent (UDP ``connect()`` only
    picks a route and a local source address); reading
    ``getsockname()`` afterward gives the OS's chosen outbound
    interface address. This deliberately avoids resolving
    ``socket.gethostname()`` to an address via ``/etc/hosts``, which on
    Ubuntu commonly maps the hostname to ``127.0.1.1`` -- a loopback
    alias that would make the daemon's mDNS advertisement point nowhere
    reachable for a genuine remote client (the exact bug this function
    exists to avoid; see the sprint 028 ticket 003 description).

    Falls back to ``"127.0.0.1"`` if the trick itself fails (no network
    interface at all, e.g. a fully offline sandbox) or resolves to a
    loopback address anyway.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(_PROBE_ADDRESS)
            addr = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return "127.0.0.1"
    if addr and not addr.startswith("127."):
        return addr
    return "127.0.0.1"


class MdnsAdvertiser:
    """Advertises the daemon's control plane on the local network via
    mDNS/Bonjour once it has a real TCP port to announce.

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
        a fake here (mirroring ``client.discovery.Discovery``'s
        ``lock_holder``/``socket_reachable`` seam) so
        register/unregister/failure-swallowing behaviour is verifiable
        without a real multicast stack.
    """

    def __init__(self, *, zeroconf_factory: Callable[[], Zeroconf] = Zeroconf) -> None:
        self._zeroconf_factory = zeroconf_factory
        self._zeroconf: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None

    def start(self, tcp_port: int, *, hostname: Optional[str] = None) -> None:
        """Register a ``_aprilcam._tcp.local.`` service record
        advertising *tcp_port*.

        Parameters
        ----------
        tcp_port:
            The TCP port the gRPC control plane is listening on
            (``daemon.rpc.build_server``'s bound port, as reported
            through ``ControlPlaneListener.start()`` and
            ``DaemonLifecycle``'s ``advertise`` hook).
        hostname:
            Overrides ``socket.gethostname()`` for the TXT ``host``
            property below. ``None`` (the default) is the real
            hostname -- what every genuine daemon process wants.

            **This parameter is not dead code.** It exists specifically
            for ticket 004's two-local-daemon-alternate-port
            integration test, which proves the advertise/browse/
            connect mechanism end to end using two real daemon
            *processes* on one physical dev machine standing in for
            two hosts (this sprint's documented hardware-blocked
            stand-in, see sprint.md's Hardware Constraints). Both
            processes share one real ``socket.gethostname()`` value.
            Ticket 004's client-side self-filter decides "is this
            advertisement my own local daemon's" by comparing a
            browsed entry's TXT ``host`` property against the
            *client's own* ``socket.gethostname()`` -- without this
            override, the second process's advertisement would carry
            the identical ``host`` value as the first, so the
            self-filter would incorrectly collapse it into "that's
            just my own local daemon" and silently discard the very
            second daemon that test exists to find. Passing a
            synthetic value here (e.g. ``"test-second-host"``) for the
            second process's ``MdnsAdvertiser.start()`` call is what
            makes the two processes distinguishable over mDNS at all.

        TXT record properties are exactly ``{b"version": ...,
        b"host": ...}`` -- **do not rename or add to these keys**.
        Ticket 004's client-side self-filter keys on ``host`` by name;
        a rename here would silently break that filter without either
        side raising an error.

        Every zeroconf call below is wrapped: a failure (socket error
        opening the multicast interface, a name collision, anything
        ``zeroconf`` itself raises) is logged at WARNING and never
        propagates -- see the module docstring's "Advertising is never
        load-bearing".
        """
        resolved_hostname = hostname if hostname is not None else socket.gethostname()
        service_name = f"aprilcam-{resolved_hostname}.{_SERVICE_TYPE}"
        zeroconf: Optional[Zeroconf] = None
        try:
            addr_bytes = socket.inet_aton(_primary_routable_ipv4())
            info = ServiceInfo(
                type_=_SERVICE_TYPE,
                name=service_name,
                port=tcp_port,
                properties={
                    b"version": aprilcam.__version__.encode(),
                    b"host": resolved_hostname.encode(),
                },
                addresses=[addr_bytes],
            )
            zeroconf = self._zeroconf_factory()
            zeroconf.register_service(info)
        except (OSError, ZeroconfError) as exc:
            logger.warning(
                "mdns: could not advertise %s on port %d (%s) -- daemon continues "
                "without mDNS advertisement",
                service_name,
                tcp_port,
                exc,
            )
            if zeroconf is not None:
                try:
                    zeroconf.close()
                except (OSError, ZeroconfError):
                    pass
            return
        self._zeroconf = zeroconf
        self._info = info
        logger.info("mdns: advertising %s on port %d", service_name, tcp_port)

    def stop(self) -> None:
        """Unregister the service record and close the ``Zeroconf``
        instance. Safe to call even if :meth:`start` was never called
        or failed partway (a quiet no-op, not an error) -- matches
        ``DaemonLifecycle.stop()``'s own "safe to call when not
        running" contract for the hook this method is wired to.
        """
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
