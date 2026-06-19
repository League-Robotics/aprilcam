"""aprilcam.client.discovery — mDNS discovery and daemon target resolution.

Provides:
- ``DaemonInfo``: dataclass describing a discovered AprilCam daemon.
- ``discover_daemons(timeout)``: browse ``_aprilcam._tcp.local.`` using
  zeroconf, returning all found daemons.  Degrades gracefully (returns
  ``[]``) when the ``zeroconf`` package is not installed.
- ``resolve_daemon_target(config, cli_args)``: four-level precedence
  resolver that returns ``(host, port, unix_path_or_None)``:

  1. Explicit CLI flag (``cli_args.daemon_host``)
  2. ``APRILCAM_DAEMON_HOST`` / ``config.daemon_host``
  3. Local Unix socket probe (never spawns a process)
  4. mDNS browse: 1 result → auto-select; >1 → error; 0 → hard error
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aprilcam.errors import DaemonNotFoundError

if TYPE_CHECKING:
    from aprilcam.config import Config

_SERVICE_TYPE = "_aprilcam._tcp.local."

_NO_DAEMON_MSG = (
    "No aprilcam daemon found — start one "
    "(`systemctl start aprilcamd` / `aprilcam daemon start`) "
    "or set APRILCAM_DAEMON_HOST."
)


# ---------------------------------------------------------------------------
# DaemonInfo
# ---------------------------------------------------------------------------


@dataclass
class DaemonInfo:
    """Metadata for a discovered AprilCam daemon service record."""

    name: str
    host: str
    port: int
    addresses: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# mDNS discovery
# ---------------------------------------------------------------------------


def discover_daemons(timeout: float = 1.0) -> list[DaemonInfo]:
    """Browse ``_aprilcam._tcp.local.`` and return discovered daemons.

    Uses ``zeroconf.ServiceBrowser`` to collect service records for the
    given *timeout* seconds.  If the ``zeroconf`` package is not installed,
    returns an empty list (graceful degradation).

    Args:
        timeout: How long (seconds) to listen for mDNS announcements.

    Returns:
        List of :class:`DaemonInfo` instances, one per discovered daemon.
        May be empty.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf  # type: ignore[import]
    except ImportError:
        return []

    results: list[DaemonInfo] = []

    def _on_service_state_change(
        zeroconf_instance: "Zeroconf",
        service_type: str,
        name: str,
        state_change: "ServiceStateChange",
    ) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        info = zeroconf_instance.get_service_info(service_type, name)
        if info is None:
            return

        # Resolve host from TXT record 'host' property, fallback to reverse-lookup
        raw_host = (info.properties or {}).get(b"host")
        if raw_host and isinstance(raw_host, bytes):
            host = raw_host.decode(errors="replace")
        else:
            # Try to convert raw address bytes to a dotted string
            addrs = info.parsed_addresses()
            if addrs:
                host = addrs[0]
            else:
                host = name  # last resort

        # Collect string-form IP addresses
        addresses: list[str] = list(info.parsed_addresses())

        results.append(
            DaemonInfo(
                name=name,
                host=host,
                port=int(info.port),
                addresses=addresses,
            )
        )

    zc = Zeroconf()
    try:
        _browser = ServiceBrowser(zc, _SERVICE_TYPE, handlers=[_on_service_state_change])
        time.sleep(timeout)
    finally:
        zc.close()

    return results


# ---------------------------------------------------------------------------
# Unix socket probe
# ---------------------------------------------------------------------------


def _probe_unix(path: str) -> bool:
    """Return True if a Unix-domain gRPC socket at *path* accepts connections.

    This is a pure connectivity probe — no data is sent, no daemon is spawned.
    Returns ``False`` on any error (file not found, permission denied, etc.).
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.connect(path)
            return True
        except OSError:
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_daemon_target(
    config: "Config",
    cli_args=None,
) -> tuple[str, int, str | None]:
    """Resolve the target (host, port, unix_path) for the AprilCam daemon.

    Precedence (highest wins):

    1. ``cli_args.daemon_host`` / ``cli_args.daemon_port`` — explicit CLI flags.
    2. ``config.daemon_host`` / ``config.daemon_port`` — from ``APRILCAM_DAEMON_HOST``
       / ``APRILCAM_DAEMON_PORT`` env vars (or dotfiles).
    3. Local Unix socket probe — if the default control socket is reachable,
       returns ``("localhost", config.daemon_port, unix_path)``.
       **Never spawns a process.**
    4. mDNS browse on ``_aprilcam._tcp.local.``:

       - Exactly 1 result → auto-select.
       - >1 results → raises :class:`~aprilcam.errors.DaemonNotFoundError`
         with a list of discovered hosts, directing the user to set
         ``APRILCAM_DAEMON_HOST``.
       - 0 results → raises :class:`~aprilcam.errors.DaemonNotFoundError`
         with instructions to start the daemon or set the env var.

    Args:
        config: Loaded :class:`~aprilcam.config.Config` instance.
        cli_args: Argparse namespace (or any object) with optional
            ``daemon_host`` and ``daemon_port`` attributes.  May be ``None``.

    Returns:
        ``(host, port, unix_path_or_None)`` — a 3-tuple where the third
        element is the Unix socket path when a local socket is used, or
        ``None`` for TCP connections.

    Raises:
        DaemonNotFoundError: When no reachable daemon is found via any
            resolution method.
    """
    # --- Priority 1: explicit CLI flag --daemon-host -----------------------
    if cli_args is not None:
        cli_host = getattr(cli_args, "daemon_host", None)
        if cli_host:
            cli_port = getattr(cli_args, "daemon_port", None) or config.daemon_port
            return (cli_host, int(cli_port), None)

    # --- Priority 2: env / config field APRILCAM_DAEMON_HOST ---------------
    if config.daemon_host:
        return (config.daemon_host, config.daemon_port, None)

    # --- Priority 3: local Unix socket probe --------------------------------
    unix_path = str(config.socket_dir / "control.sock")
    if _probe_unix(unix_path):
        return ("localhost", config.daemon_port, unix_path)

    # --- Priority 4: mDNS browse -------------------------------------------
    found = discover_daemons()
    if len(found) == 1:
        d = found[0]
        return (d.host, d.port, None)
    if len(found) > 1:
        names = ", ".join(
            f"{d.host}:{d.port}" for d in found
        )
        raise DaemonNotFoundError(
            f"Multiple aprilcam daemons found ({names}). "
            "Set APRILCAM_DAEMON_HOST to select one."
        )

    raise DaemonNotFoundError(_NO_DAEMON_MSG)
