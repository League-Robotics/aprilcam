"""
aprilcam.daemon.mdns — MDNSAdvertiser: mDNS/Bonjour service advertisement.

Registers a ``_aprilcam._tcp.local.`` service record via the ``zeroconf``
library when the daemon starts with TCP transport enabled.  All zeroconf
calls are wrapped in try/except so that failures never crash the daemon.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

log = logging.getLogger(__name__)

_SERVICE_TYPE = "_aprilcam._tcp.local."


def _primary_routable_ipv4() -> str:
    """Return the primary routable IPv4 address of this host.

    Uses the UDP-socket trick — connect a SOCK_DGRAM socket to a non-routable
    TEST-NET-1 address (192.0.2.1).  No packets are sent; the OS selects the
    outbound interface and we read the local address from ``getsockname()``.
    This avoids resolving the hostname via ``/etc/hosts``, which on Ubuntu
    often maps the hostname to ``127.0.1.1`` (a loopback alias).

    Falls back to ``127.0.0.1`` on any error.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 53))
            addr = sock.getsockname()[0]
        finally:
            sock.close()
        # Skip loopback addresses (127.*) — they are not routable.
        if addr and not addr.startswith("127."):
            return addr
    except OSError:
        pass
    return "127.0.0.1"


class MDNSAdvertiser:
    """Advertise the AprilCam daemon on the local network via mDNS/Bonjour.

    Usage::

        advertiser = MDNSAdvertiser()
        advertiser.start(tcp_port=5280)
        # ... daemon runs ...
        advertiser.stop()

    All errors are caught and logged as warnings; they do not propagate.
    """

    def __init__(self) -> None:
        self._zeroconf: Optional[object] = None
        self._info: Optional[object] = None

    def start(self, tcp_port: int) -> None:
        """Register the mDNS service record.

        Parameters
        ----------
        tcp_port:
            The TCP port the gRPC server is listening on.
        """
        try:
            from zeroconf import ServiceInfo, Zeroconf  # type: ignore[import]

            hostname = socket.gethostname()
            service_name = f"aprilcam-{hostname}.{_SERVICE_TYPE}"

            # Determine the primary routable IPv4 address via the UDP-socket
            # trick rather than resolving the hostname.  On Ubuntu the hostname
            # often resolves to 127.0.1.1 (a /etc/hosts loopback alias) which
            # is not reachable from remote clients.
            addr_str = _primary_routable_ipv4()
            addr_bytes = socket.inet_aton(addr_str)
            log.debug("mDNS: advertising on IP %s", addr_str)

            self._info = ServiceInfo(
                type_=_SERVICE_TYPE,
                name=service_name,
                port=tcp_port,
                properties={b"version": b"1", b"host": hostname.encode()},
                addresses=[addr_bytes],
            )

            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(self._info)

            log.info(
                "mDNS: registered %s on port %d",
                service_name,
                tcp_port,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("mDNS: registration failed (daemon continues): %s", exc)
            self._zeroconf = None
            self._info = None

    def stop(self) -> None:
        """Unregister the mDNS service record and close the Zeroconf instance."""
        if self._zeroconf is None:
            return
        try:
            if self._info is not None:
                self._zeroconf.unregister_service(self._info)
            self._zeroconf.close()
            log.info("mDNS: service unregistered")
        except Exception as exc:  # noqa: BLE001
            log.warning("mDNS: error during stop: %s", exc)
        finally:
            self._zeroconf = None
            self._info = None
