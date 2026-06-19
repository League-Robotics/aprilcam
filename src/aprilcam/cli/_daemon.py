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
    """Add ``--daemon-host`` and ``--daemon-port`` to *parser*.

    These flags override ``APRILCAM_DAEMON_HOST`` / ``APRILCAM_DAEMON_PORT``
    and all auto-discovery methods.

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
        "--daemon-host",
        default=None,
        metavar="HOST",
        dest="daemon_host",
        help=(
            "Hostname or IP of the AprilCam daemon. "
            "Overrides APRILCAM_DAEMON_HOST and mDNS discovery."
        ),
    )
    group.add_argument(
        "--daemon-port",
        type=int,
        default=None,
        metavar="PORT",
        dest="daemon_port",
        help="TCP port the daemon listens on (default: 5280).",
    )
    return group


def connect_from_args(config: "Config", args) -> "DaemonControl":
    """Resolve the daemon target and return a connected :class:`DaemonControl`.

    Calls :func:`~aprilcam.client.discovery.resolve_daemon_target` with
    the parsed *args* namespace, then opens a gRPC channel and performs
    a probe ``ListCameras`` call to confirm reachability.

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

    host, port, unix_path = resolve_daemon_target(config, args)

    dc = DaemonControl(
        unix_path=unix_path,
        host=host,
        port=port,
    )
    dc.connect()
    # Probe to verify the daemon is actually responding
    dc.list_cameras()
    return dc
