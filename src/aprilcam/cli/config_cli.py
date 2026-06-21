"""`aprilcam config` — show the version and resolved configuration.

A pure-client command: it loads :class:`aprilcam.config.Config` (which reads
``/etc/aprilcam.env``, ``/etc/aprilcam/aprilcam.env``, ``~/.aprilcam``,
project ``.aprilcam``/``.env`` dotfiles, and ``APRILCAM_*`` environment
variables) and prints the effective values, so a user can see exactly where
the daemon and clients will read/write data, including the log directory.
No camera hardware or daemon stack is touched.
"""

from __future__ import annotations

import argparse
import json
from typing import List, Optional

from .. import cli as _cli  # reuse the shared version helper
from ..config import Config


def _collect(cfg: Config) -> dict:
    """Build an ordered mapping of config labels → string values for display."""
    return {
        "version": _cli._get_version(),
        "data_dir": str(cfg.data_dir),
        "cameras_dir": str(cfg.cameras_dir),
        "playfields_dir": str(cfg.playfields_dir),
        "calibration_dir": str(cfg.calibration_dir),
        "log_dir": str(cfg.log_dir),
        "socket_dir": str(cfg.socket_dir),
        "daemon_pidfile": str(cfg.daemon_pidfile),
        "env_dir": str(cfg.env_dir) if cfg.env_dir else "(none)",
        "log_level": cfg.log_level,
        "detection_fps": cfg.detection_fps,
        "static_deskew": cfg.static_deskew,
        "deskew_px_per_cm": cfg.deskew_px_per_cm,
        "undistort": cfg.undistort,
        "movement_threshold_px": cfg.movement_threshold_px,
    }


def main(argv: Optional[List[str]] = None) -> int:
    from ..config import CONFIG_VARS

    _rows = "\n".join(
        f"  {v['key']:<36}{v['default']:<32}{v['description']}" for v in CONFIG_VARS
    )
    epilog = (
        "environment variables (override the dotfiles above):\n"
        f"  {'VARIABLE':<36}{'DEFAULT':<32}DESCRIPTION\n"
        f"{_rows}"
    )
    parser = argparse.ArgumentParser(
        prog="config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Show the aprilcam version and the resolved configuration "
            "(data/socket/log directories, daemon pidfile, deskew settings, ...) "
            "as merged from /etc/aprilcam.env, /etc/aprilcam/aprilcam.env, "
            "~/.aprilcam, project .aprilcam/.env dotfiles, and "
            "APRILCAM_* environment variables."
        ),
        epilog=epilog,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the configuration as a JSON object instead of a table.",
    )
    parser.add_argument(
        "--vars",
        action="store_true",
        help="List all APRILCAM_* variables with defaults and descriptions.",
    )
    args = parser.parse_args(argv)

    if args.vars:
        for var in CONFIG_VARS:
            print(f"{var['key']:<40} {var['default']:<35} {var['description']}")
        return 0

    cfg = Config.load()
    data = _collect(cfg)

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title=f"aprilcam {data['version']}", show_header=False, box=None)
    table.add_column("key", style="bold cyan", justify="right")
    table.add_column("value")
    for key, value in data.items():
        if key == "version":
            continue
        table.add_row(key, str(value))
    console.print(table)
    return 0
