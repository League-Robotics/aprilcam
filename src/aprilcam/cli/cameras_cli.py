from __future__ import annotations

import argparse
from typing import List, Optional

from ..config import AppConfig, Config
from ..client.control import DaemonControl
from ..client.host_codes import code_for, find_host, load_store
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

    # Determine whether the connected daemon is local or remote from the
    # DaemonControl itself (the root --host flag is consumed into the
    # environment, so args.daemon_host is not reliable here), then find its
    # entry in the host store so we can show stable alpha codes.
    connected_host: Optional[str] = None
    is_remote = False
    if not getattr(dc, "_unix_path", None):
        ch = getattr(dc, "_host", None)
        if ch and ch not in ("localhost", "127.0.0.1"):
            connected_host = ch
            is_remote = True
    host_store_entry: Optional[dict] = None
    host_num: Optional[int] = None
    try:
        store = load_store(config)
        if is_remote:
            host_store_entry = find_host(store, host=connected_host)
        else:
            host_store_entry = next(
                (h for h in store.get("hosts", []) if h.get("kind") == "local"),
                None,
            )
        if host_store_entry is not None:
            host_num = host_store_entry.get("num")
    except Exception:
        pass

    def _camera_code(dev) -> Optional[str]:
        """Return the alpha code for *dev* from the store, or None."""
        if host_num is None or host_store_entry is None:
            return None
        slug = dev.slug
        cameras = host_store_entry.get("cameras", [])
        cam_entry = next((c for c in cameras if c.get("slug") == slug), None)
        if cam_entry is None:
            return None
        return code_for(host_num, cam_entry["num"], not is_remote)

    no_store_hint_shown = False

    # The bracketed number is the PERSISTENT enumeration handle (stable across
    # plug/unplug) — the same number accepted by ``aprilcam view/tags/calibrate``
    # and shown in the live view. The unstable OS probe index is only shown
    # under --details for debugging.
    print("Cameras:")
    if devices:
        for dev in devices:
            num = dev.enum or dev.index
            code = _camera_code(dev)
            code_str = f" {code}" if code else ""
            if args.details:
                print(
                    f"  [{num}]{code_str} {dev.name}  (slug: {dev.slug}, os-index: {dev.index})"
                )
            else:
                print(f"  [{num}]{code_str} {dev.name}")
            if code is None and not no_store_hint_shown:
                no_store_hint_shown = True
        if no_store_hint_shown:
            print("  (run 'aprilcam probe' for stable alpha codes)")
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
