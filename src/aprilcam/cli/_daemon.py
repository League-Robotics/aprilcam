"""aprilcam.cli._daemon — shared daemon-connection helpers for CLI commands.

Every CLI command that needs a :class:`~aprilcam.client.control.DaemonControl`
should import :func:`add_daemon_args` and :func:`connect_from_args` from here
instead of wiring its own argparse group and resolution logic.

Usage in a CLI command::

    from aprilcam.cli._daemon import add_daemon_args, connect_from_args

    def main(argv=None):
        parser = argparse.ArgumentParser(...)
        add_daemon_args(parser)
        args = parser.parse_args(argv)
        config = Config.load()
        dc = connect_from_args(config, args)
        # use dc …
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aprilcam.config import Config
    from aprilcam.client.control import DaemonControl


def add_daemon_args(parser: argparse.ArgumentParser) -> argparse._ArgumentGroup:
    """Add ``--host`` / ``--port`` (and ``--daemon-host`` / ``--daemon-port`` aliases) to *parser*.

    These flags override ``APRILCAM_DAEMON_HOST`` / ``APRILCAM_DAEMON_PORT``
    and all auto-discovery methods.  The ``--daemon-host`` / ``--daemon-port``
    long forms are kept as accepted aliases for backward compatibility; the
    ``dest`` names remain ``daemon_host`` / ``daemon_port`` so
    :func:`~aprilcam.client.discovery.resolve_daemon_target` requires no change.

    ``--host`` also accepts a single host-store letter (e.g. ``F``) which is
    resolved to the stored hostname/IP by
    :func:`~aprilcam.client.host_codes.resolve_host_token` inside
    :func:`connect_from_args`.

    Args:
        parser: The argparse parser (or sub-parser) to augment.

    Returns:
        The argument group that was added (rarely needed by callers).
    """
    group = parser.add_argument_group(
        "daemon connection",
        "Override the default daemon discovery (env vars, mDNS, Unix socket).",
    )
    group.add_argument(
        "--host",
        "--daemon-host",
        default=None,
        metavar="HOST",
        dest="daemon_host",
        help=(
            "Hostname, IP, or single-letter host code of the AprilCam daemon. "
            "Overrides APRILCAM_DAEMON_HOST and mDNS discovery. "
            "(--daemon-host is an accepted alias.)"
        ),
    )
    group.add_argument(
        "--port",
        "--daemon-port",
        type=int,
        default=None,
        metavar="PORT",
        dest="daemon_port",
        help="TCP port the daemon listens on (default: 5280). (--daemon-port is an accepted alias.)",
    )
    return group


def connect_from_args(config: "Config", args) -> "DaemonControl":
    """Resolve the daemon target and return a connected :class:`DaemonControl`.

    Calls :func:`~aprilcam.client.discovery.resolve_daemon_target` with
    the parsed *args* namespace, then opens a gRPC channel and performs
    a probe ``ListCameras`` call to confirm reachability.

    If ``args.daemon_host`` is a single uppercase letter, it is resolved
    to the stored hostname/IP via
    :func:`~aprilcam.client.host_codes.resolve_host_token` before
    discovery runs.

    Args:
        config: Loaded :class:`~aprilcam.config.Config` instance.
        args: Argparse namespace containing ``daemon_host`` and
            ``daemon_port`` attributes (added by :func:`add_daemon_args`).
            May also be any object with those attributes.

    Returns:
        A connected :class:`~aprilcam.client.control.DaemonControl`.

    Raises:
        DaemonNotFoundError: When no reachable daemon is found.
        grpc.RpcError: When the daemon is found but the probe RPC fails.
    """
    from aprilcam.client.control import DaemonControl
    from aprilcam.client.discovery import resolve_daemon_target
    from aprilcam.client.host_codes import load_store, resolve_host_token

    host, port, unix_path = resolve_daemon_target(config, args)

    # Resolve a single-letter host code (e.g. ``--host A`` or
    # ``APRILCAM_DAEMON_HOST=A``) to its stored hostname/IP. This runs on the
    # final resolved host, so it works regardless of whether the value came from
    # the flag, the environment (the root ``--host`` flag is consumed into the
    # environment), or a config file. It is a no-op for ordinary hostnames/IPs.
    if unix_path is None and host:
        try:
            host = resolve_host_token(host, load_store(config))
        except Exception:
            pass

    dc = DaemonControl(
        unix_path=unix_path,
        host=host,
        port=port,
    )
    dc.connect()
    # Probe to verify the daemon is actually responding
    dc.list_cameras()
    return dc


def resolve_camera_code(
    camera_arg: str,
    config: "Config",
    args,
) -> "tuple[str, int] | None":
    """Resolve a camera code (e.g. ``A``, ``FB``) to ``(daemon_host, cam_enum)``.

    If *camera_arg* is a 1- or 2-letter alpha string that matches a code in
    the host store, this function:

    - Returns ``(resolved_host, cam_enum)`` where *resolved_host* is the
      stored hostname/IP for the remote host (or an empty string for the
      local host), and *cam_enum* is the camera's enumeration number.
    - Sets ``args.daemon_host`` to the resolved host so that callers can
      pass *args* to :func:`connect_from_args` and reach the right daemon.

    Returns ``None`` when *camera_arg* is not a valid camera code, so callers
    can fall back to their existing (index/pattern) resolution logic.

    Args:
        camera_arg: The raw camera positional argument from the CLI.
        config: Loaded :class:`~aprilcam.config.Config`.
        args: Argparse namespace to patch with ``daemon_host`` on a match.

    Returns:
        ``(host, enum)`` on code match, or ``None`` if not a code.
    """
    from aprilcam.client.host_codes import load_store, resolve_code

    # Must be 1 or 2 alphabetic characters to be a code.
    s = camera_arg.strip()
    if not (1 <= len(s) <= 2 and s.isalpha()):
        return None

    try:
        store = load_store(config)
        host_entry, cam_entry = resolve_code(s.upper(), store)
    except (ValueError, Exception):
        return None

    # Determine the host address to reach this daemon.
    is_local = host_entry.get("kind") == "local"
    if is_local:
        resolved_host = ""  # use existing discovery (local unix / localhost)
    else:
        resolved_host = host_entry.get("host") or (
            host_entry.get("addresses", [""])[0]
        )

    cam_enum: int = cam_entry.get("enum") or cam_entry.get("index", 0)

    # Patch args so connect_from_args reaches the right daemon.
    if resolved_host:
        try:
            args.daemon_host = resolved_host
        except AttributeError:
            pass  # immutable namespace — caller must handle this.

    return (resolved_host, cam_enum)
