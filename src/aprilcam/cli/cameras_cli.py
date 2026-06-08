from __future__ import annotations

import argparse
from typing import Dict, List, Optional

from ..camera.camutil import (
    CameraInfo,
    list_cameras,
    default_backends,
    select_camera_by_pattern,
    macos_avfoundation_device_names,
)
from ..camera.identity import CameraIdentity, resolve_identity
from ..camera.registry import CameraRecord, CameraRegistry
from ..config import AppConfig, Config


def _live_identity_by_uid(cams: List[CameraInfo]) -> Dict[str, CameraInfo]:
    """Map ``unique_id`` → live ``CameraInfo`` for the connected cameras.

    Each live camera's stable identity is resolved (using any identity fields
    already attached by ``list_cameras``, else re-resolving from the index) so
    it can be matched against persisted registry records by ``unique_id``.
    """
    by_uid: Dict[str, CameraInfo] = {}
    for cam in cams:
        uid = cam.unique_id
        if not uid:
            try:
                ident = resolve_identity(cam.index, name=cam.device_name or cam.name)
                uid = ident.unique_id
                cam.unique_id = uid
            except Exception:
                uid = None
        if uid:
            by_uid.setdefault(uid, cam)
    return by_uid


def _render_registry(
    records: List[CameraRecord], live_by_uid: Dict[str, CameraInfo]
) -> None:
    """Print every registered camera with its enumeration number.

    Connected cameras (those whose ``unique_id`` is in ``live_by_uid``) show
    their current OS index; previously-seen-but-disconnected cameras are
    rendered grayed-out (ANSI dim, via ``rich``) and marked offline. Records
    are ordered by enumeration number so numbers stay stable in the listing.
    """
    from rich.console import Console
    from rich.text import Text

    console = Console()

    def _sort_key(rec: CameraRecord):
        return (rec.enum is None, rec.enum if rec.enum is not None else 0)

    for rec in sorted(records, key=_sort_key):
        live = live_by_uid.get(rec.unique_id)
        enum = rec.enum if rec.enum is not None else "?"
        name = rec.name or rec.dir or rec.unique_id
        if live is not None:
            line = Text()
            line.append(f"  #{enum} ")
            line.append(f"[{live.index}] ", style="bold")
            line.append(str(name))
            console.print(line)
        else:
            line = Text(f"  #{enum} [offline] {name}", style="dim")
            console.print(line)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cameras",
        description=(
            "List cameras from the persistent registry. Connected cameras show "
            "their current OS index and enumeration number; previously-seen "
            "cameras that are now disconnected are shown grayed-out as offline "
            "while retaining their enumeration number. Identity is keyed on a "
            "stable hardware id; for cameras without a serial/UUID the id is "
            "derived from the USB location path, so moving such a camera to a "
            "different USB port may make it appear as a new camera. On macOS, "
            "AVFoundation is only probed for indices 0-1; use CAP_ANY to find "
            "additional devices."
        ),
    )
    parser.add_argument("--max-cams", type=int, default=10, help="Maximum camera indices to probe (default: 10)")
    parser.add_argument("--backend", type=str, choices=["auto", "avfoundation", "v4l2", "msmf", "dshow"], default="auto")
    parser.add_argument("--pattern", type=str, help="Pattern to match camera name (overrides .env CAMERA)")
    parser.add_argument("--quiet", action="store_true", default=True, help="Reduce OpenCV logging noise (default: on)")
    parser.add_argument("--verbose", action="store_true", help="Show OpenCV warnings during camera probing")
    parser.add_argument("--details", action="store_true", help="On macOS, use ffmpeg avfoundation names if available")
    parser.add_argument("--stop-after-failures", type=int, default=4, help="Per-backend consecutive failure cutoff to reduce noise (default 4)")
    args = parser.parse_args(argv)

    # Attempt to read .env for CAMERA pattern; tolerant to missing guard in this tool
    pattern = None
    try:
        cfg = AppConfig.load()
        pattern = cfg.env.get("CAMERA")
    except Exception:
        pass
    if args.pattern:
        pattern = args.pattern

    if args.verbose:
        args.quiet = False
    # Quiet logging if requested
    if args.quiet:
        try:
            import cv2 as cv
            if hasattr(cv, "utils") and hasattr(cv.utils, "logging"):
                cv.utils.logging.setLogLevel(cv.utils.logging.LOG_LEVEL_ERROR)
        except Exception:
            pass

    be_map = {
        "auto": None,
        "avfoundation": 1200,
        "v4l2": 200,
        "msmf": 1400,
        "dshow": 700,
    }
    be = be_map.get(args.backend)
    backends = default_backends() if be is None else [be]
    # For avfoundation, probing many indices can be noisy; if default, reduce
    max_probe = args.max_cams
    if args.backend == "avfoundation" and args.max_cams == 10:
        max_probe = 2

    cams = list_cameras(max_probe, backends=backends, stop_after_failures=int(args.stop_after_failures), quiet=bool(args.quiet), detailed_names=bool(args.details))
    live_by_uid = _live_identity_by_uid(cams)

    # Merge the live device list with the persistent registry so disconnected
    # cameras still appear (grayed out) with their stable enumeration numbers.
    records: List[CameraRecord] = []
    try:
        config = Config.load()
        registry = CameraRegistry(config.cameras_dir)
        # Register any connected camera not yet known so it gets an enum number.
        # Build the identity from the camera's already-resolved unique_id (the
        # same key used in ``live_by_uid``) so the new record matches the live
        # camera and is rendered as connected, not offline.
        for uid, cam in live_by_uid.items():
            if uid not in registry:
                try:
                    registry.resolve(
                        CameraIdentity(
                            unique_id=uid,
                            reason="cli_live",
                            is_fallback=False,
                            vid=cam.vid,
                            pid=cam.pid,
                            serial=cam.serial,
                            location=cam.location,
                            name=cam.device_name or cam.name,
                        )
                    )
                except Exception:
                    pass
        records = list(registry.records())
    except Exception:
        records = []

    print("Cameras:")
    if records:
        _render_registry(records, live_by_uid)
    else:
        # Fallback: no registry available — list the live devices directly.
        av_names = macos_avfoundation_device_names() if args.details else {}
        for c in cams:
            if av_names and c.backend == "AVFOUNDATION" and c.index in av_names:
                label = f"[{c.index}] {av_names[c.index]} (index {c.index}, AVFOUNDATION)"
            else:
                label = f"[{c.index}] {c.name}"
            print(f"  {label}")
        if not cams:
            print("  (none found)")

    # The pattern selector operates on connected cameras only.
    chosen = select_camera_by_pattern(pattern, cams) if pattern else None
    if chosen is not None:
        print(f"Suggested index by pattern '{pattern}': {chosen}")
    else:
        if pattern:
            print(f"No camera matched pattern '{pattern}'.")

    return 0
