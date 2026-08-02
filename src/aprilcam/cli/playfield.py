"""``aprilcam playfield config <file>`` (ticket 022-001); ``aprilcam
playfield image`` (ticket 022-005, UC-06/UC-19); ``aprilcam playfield
view`` (ticket 022-006, UC-18).

Thin argv-to-client-call shell over ``config/loader.py`` (client-side
document validation) and ``client/daemon_client.py`` (the push/capture
RPCs) -- ``adapters.md``'s "adapters hold no authoritative state,
adapters do no vision" rules apply here exactly as they do in
``cli/camera.py``.

**Push, or ``--dump`` to read back.** ``config``'s first CLI *read*
path (ticket 023-005, UC-24): ``aprilcam playfield config <name>
--dump`` prints the live document via the already-existing
``GetPlayfield`` RPC (``Daemon.get_playfield``), in exact on-disk JSON
(``PlayfieldDefinition.to_dict()``). Before this ticket, fetching a
playfield back (``get_playfield``/``list_playfields``) was exercised
only through the daemon API directly (tests, any MCP/Python caller);
no ``playfield get``/``list`` CLI verb exists yet -- ``--dump`` is a
flag on ``config``, matching ``camera config``'s ``--reset``/``--dump``
shape (a flag on the noun, not a new subcommand), not those.

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

**``playfield view`` (ticket 022-006, UC-18).** ``cmd_view`` makes no
RPC call directly -- like ``camera.py``'s ``cmd_view``, it resolves the
camera (this module's own convention, above) and connects, then hands
the connected ``Daemon`` to ``view/viewer.py``'s ``run_playfield_view``,
which owns the poll loop and the ``client/renderer.py`` rendering. Two
exceptions get a UC-18-specific message rather than the generic
``report_error`` text: ``view.viewer.NoDisplayAvailable`` (pointing at
``playfield image``, mirroring UC-16.S-3) and ``errors.NotCalibrated``
(pointing at ``camera calibrate``, UC-18.S-2 -- **never** caught to
substitute a raw frame; ``run_playfield_view`` itself never falls back,
this only adds the pointer to the message). ``--all`` is refused up
front, before any connection is attempted, matching ``camera view``'s
single-target rule.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from aprilcam.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    add_all_option,
    add_daemon_option,
    incomplete_command,
    report_error,
    resolve_playfield_camera,
)
from aprilcam.client.discovery import Discovery
from aprilcam.config.interfaces import ConfigLoader
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import AprilCamError, NotCalibrated
from aprilcam.view import viewer

__all__ = ["add_subparser", "cmd_config", "cmd_image", "cmd_view"]


def cmd_config(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    loader_factory: Callable[[], ConfigLoader] = JsonConfigLoader,
) -> int:
    """``aprilcam playfield config <file> [-d HOST]`` pushes a playfield
    definition document. ``aprilcam playfield config <name> --dump [-d
    HOST]`` (ticket 023-005, UC-24) prints the live document instead,
    via the already-existing ``GetPlayfield`` RPC -- the module
    docstring's "first CLI read path." The two modes share the
    positional argument but are mutually exclusive in meaning, mirroring
    ``cli/camera.py``'s ``cmd_config``: with ``--dump`` it names a
    *playfield*, not a file to load and push, and the dump check runs
    **before** ``loader.load_playfield`` ever touches the filesystem.
    """
    if args.dump:
        try:
            playfield = discovery_factory().connect(args.daemon).get_playfield(args.file)
        except AprilCamError as exc:
            return report_error(exc)
        print(json.dumps(playfield.definition.to_dict(), indent=2, sort_keys=True))
        return EXIT_OK

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


def cmd_view(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    run_view: Callable[[str, Daemon], None] = viewer.run_playfield_view,
) -> int:
    """``aprilcam playfield view [--camera CAMERA] [-d HOST]`` (UC-18,
    ticket 022-006).

    Opens a poll-driven window (``view/viewer.py``'s
    ``run_playfield_view``) showing the deskewed, calibrated field with
    detected tags drawn -- north up, east right, ``client/renderer.py``'s
    tag boundaries and "hats". No camera positional: resolved via
    ``--camera`` or, when omitted, ``cli._common.resolve_playfield_camera``
    -- the same convention :func:`cmd_image` uses.

    ``--all`` is refused up front, before any connection is attempted --
    a live video window has no sensible multi-daemon meaning, matching
    ``camera view``'s single-target rule. A headless host fails with a
    message pointing at ``playfield image`` instead of a GUI-toolkit
    backtrace (mirroring UC-16.S-3). An uncalibrated camera's
    :class:`~aprilcam.errors.NotCalibrated` -- raised from the first
    poll tick's ``get_frame(deskewed=True)``, never caught by
    ``run_playfield_view`` to substitute a raw frame -- is caught here
    only to add the ``camera calibrate`` pointer (UC-18.S-2).
    """
    if args.all:
        print(
            "error: 'playfield view' is single-target -- a live video "
            "window has no sensible multi-daemon meaning (UC-18, "
            "matching 'camera view'). Drop --all and target one camera.",
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
        run_view(camera, daemon)
    except viewer.NoDisplayAvailable as exc:
        print(
            f"error: no display available ({exc}) -- run 'aprilcam "
            f"playfield image' instead (UC-18, mirroring UC-16.S-3).",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except NotCalibrated as exc:
        print(
            f"error: {exc} -- calibrate first with 'aprilcam camera "
            f"calibrate {camera}' (UC-18.S-2).",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except AprilCamError as exc:
        return report_error(exc)
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``playfield config``/``image``/``view`` into the top-level
    parser."""
    parser = subparsers.add_parser("playfield", help="playfield definitions")
    parser.set_defaults(func=incomplete_command(parser))
    sub = parser.add_subparsers(dest="playfield_command", metavar="<command>")

    p_config = sub.add_parser(
        "config", help="push a playfield definition document, or --dump the live one"
    )
    p_config.add_argument(
        "file",
        help="path to a playfield definition JSON file to push; with --dump, the "
        "playfield name instead",
    )
    add_daemon_option(p_config)
    p_config.add_argument(
        "--dump",
        action="store_true",
        help="print the live playfield document to stdout in exact on-disk JSON, "
        "instead of pushing (ticket 023-005, UC-24)",
    )
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

    p_view = sub.add_parser(
        "view", help="open a live deskewed, calibrated playfield window (UC-18, no --all)"
    )
    p_view.add_argument(
        "--camera",
        default=None,
        help="camera name (slug or persistent number) -- resolved automatically "
        "when omitted and exactly one camera is linked to a playfield",
    )
    add_daemon_option(p_view)
    add_all_option(p_view, note=" (refused -- viewing is single-target)")
    p_view.set_defaults(func=cmd_view)
