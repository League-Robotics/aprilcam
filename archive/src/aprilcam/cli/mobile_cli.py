"""``aprilcam mobile`` — manage the daemon's mobile-tag registry.

A *mobile tag* is a tag mounted on a robot. You tell the daemon where the tag
sits relative to the robot's centre of rotation, and the daemon then reports the
robot centre (not the raw tag) for that id, and persists the registration.

    aprilcam mobile register <tag_id> [--x MM] [--y MM] [--z CM] [--yaw DEG] [--owner NAME]
    aprilcam mobile clear <tag_id>
    aprilcam mobile clear --all
    aprilcam mobile list
"""

from __future__ import annotations

import argparse
from typing import List, Optional

from ..config import Config
from ._daemon import add_daemon_args, connect_from_args


def _print_registry(rows: list) -> None:
    if not rows:
        print("  (no mobile tags registered)")
        return
    print(f"  {'TAG':>5}  {'X(mm)':>8} {'Y(mm)':>8} {'Z(cm)':>7} {'YAW(deg)':>9}  OWNER")
    for r in sorted(rows, key=lambda x: x["tag_id"]):
        print(
            f"  {r['tag_id']:>5}  {r['x_mm']:>8.1f} {r['y_mm']:>8.1f} "
            f"{r['z_cm']:>7.1f} {r['yaw_deg']:>9.1f}  {r['owner']}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mobile",
        description=(
            "Manage the daemon's mobile-tag registry. A mobile tag is mounted on "
            "a robot; register its pose relative to the robot's centre of "
            "rotation and the daemon reports the robot centre for that tag."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_reg = sub.add_parser("register", help="Register/replace a mobile tag's mount pose")
    p_reg.add_argument("tag_id", type=int, help="AprilTag id")
    p_reg.add_argument("--x", type=float, default=0.0, metavar="MM", help="tag forward of robot centre (mm)")
    p_reg.add_argument("--y", type=float, default=0.0, metavar="MM", help="tag left of robot centre (mm)")
    p_reg.add_argument("--z", type=float, default=0.0, metavar="CM", help="tag height above the playfield (cm)")
    p_reg.add_argument("--yaw", type=float, default=0.0, metavar="DEG", help="tag heading vs robot forward (deg)")
    p_reg.add_argument("--owner", type=str, default="", help="optional owner/robot label")
    add_daemon_args(p_reg)

    p_clr = sub.add_parser("clear", help="Remove one mobile tag, or all with --all")
    p_clr.add_argument("tag_id", type=int, nargs="?", help="AprilTag id to remove")
    p_clr.add_argument("--all", action="store_true", help="clear the entire registry")
    add_daemon_args(p_clr)

    p_lst = sub.add_parser("list", help="List registered mobile tags")
    add_daemon_args(p_lst)

    args = parser.parse_args(argv)
    config = Config.load()

    try:
        dc = connect_from_args(config, args)
    except Exception as exc:
        print(f"Error: could not contact daemon: {exc}")
        print("Make sure the daemon is running: aprilcam daemon start")
        return 1

    try:
        if args.action == "register":
            rows = dc.register_mobile_tag(
                args.tag_id, x_mm=args.x, y_mm=args.y, z_cm=args.z,
                yaw_deg=args.yaw, owner=args.owner,
            )
            print(f"Registered mobile tag {args.tag_id}. Registry:")
            _print_registry(rows)
        elif args.action == "clear":
            if args.all:
                rows = dc.clear_mobile_tags()
                print("Cleared all mobile tags.")
            elif args.tag_id is not None:
                rows = dc.clear_mobile_tag(args.tag_id)
                print(f"Cleared mobile tag {args.tag_id}.")
            else:
                print("Error: specify a tag_id or --all")
                return 1
            _print_registry(rows)
        elif args.action == "list":
            rows = dc.list_mobile_tags()
            print("Mobile tags:")
            _print_registry(rows)
    except Exception as exc:
        print(f"Error: daemon RPC failed: {exc}")
        return 1
    finally:
        dc.close()
    return 0
