"""Entry point for ``python -m aprilcam.daemon``.

Loads configuration from all sources, configures logging, and starts the DaemonServer.

Transport flags
---------------
--unix / --no-unix      Enable/disable the Unix domain socket transport (default: enabled).
--tcp  / --no-tcp       Enable/disable the TCP transport (default: enabled).
--tcp-port N            TCP port to bind (default: 5280).
--unix-path PATH        Filesystem path for the Unix socket
                        (default: <socket_dir>/control.sock).
"""

import argparse
import logging
import sys

from aprilcam.config import Config
from aprilcam.daemon.server import DaemonServer, _DEFAULT_TCP_PORT


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m aprilcam.daemon",
        description="AprilCam daemon",
    )
    parser.add_argument(
        "--unix",
        dest="unix_enabled",
        action="store_true",
        default=True,
        help="Enable Unix domain socket transport (default: enabled)",
    )
    parser.add_argument(
        "--no-unix",
        dest="unix_enabled",
        action="store_false",
        help="Disable Unix domain socket transport",
    )
    parser.add_argument(
        "--tcp",
        dest="tcp_enabled",
        action="store_true",
        default=True,
        help="Enable TCP transport (default: enabled)",
    )
    parser.add_argument(
        "--no-tcp",
        dest="tcp_enabled",
        action="store_false",
        help="Disable TCP transport",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=_DEFAULT_TCP_PORT,
        metavar="N",
        help=f"TCP port to bind (default: {_DEFAULT_TCP_PORT})",
    )
    parser.add_argument(
        "--unix-path",
        default=None,
        metavar="PATH",
        help="Unix socket path (default: <socket_dir>/control.sock)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    if not args.unix_enabled and not args.tcp_enabled:
        print(
            "error: at least one transport must be enabled (--unix or --tcp).",
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config.load()
    logging.basicConfig(
        level=config.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Default the Unix socket path to the configured socket_dir so the daemon,
    # its client probe, and the pidfile all agree (the hardcoded
    # _DEFAULT_UNIX_PATH only matched config.socket_dir before _default_dirs()
    # moved the runtime dir to $TMPDIR/aprilcam-<uid> on macOS).
    unix_path = args.unix_path or str(config.socket_dir / "control.sock")

    # Warm the libcamera camera list on the MAIN thread before the asyncio loop
    # / gRPC threads start. Enumerating later (via the `cam` subprocess) from
    # inside the running daemon gets the child reaped early by asyncio's
    # child-watcher and yields an empty list; priming here caches the fixed CSI
    # cameras for the process lifetime. No-op on non-libcamera hosts.
    try:
        from aprilcam.camera import libcam

        if libcam.backend_enabled():
            cams = libcam.list_cameras()
            logging.getLogger("aprilcam.daemon").info(
                "libcamera backend: %d camera(s) detected", len(cams)
            )
    except Exception:
        logging.getLogger("aprilcam.daemon").exception("libcamera warm-up failed")

    DaemonServer(
        config,
        unix_enabled=args.unix_enabled,
        tcp_enabled=args.tcp_enabled,
        unix_path=unix_path,
        tcp_port=args.tcp_port,
    ).run()


if __name__ == "__main__":
    main()
