"""CLI command: aprilcam tags — list all tags detected by the daemon camera."""

from __future__ import annotations

import argparse
import math
from typing import List, Optional


def _print_table(
    title: str,
    rows: List[dict],
    has_world: bool,
) -> None:
    """Print a formatted table for one tag family."""
    print(title)

    if not rows:
        print("  (none detected)\n")
        return

    # Column headers
    if has_world:
        hdr = f"  {'ID':>5}  {'px_x':>8}  {'px_y':>8}  {'wx_cm':>8}  {'wy_cm':>8}  {'yaw_deg':>8}"
        sep = (
            "  " + "-" * 5 + "  " + "-" * 8 + "  " + "-" * 8
            + "  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 8
        )
    else:
        hdr = f"  {'ID':>5}  {'px_x':>8}  {'px_y':>8}  {'yaw_deg':>8}"
        sep = "  " + "-" * 5 + "  " + "-" * 8 + "  " + "-" * 8 + "  " + "-" * 8

    print(hdr)
    print(sep)
    for r in rows:
        yaw_deg = math.degrees(r.get("yaw", 0.0))
        if has_world and r.get("wx") is not None:
            print(
                f"  {r['label']:>5}  {r['px'][0]:8.1f}  {r['px'][1]:8.1f}"
                f"  {r['wx']:8.1f}  {r['wy']:8.1f}  {yaw_deg:8.1f}"
            )
        else:
            wx_col = "  {:>8}  {:>8}".format("—", "—") if has_world else ""
            print(
                f"  {r['label']:>5}  {r['px'][0]:8.1f}  {r['px'][1]:8.1f}{wx_col}"
                f"  {yaw_deg:8.1f}"
            )
    print()


def main(argv: Optional[List[str]] = None) -> int:
    """Detect and list all tags visible to the daemon camera.

    Connects to the running AprilCam daemon via gRPC (using the shared
    ``--daemon-host``/``--daemon-port`` flags) and calls ``OpenCamera`` +
    ``GetTags`` RPC.  No local ``cv.VideoCapture`` is opened — the daemon is
    the sole camera owner.
    """
    from ..cli._daemon import add_daemon_args, connect_from_args, resolve_camera_code
    from ..config import Config
    from ..camera.identity import resolve_all
    from ..camera.registry import (
        CameraRegistry,
        CameraSelectError,
        resolve_enum_to_index,
    )
    from ..camera.camutil import list_cameras, select_camera_by_pattern

    parser = argparse.ArgumentParser(
        prog="aprilcam tags",
        description=(
            "List ArUco and AprilTag markers detected by the daemon camera. "
            "The daemon must be running (`aprilcam daemon start`). "
            "CAMERA accepts a camera number, name pattern, or an alpha code "
            "(e.g. 'A' for local camera 1, 'FB' for host F camera B)."
        ),
    )
    parser.add_argument(
        "camera", metavar="CAMERA",
        help=(
            "Camera number, name pattern, or alpha code "
            "(e.g. 'A', 'FB' — run 'aprilcam probe' for codes)"
        ),
    )
    add_daemon_args(parser)
    args = parser.parse_args(argv)

    config = Config.load()

    # --- Try alpha-code resolution first ------------------------------------
    # If CAMERA is a 1- or 2-letter code (e.g. "A", "FB"), resolve it via the
    # host store.  This also patches args.daemon_host to reach the right host.
    code_result = resolve_camera_code(args.camera, config, args)
    if code_result is not None:
        _resolved_host, cam_index = code_result
        # cam_index here is already the enum handle — connect and open directly.
        try:
            dc = connect_from_args(config, args)
        except Exception as exc:
            print(f"ERROR: could not connect to daemon: {exc}")
            return 1
        try:
            cam_name, _camera_dir = dc.open_camera(cam_index)
            tag_frame = dc.get_tags(cam_name)
        except Exception as exc:
            print(f"ERROR: daemon RPC failed: {exc}")
            dc.close()
            return 1
        finally:
            dc.close()
        tags = tag_frame.tags
        # Fall through to printing logic below.
    else:
        # --- Resolve the camera argument to a device index (legacy path) ------
        # We do this first so we can give a clear error before connecting to the
        # daemon.
        try:
            enum_no = int(args.camera)
            registry = CameraRegistry(config.cameras_dir)
            try:
                cam_index = resolve_enum_to_index(enum_no, registry, resolve_all())
            except CameraSelectError as exc:
                print(f"ERROR: {exc}")
                return 1
        except ValueError:
            # Not a number — treat it as a name pattern against connected cameras.
            cam_index = select_camera_by_pattern(args.camera, list_cameras(quiet=True))
            if cam_index is None:
                print(f"ERROR: no connected camera matched '{args.camera}'")
                return 1

        # Connect to the daemon — no local VideoCapture, the daemon owns the camera.
        try:
            dc = connect_from_args(config, args)
        except Exception as exc:
            print(f"ERROR: could not connect to daemon: {exc}")
            return 1

        try:
            cam_name, _camera_dir = dc.open_camera(cam_index)
            tag_frame = dc.get_tags(cam_name)
        except Exception as exc:
            print(f"ERROR: daemon RPC failed: {exc}")
            dc.close()
            return 1
        finally:
            dc.close()

        tags = tag_frame.tags

    total = len(tags)
    has_world = any(t.world_xy is not None for t in tags)

    print(f"\nCamera '{cam_name}' — {total} tag(s) detected via daemon\n")

    # Separate ArUco (negative id in legacy encoding; positive in TagRecord)
    # from AprilTags.  In the daemon protocol TagRecord.id is always the
    # positive marker id (ArUco uses a different field/family string).
    # We rely on `tag_record.family` when available; fall back to id sign.
    aruco_rows: list[dict] = []
    april_rows: list[dict] = []

    for t in tags:
        family = getattr(t, "family", "") or ""
        px = list(t.center_px) if t.center_px else [0.0, 0.0]
        wxy = t.world_xy
        row: dict = {
            "label": str(t.id),
            "px": px,
            "wx": float(wxy[0]) if wxy else None,
            "wy": float(wxy[1]) if wxy else None,
            "yaw": getattr(t, "yaw", 0.0) or 0.0,
        }
        if "aruco" in family.lower() or "4x4" in family.lower():
            aruco_rows.append(row)
        else:
            april_rows.append(row)

    # Sort by numeric id
    aruco_rows.sort(key=lambda r: int(r["label"]) if r["label"].lstrip("-").isdigit() else 0)
    april_rows.sort(key=lambda r: int(r["label"]) if r["label"].lstrip("-").isdigit() else 0)

    world_note = " (no calibration — world coords unavailable)" if not has_world else " (cm)"

    _print_table(f"ArUco 4x4{world_note}:", aruco_rows, has_world)
    _print_table(f"AprilTag 36h11{world_note}:", april_rows, has_world)

    return 0
