from __future__ import annotations

import argparse
from typing import List, Optional

from ..config import AppConfig, Config
from ..client.control import DaemonControl
from ._daemon import add_daemon_args, connect_from_args


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cameras",
        description=(
            "List cameras available on the daemon host. The daemon probes the "
            "hardware; no local camera probe is performed by this command. "
            "Use `aprilcam daemon start` to ensure the daemon is running."
        ),
    )
    parser.add_argument(
        "--pattern",
        type=str,
        help="Pattern to match camera name (overrides .env CAMERA)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Show extra details (slug) alongside each camera",
    )
    add_daemon_args(parser)
    args = parser.parse_args(argv)

    # Attempt to read .env for CAMERA pattern; tolerant to missing guard
    pattern = None
    try:
        cfg = AppConfig.load()
        pattern = cfg.env.get("CAMERA")
    except Exception:
        pass
    if args.pattern:
        pattern = args.pattern

    # Enumerate cameras via the daemon — no local hardware probe
    try:
        config = Config.load()
        dc = connect_from_args(config, args)
        devices = dc.enumerate_cameras()
    except Exception as exc:
        print(f"Error: could not contact daemon: {exc}")
        print("Make sure the daemon is running: aprilcam daemon start")
        return 1

    # The bracketed number is the PERSISTENT enumeration handle (stable across
    # plug/unplug) — the same number accepted by ``aprilcam view/tags/calibrate``
    # and shown in the live view. The unstable OS probe index is only shown
    # under --details for debugging.
    print("Cameras:")
    if devices:
        for dev in devices:
            num = dev.enum or dev.index
            if args.details:
                print(
                    f"  [{num}] {dev.name}  (slug: {dev.slug}, os-index: {dev.index})"
                )
            else:
                print(f"  [{num}] {dev.name}")
    else:
        print("  (none found)")

    # Pattern match on name if requested
    if pattern:
        matched = [d for d in devices if pattern.lower() in d.name.lower()]
        if matched:
            m = matched[0]
            print(
                f"Matched pattern '{pattern}': camera {m.enum or m.index} ({m.name})"
            )
        else:
            print(f"No camera matched pattern '{pattern}'.")

    return 0
