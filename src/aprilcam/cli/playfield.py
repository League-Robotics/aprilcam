"""``aprilcam playfield config <file>`` (ticket 022-001); ``aprilcam
playfield image`` (ticket 022-005, UC-06/UC-19).

Thin argv-to-client-call shell over ``config/loader.py`` (client-side
document validation) and ``client/daemon_client.py`` (the push/capture
RPCs) -- ``adapters.md``'s "adapters hold no authoritative state,
adapters do no vision" rules apply here exactly as they do in
``cli/camera.py``.

**Push only, for ``config``.** Fetching a playfield back
(``get_playfield``/``list_playfields``) is exercised through the daemon
API directly (tests, and any future MCP/Python caller) -- sprint.md's
Builds section for this ticket names only the push command; a
``playfield get``/``list`` CLI verb is not part of this slice.

**Validate before any RPC.** ``ConfigLoader.load_playfield`` raises
``MalformedDocument`` (a no-``name`` document, invalid JSON, a
duplicate ArUco id, ...) *before* ``set_playfield`` is ever called --
UC-23.S-4's "a bad file never reaches the daemon."

**``playfield image`` names no camera** (``cli.md``'s "Playfield
commands resolve their camera" convention): the daemon's ``get_frame``
is camera-addressed, always -- no daemon-side "the playfield's camera"
shortcut (``daemon.interfaces.Daemon.get_frame``'s own docstring) -- so
resolution happens here, client-side, via
``cli._common.resolve_playfield_camera`` reading ``CameraState.playfield``
links off a fresh ``list_cameras()`` call. ``--camera`` overrides it
outright. Otherwise this mirrors ``camera image``'s three output modes
and pre-capture checks (UC-17, extended to UC-19 here) almost exactly
-- see that module's docstring for why each check (unwritable
destination, an attached terminal without ``--force``) runs *before*
any capture RPC, not after.
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
    add_daemon_option,
    incomplete_command,
    report_error,
    resolve_playfield_camera,
)
from aprilcam.client.discovery import Discovery
from aprilcam.config.interfaces import ConfigLoader
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_config", "cmd_image"]


def cmd_config(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    loader_factory: Callable[[], ConfigLoader] = JsonConfigLoader,
) -> int:
    """``aprilcam playfield config <file> [-d HOST]``."""
    loader = loader_factory()
    try:
        definition = loader.load_playfield(Path(args.file))
    except AprilCamError as exc:
        return report_error(exc)

    try:
        discovery_factory().connect(args.daemon).set_playfield(definition)
    except AprilCamError as exc:
        return report_error(exc)

    print(
        f"pushed playfield {definition.name!r} "
        f"({len(definition.features())} features, "
        f"{len(definition.required_aruco_ids())} required ArUco ids)"
    )
    return 0


def _unwritable_reason(path: Path) -> Optional[str]:
    """Why *path* cannot be written, or ``None`` if it can -- checked
    before any capture RPC runs, mirroring ``cli/camera.py``'s
    ``cmd_image`` (UC-17: "error before capture, not after") exactly.
    Duplicated rather than imported: each CLI module owns its own small
    argv-shaping helpers, the same "each side owns its own small
    conversion" pattern ``daemon/rpc.py``'s and ``client/models.py``'s
    module docstrings already state for the proto boundary."""
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.exists():
        return f"directory does not exist: {parent}"
    if not os.access(parent, os.W_OK):
        return f"directory not writable: {parent}"
    if path.exists() and not os.access(path, os.W_OK):
        return f"path exists and is not writable: {path}"
    return None


def cmd_image(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam playfield image [--camera CAMERA] [-o FILE | --base64]
    [-d HOST] [--encoding jpeg|png] [--force]`` (UC-06, UC-19).

    No camera positional -- resolved via ``--camera`` or, when omitted,
    ``cli._common.resolve_playfield_camera`` (module docstring). An
    uncalibrated camera's :class:`~aprilcam.errors.NotCalibrated`
    surfaces through :func:`~aprilcam.cli._common.report_error` exactly
    like any other typed error -- no file is written and no raw frame is
    substituted, since ``get_frame(deskewed=True)`` itself never
    produces one for that camera (``daemon/service.py``'s
    ``_get_deskewed_frame``).
    """
    if args.output is not None:
        problem = _unwritable_reason(Path(args.output))
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_ERROR
    elif not args.base64 and not args.force and sys.stdout.isatty():
        print(
            "error: refusing to write raw image bytes to a terminal -- "
            "redirect stdout (e.g. '> field.png'), pass --force, or use "
            "--base64 (UC-17.S-4, extended to UC-19)",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        daemon = discovery_factory().connect(args.daemon)
    except AprilCamError as exc:
        return report_error(exc)

    camera, problem = resolve_playfield_camera(daemon, camera=args.camera)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_ERROR
    assert camera is not None  # resolve_playfield_camera's contract: exactly one of the pair is None

    try:
        frame = daemon.get_frame(camera, deskewed=True, encoding=args.encoding)
    except AprilCamError as exc:
        return report_error(exc)

    if args.output is not None:
        Path(args.output).write_bytes(frame.data)
        px_per_cm = f"{frame.px_per_cm:.3f}" if frame.px_per_cm is not None else "?"
        print(
            f"wrote {len(frame.data)} bytes to {args.output} "
            f"({frame.encoding}, {frame.width}x{frame.height}, {px_per_cm} px/cm)"
        )
    elif args.base64:
        print(base64.b64encode(frame.data).decode("ascii"))
    else:
        sys.stdout.buffer.write(frame.data)
        sys.stdout.buffer.flush()
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``playfield config``/``image`` into the top-level parser."""
    parser = subparsers.add_parser("playfield", help="playfield definitions")
    parser.set_defaults(func=incomplete_command(parser))
    sub = parser.add_subparsers(dest="playfield_command", metavar="<command>")

    p_config = sub.add_parser("config", help="push a playfield definition document")
    p_config.add_argument("file", help="path to a playfield definition JSON file")
    add_daemon_option(p_config)
    p_config.set_defaults(func=cmd_config)

    p_image = sub.add_parser(
        "image", help="capture one deskewed, calibrated playfield frame (UC-06, UC-19)"
    )
    p_image.add_argument(
        "--camera",
        default=None,
        help="camera name (slug or persistent number) -- resolved automatically "
        "when omitted and exactly one camera is linked to a playfield",
    )
    add_daemon_option(p_image)
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
