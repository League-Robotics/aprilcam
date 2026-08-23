from __future__ import annotations

import argparse
import os
from typing import List, Optional

from ..config import AppConfig, Config
from ..client.host_codes import code_for, find_host, load_store, num_to_alpha
from ._daemon import add_daemon_args, connect_from_args


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cameras",
        description=(
            "List cameras. With no --host, lists every camera on every daemon "
            "on the network recorded by 'aprilcam probe', addressed by its "
            "base-26 code: host-letter + camera-letter (local cameras need no "
            "host letter). With --host HOST, lists that one daemon's cameras "
            "live."
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

    config = Config.load()

    # An explicit --host (flag or APRILCAM_DAEMON_HOST, set by the root --host)
    # means "list that one daemon, live". With no host, show the whole network
    # from the host store that `aprilcam probe` builds.
    explicit_host = bool(
        getattr(args, "daemon_host", None) or os.environ.get("APRILCAM_DAEMON_HOST")
    )
    store = load_store(config)
    hosts = store.get("hosts", [])

    if not explicit_host and hosts:
        return _list_all_from_store(hosts, args.details, config)

    return _list_single_daemon(config, args)


def _live_slugs_for_host(config: Config, h: dict) -> Optional[set]:
    """Return the set of camera slugs the host's daemon reports live.

    Connects to the host's daemon (local Unix socket for the local host, TCP
    for a remote one) and enumerates its cameras *now*. Returns the set of
    live slugs, or ``None`` if the daemon could not be reached (so the caller
    can fall back to the stored list rather than hiding everything).
    """
    from ..client.control import DaemonControl
    from ..client.discovery import _probe_unix

    dc: Optional[DaemonControl] = None
    try:
        if h.get("kind") == "local":
            unix_path = str(config.socket_dir / "control.sock")
            if _probe_unix(unix_path):
                dc = DaemonControl(unix_path=unix_path)
            else:
                dc = DaemonControl(host="localhost", port=config.daemon_port)
        else:
            addr = h.get("host") or (h.get("addresses") or [""])[0]
            if not addr:
                return None
            dc = DaemonControl(host=addr, port=config.daemon_port)
        dc.connect()
        devices = dc.enumerate_cameras()
        return {d.slug for d in devices}
    except Exception:
        return None
    finally:
        if dc is not None:
            try:
                dc.close()
            except Exception:
                pass


def _list_all_from_store(hosts: List[dict], details: bool, config: Config) -> int:
    """List the cameras that are actually connected on every known daemon.

    Reads the persistent host store built by ``aprilcam probe`` for the stable
    base-26 codes, but reconciles each host against its *live* daemon so only
    cameras that are connected **right now** are shown — every code printed
    here can be opened with ``aprilcam view <code>``. Cameras in the store
    that the daemon no longer reports (unplugged since the last probe) are
    hidden. If a host's daemon can't be reached, its stored list is shown
    as-is with a note, rather than hiding everything.

    Each camera's code is produced by ``code_for``: a single letter for local
    cameras, or host-letter + camera-letter for remote ones.
    """
    print("Cameras (connected now; codes open with 'aprilcam view <code>'):")
    print()

    def _enum_key(c: dict) -> int:
        e = c.get("enum")
        return e if e is not None else (c.get("index") or 0)

    for h in sorted(hosts, key=lambda x: x.get("num", 0)):
        h_num = h.get("num", 0)
        h_letter = num_to_alpha(h_num) if h_num else "?"
        h_name = h.get("host", "?")
        is_local = h.get("kind") == "local"
        suffix = "  (local)" if is_local else ""

        live_slugs = _live_slugs_for_host(config, h)
        unreachable = live_slugs is None
        note = "  (daemon unreachable — showing last probe)" if unreachable else ""
        print(f"  {h_name}  [{h_letter}]{suffix}{note}")

        stored = sorted(h.get("cameras", []), key=_enum_key)
        # Show only cameras the daemon reports live (matched by stable slug),
        # unless the daemon is unreachable — then fall back to the stored list.
        if unreachable:
            cams = stored
        else:
            cams = [c for c in stored if c.get("slug") in live_slugs]

        if not cams:
            print("      (no cameras connected)" if not unreachable else "      (no cameras)")
        for c in cams:
            cam_num = c.get("num")
            code = code_for(h_num, cam_num, is_local) if cam_num is not None else "?"
            name = c.get("name", "")
            if details:
                enum = c.get("enum")
                if enum is None:
                    enum = c.get("index")
                print(
                    f"      {code:<6}{name}  (slug: {c.get('slug', '')}, enum: {enum})"
                )
            else:
                print(f"      {code:<6}{name}")
        print()

    print("Open one with:  aprilcam view <code>   (e.g. 'aprilcam view A')")
    return 0


def _list_single_daemon(config: Config, args: argparse.Namespace) -> int:
    """List cameras on a single daemon (live), with stable alpha codes.

    Used when ``--host`` is given (query that daemon) or when the host store is
    empty (no probe yet) — falls back to the local daemon. The bracketed number
    is the persistent enumeration handle accepted by ``view``/``tags``.
    """
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
