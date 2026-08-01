"""``aprilcam camera list|probe|image`` (ticket 020-006).

Thin argv-to-client-call shells over ``client/discovery.py`` (ticket
020-005) -- ``adapters.md``'s "adapters hold no authoritative state,
adapters do no vision" rules apply throughout. Every RPC-shaped call
here is one line; the rest is argv shaping, output formatting, and
mapping typed ``aprilcam.errors`` values to exit codes.

**``camera image``'s destinations.** One frame, three destinations
(UC-17): a file (``-o FILE``), base64 to stdout (``--base64``), or raw
bytes to stdout (the default). Raw bytes refuse an attached terminal
unless ``--force`` is given (UC-17.S-4) -- checked, like an unwritable
``-o`` path, **before** any capture RPC runs, so a doomed destination
never costs a camera a frame it will not get to keep (UC-17: "error
before capture, not after").

**``camera image --all``.** ``cli.md``'s fan-out table lists
``camera image`` as fan-out-eligible, but only the base64 destination
has a sensible multi-daemon meaning: a single ``-o FILE`` cannot hold
more than one daemon's frame without the second write clobbering the
first, and concatenating raw binary from several daemons onto one
stdout stream is not a valid image in any format. ``--all`` combined
with either of those two destinations is therefore refused up front,
naming ``--base64`` as the way to fan out -- the documented exception
this ticket's brief asks for, not an oversight.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from aprilcam.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    add_all_option,
    add_daemon_option,
    fan_out,
    incomplete_command,
    print_cameras,
    print_probe_report,
    report_error,
)
from aprilcam.client.discovery import Discovery
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_list", "cmd_probe", "cmd_image"]


def cmd_list(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera list [-d HOST] [--all]``."""
    discovery = discovery_factory()
    if args.all:
        def render(label: str, cameras) -> None:
            print(f"{label}:")
            print_cameras(cameras)

        return fan_out(discovery, lambda daemon: daemon.list_cameras(), render)

    try:
        cameras = discovery.connect(args.daemon).list_cameras()
    except AprilCamError as exc:
        return report_error(exc)
    print_cameras(cameras)
    return EXIT_OK


def cmd_probe(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera probe [-d HOST] [--all]``."""
    discovery = discovery_factory()
    if args.all:
        def render(label: str, report) -> None:
            print(f"{label}:")
            print_probe_report(report)

        return fan_out(discovery, lambda daemon: daemon.probe_cameras(), render)

    try:
        report = discovery.connect(args.daemon).probe_cameras()
    except AprilCamError as exc:
        return report_error(exc)
    print_probe_report(report)
    return EXIT_OK


def _unwritable_reason(path: Path) -> Optional[str]:
    """Why *path* cannot be written, or ``None`` if it can -- checked
    before any capture RPC runs (see the module docstring)."""
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.exists():
        return f"directory does not exist: {parent}"
    if not os.access(parent, os.W_OK):
        return f"directory not writable: {parent}"
    if path.exists() and not os.access(path, os.W_OK):
        return f"path exists and is not writable: {path}"
    return None


def _cmd_image_all(
    args: argparse.Namespace, discovery_factory: Callable[[], Discovery]
) -> int:
    """``--all`` fan-out for ``camera image``. See the module docstring
    for why only the base64 destination is offered."""
    if args.output is not None:
        print(
            "error: --all cannot write to a single file (-o) -- each "
            "daemon's frame would overwrite the last. Use --base64 to "
            "fan out, or drop --all to target one daemon with -o.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if not args.base64:
        print(
            "error: --all has no sensible raw-bytes-to-stdout meaning "
            "(binary from multiple daemons concatenated is not a valid "
            "image). Use --base64 to fan out, or drop --all.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    discovery = discovery_factory()

    def render(label: str, frame) -> None:
        print(f"{label}: {base64.b64encode(frame.data).decode('ascii')}")

    return fan_out(
        discovery,
        lambda daemon: daemon.get_frame(args.camera, deskewed=False, encoding=args.encoding),
        render,
    )


def cmd_image(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera image <camera> [-o FILE | --base64] [-d HOST]
    [--all] [--encoding jpeg|png] [--force]``."""
    if args.all:
        return _cmd_image_all(args, discovery_factory)

    if args.output is not None:
        problem = _unwritable_reason(Path(args.output))
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_ERROR
    elif not args.base64 and not args.force and sys.stdout.isatty():
        print(
            "error: refusing to write raw image bytes to a terminal -- "
            "redirect stdout (e.g. '> frame.jpg'), pass --force, or use "
            "--base64 (UC-17.S-4)",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        frame = discovery_factory().connect(args.daemon).get_frame(
            args.camera, deskewed=False, encoding=args.encoding
        )
    except AprilCamError as exc:
        return report_error(exc)

    if args.output is not None:
        Path(args.output).write_bytes(frame.data)
        print(f"wrote {len(frame.data)} bytes to {args.output} ({frame.encoding})")
    elif args.base64:
        print(base64.b64encode(frame.data).decode("ascii"))
    else:
        sys.stdout.buffer.write(frame.data)
        sys.stdout.buffer.flush()
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``camera list|probe|image`` into the top-level parser."""
    parser = subparsers.add_parser(
        "camera", help="camera listing, probing, and single-frame capture"
    )
    parser.set_defaults(func=incomplete_command(parser))
    sub = parser.add_subparsers(dest="camera_command", metavar="<command>")

    p_list = sub.add_parser("list", help="list cameras on a daemon")
    add_daemon_option(p_list)
    add_all_option(p_list)
    p_list.set_defaults(func=cmd_list)

    p_probe = sub.add_parser("probe", help="force a deep camera re-probe")
    add_daemon_option(p_probe)
    add_all_option(p_probe)
    p_probe.set_defaults(func=cmd_probe)

    p_image = sub.add_parser("image", help="capture one raw frame")
    p_image.add_argument("camera", help="camera name (slug)")
    add_daemon_option(p_image)
    add_all_option(p_image, note=" (--base64 destination only)")
    output_group = p_image.add_mutually_exclusive_group()
    output_group.add_argument(
        "-o", "--output", metavar="FILE", default=None, help="write the frame to FILE"
    )
    output_group.add_argument(
        "--base64", action="store_true", help="write base64-encoded frame to stdout"
    )
    p_image.add_argument(
        "--encoding",
        choices=("jpeg", "png"),
        default="jpeg",
        help="capture encoding: jpeg (default) or png (lossless, for test fixtures)",
    )
    p_image.add_argument(
        "--force",
        action="store_true",
        help="allow raw image bytes to an attached terminal (normally refused)",
    )
    p_image.set_defaults(func=cmd_image)
