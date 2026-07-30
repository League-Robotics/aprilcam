"""CLI subcommand: aprilcam probe — discover daemons and build the host store.

Probes the local daemon (via Unix socket / localhost) and any daemons
announced via mDNS, enumerates cameras on each, and updates the persistent
host store (``<data_dir>/hosts.json``) with stable numeric assignments.

Prints a summary table showing each host's letter code, address, kind, and
the camera codes assigned to it.
"""

from __future__ import annotations

import socket
from typing import Optional

from ..config import Config
from ..client.host_codes import (
    code_for,
    load_store,
    merge_probe_results,
    save_store,
)


def _is_local_host(host: str, addresses: list[str]) -> bool:
    """Return True if *host* / *addresses* resolves to the local machine."""
    local_names = {"localhost", "127.0.0.1", "::1"}
    if host in local_names:
        return True
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip in addresses:
            return True
    except OSError:
        pass
    return False


def _probe_local(config: Config) -> Optional[dict]:
    """Probe the local daemon via Unix socket / localhost.

    Returns a probe dict (for :func:`merge_probe_results`) or ``None`` when
    no local daemon is reachable.
    """
    from ..client.control import DaemonControl
    from ..client.discovery import _probe_unix

    unix_path = str(config.socket_dir / "control.sock")
    has_unix = _probe_unix(unix_path)

    dc: Optional[DaemonControl] = None
    try:
        if has_unix:
            dc = DaemonControl(unix_path=unix_path)
        else:
            dc = DaemonControl(host="localhost", port=config.daemon_port)
        dc.connect()
        dc.list_cameras()  # probe connectivity
        devices = dc.enumerate_cameras()
    except Exception:
        return None
    finally:
        if dc is not None:
            try:
                dc.close()
            except Exception:
                pass

    cameras = [
        {
            "enum": d.enum if d.enum is not None else d.index,
            "index": d.index,
            "name": d.name,
            "slug": d.slug,
        }
        for d in devices
    ]

    try:
        local_hostname = socket.gethostname()
    except OSError:
        local_hostname = "localhost"

    return {
        "host": local_hostname,
        "addresses": ["127.0.0.1"],
        "kind": "local",
        "cameras": cameras,
    }


def _probe_remote(info) -> Optional[dict]:
    """Probe one mDNS-discovered daemon (``DaemonInfo``).

    Returns a probe dict or ``None`` on error.
    """
    from ..client.control import DaemonControl

    dc: Optional[DaemonControl] = None
    try:
        dc = DaemonControl(host=info.host, port=info.port)
        dc.connect()
        dc.list_cameras()
        devices = dc.enumerate_cameras()
    except Exception:
        return None
    finally:
        if dc is not None:
            try:
                dc.close()
            except Exception:
                pass

    cameras = [
        {
            "enum": d.enum if d.enum is not None else d.index,
            "index": d.index,
            "name": d.name,
            "slug": d.slug,
        }
        for d in devices
    ]

    return {
        "host": info.host,
        "addresses": list(info.addresses),
        "kind": "remote",
        "cameras": cameras,
    }


def _dedupe(results: list[dict]) -> list[dict]:
    """Remove duplicate probe results, preferring earlier entries."""
    seen_hosts: set[str] = set()
    seen_addrs: set[str] = set()
    out: list[dict] = []
    for r in results:
        h = r.get("host", "")
        addrs = set(r.get("addresses", []))
        if h in seen_hosts or (addrs & seen_addrs):
            continue
        seen_hosts.add(h)
        seen_addrs |= addrs
        out.append(r)
    return out


def _print_table(store: dict) -> None:
    """Print a summary table of the store after probing."""
    hosts = store.get("hosts", [])
    if not hosts:
        print("No daemons found.")
        return

    for h in hosts:
        h_num: int = h.get("num", 0)
        h_kind: str = h.get("kind", "remote")
        h_host: str = h.get("host", "?")
        is_local = h_kind == "local"

        cameras: list[dict] = h.get("cameras", [])
        cam_parts: list[str] = []
        for c in cameras:
            c_num: int = c.get("num", 0)
            c_name: str = c.get("name", "?")
            c_enum = c.get("enum")
            code = code_for(h_num, c_num, is_local)
            cam_parts.append(f"{code} {c_name} (enum {c_enum})")

        from ..client.host_codes import num_to_alpha
        h_letter = num_to_alpha(h_num)
        cam_str = ",  ".join(cam_parts) if cam_parts else "(no cameras)"
        print(f"{h_letter}  {h_host}  [{h_kind}]  ->  {cam_str}")


def main(argv: Optional[list[str]] = None) -> int:
    """Probe all reachable daemons and update the host store.

    Discovers the local daemon (Unix socket / localhost) and any mDNS-
    announced daemons, enumerates their cameras, merges results into the
    persistent host store (``<data_dir>/hosts.json``), and prints a table.
    """
    import argparse

    from ..client.discovery import discover_daemons

    parser = argparse.ArgumentParser(
        prog="aprilcam probe",
        description=(
            "Discover reachable AprilCam daemons (local + mDNS), enumerate "
            "their cameras, and update the host store for stable alpha codes."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        metavar="SECS",
        help="mDNS browse timeout in seconds (default: 1.0).",
    )
    args = parser.parse_args(argv)

    config = Config.load()

    print("Probing local daemon…")
    local_result = _probe_local(config)

    print(f"Browsing mDNS for {args.timeout:.1f}s…")
    mdns_infos = discover_daemons(timeout=args.timeout)

    results: list[dict] = []
    if local_result is not None:
        results.append(local_result)

    for info in mdns_infos:
        # Skip if this mDNS record describes the local machine.
        if _is_local_host(info.host, list(info.addresses)):
            # Still merge address info into the local entry if we have one.
            if local_result is not None:
                local_result["addresses"] = list(
                    set(local_result["addresses"]) | set(info.addresses)
                )
            continue
        r = _probe_remote(info)
        if r is not None:
            results.append(r)

    results = _dedupe(results)

    if not results:
        print("No reachable daemons found.")
        print("Start a daemon with: aprilcam daemon start")
        return 0

    # Load existing store, merge, save.
    store = load_store(config)
    store = merge_probe_results(store, results)
    save_store(config, store)

    print()
    _print_table(store)
    return 0
