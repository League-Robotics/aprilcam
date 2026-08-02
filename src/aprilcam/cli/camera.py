"""``aprilcam camera list|probe|image|tags|view`` (tickets 020-006,
021-006, 022-002).

Thin argv-to-client-call shells over ``client/discovery.py`` (ticket
020-005) -- ``adapters.md``'s "adapters hold no authoritative state,
adapters do no vision" rules apply throughout. Every RPC-shaped call
here is one line; the rest is argv shaping, output formatting, and
mapping typed ``aprilcam.errors`` values to exit codes.

**``camera view`` (ticket 022-002).** ``cmd_view`` itself makes no RPC
call directly -- it connects (or reports why it could not) and then
hands the connected ``Daemon`` to ``view/viewer.py``'s
``run_raw_view``, which owns the poll loop. See that module's docstring
for the "raw means raw" and headless-detection design; see this
module's ``cmd_view`` docstring for the CLI-boundary contract
(``--all`` refusal, the UC-16.S-3 headless message).

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

**``camera tags <camera>`` (ticket 021-006).** ``cli.md``'s command
surface names ``playfield tags`` -- resolved client-side from
``CameraState.playfield`` links -- but that link is always ``None``
until the config-service sprint builds playfields, so that spelling
would fail on every invocation this sprint. This sprint ships the
camera-addressed form instead, mirroring ``camera image``; see
``docs/design/use-cases/cli.md``'s command-surface block and UC-20 for
the deferred spelling and the forward pointer, and sprint 021's
Architecture Decision 3 for the reasoning. Every record this sprint is
uncalibrated by construction (``daemon/detection.py`` never sets
``calibrated=True`` -- calibration does not exist yet), so
``_format_tag_row`` below prints an explicit ``UNCALIBRATED`` marker
plus pixel coordinates and never falls through to printing
``None``-derived zeros as if they were a world position
(UC-20.S-3, the ``TagRecord`` "world-first contract" in ``types.py``).
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
from aprilcam.cli import calibrate as calibrate_cli
from aprilcam.client.discovery import Discovery
from aprilcam.config.interfaces import ConfigLoader
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import AprilCamError
from aprilcam.types import TagFrame, TagRecord
from aprilcam.view import viewer

__all__ = [
    "add_subparser",
    "cmd_list",
    "cmd_probe",
    "cmd_image",
    "cmd_tags",
    "cmd_config",
    "cmd_view",
]


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


def _format_tag_row(record: TagRecord) -> str:
    """One ``TagRecord`` as a single line -- family, id, then either a
    world position (calibrated) or an explicit ``UNCALIBRATED`` marker
    plus pixel coordinates.

    Calibration went live this sprint (ticket 022-003), so the
    calibrated branch below is exercised for real now, not just held as
    dead-but-correct code for a future sprint to activate.

    Guarding on both ``record.calibrated`` and ``record.world is not
    None`` means a record that is inconsistent about its own
    calibration state still cannot print a fabricated world position
    (UC-20.S-3's failure-mode requirement, taken literally: never
    ``0.0, 0.0`` or any world-looking number for an uncalibrated
    record).

    ``yaw`` (the tag's own facing direction, ``record.yaw_rad``) is
    printed ahead of ``heading`` (motion direction, ``record.
    heading_rad``) -- deliberately, since for a stationary tag ``yaw``
    is the only one of the two that means anything: ``heading_rad`` is
    ``None`` (undefined, not stale) for a tag that has not moved enough
    to clear ``vision.motion.MIN_RELIABLE_DISPLACEMENT_CM`` between
    samples (ticket 022-007's real-hardware finding -- see that
    module's docstring), while ``yaw_rad`` is a per-frame orientation
    reading with no such motion precondition and stays populated
    whether or not the tag is moving.
    """
    header = f"{record.tag.family.value} {record.tag.number}"
    if record.calibrated and record.world is not None:
        yaw = "-" if record.yaw_rad is None else f"{record.yaw_rad:.3f}"
        heading = "-" if record.heading_rad is None else f"{record.heading_rad:.3f}"
        speed = "-" if record.speed is None else f"{record.speed:.2f}"
        return (
            f"  {header:<14} world=({record.world.x:.1f}, {record.world.y:.1f}) "
            f"yaw={yaw} heading={heading} speed={speed}"
        )
    if record.pixel is not None:
        pixel = f"({record.pixel.x:.1f}, {record.pixel.y:.1f})"
    else:
        pixel = "(unavailable)"
    return f"  {header:<14} UNCALIBRATED -- pixel={pixel}"


def _print_tags(frame: TagFrame) -> None:
    """A ``get_tags`` result, one row per detected tag. An empty frame
    prints an explicit ``(no tags detected)`` rather than nothing at
    all -- an empty field is a normal, successful state, not an error
    (UC-20's failure mode: "nothing detected" must never read as a
    stack trace or a blank screen someone has to guess about)."""
    if not frame.tags:
        print("  (no tags detected)")
        return
    for record in frame.tags:
        print(_format_tag_row(record))


def cmd_tags(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera tags <camera> [-d HOST] [--all]``."""
    discovery = discovery_factory()
    if args.all:
        def render(label: str, frame: TagFrame) -> None:
            print(f"{label}:")
            _print_tags(frame)

        return fan_out(discovery, lambda daemon: daemon.get_tags(args.camera), render)

    try:
        frame = discovery.connect(args.daemon).get_tags(args.camera)
    except AprilCamError as exc:
        return report_error(exc)
    _print_tags(frame)
    return EXIT_OK


def cmd_config(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    loader_factory: Callable[[], ConfigLoader] = JsonConfigLoader,
) -> int:
    """``aprilcam camera config <file> [-d HOST]`` pushes a whole
    ``CameraConfig`` document (ticket 022-001; real hardware settings
    apply ticket 023-001). ``aprilcam camera config <camera> --reset
    [-d HOST]`` (ticket 023-004) discards any live override and
    re-applies whatever is installed at `config_dir/cameras/<slug>/
    config.json` -- or the blank introspected-plus-blank template when
    nothing is installed -- with no daemon restart (sprint.md Decision
    3: reset always means this fixed on-disk location, never whatever
    was last pushed).

    The two modes share the positional argument but are mutually
    exclusive in meaning: with ``--reset`` it names a *camera* (slug or
    persistent number), not a file path -- there is no document to load
    client-side, the daemon reads its own disk.

    Pushing: ``name``/``playfield`` take effect (the camera is linked)
    and ``settings`` are applied to the camera's hardware immediately
    when it is currently held open (``DaemonService.set_camera_config``
    -> ``Camera.apply_config``). An unsupported key or a value the
    hardware will not take fails the **whole** push
    (``UnsupportedSetting``, surfaced here through ``report_error`` like
    any other typed error) and leaves both the hardware and the
    previously-pushed document unchanged (UC-03.S-3).
    """
    if args.reset:
        try:
            discovery_factory().connect(args.daemon).reset_camera_config(args.file)
        except AprilCamError as exc:
            return report_error(exc)
        print(f"reset camera config for {args.file!r} -- on-disk document now in force")
        return EXIT_OK

    loader = loader_factory()
    try:
        config = loader.load_camera_config(Path(args.file))
    except AprilCamError as exc:
        return report_error(exc)

    try:
        discovery_factory().connect(args.daemon).set_camera_config(config)
    except AprilCamError as exc:
        return report_error(exc)

    playfield = config.playfield if config.playfield is not None else "(none)"
    print(f"pushed camera config {config.name!r} -- playfield={playfield}")
    return EXIT_OK


def cmd_view(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    run_view: Callable[[str, Daemon], None] = viewer.run_raw_view,
) -> int:
    """``aprilcam camera view <camera> [-d HOST]`` (UC-16, ticket
    022-002).

    Opens a poll-driven window (``view/viewer.py``) showing *camera*'s
    raw frames -- camera settings applied, nothing else drawn or
    warped (UC-16 "raw means raw"). ``--all`` is refused up front,
    before any connection is attempted -- viewing is single-target,
    matching ``cli.md``'s fan-out rule (a live video window has no
    sensible multi-daemon meaning) and the refusal ``camera calibrate
    --all`` gets in ticket 003. A headless host (no display) fails with
    a message pointing at ``camera image`` instead of a GUI-toolkit
    backtrace (UC-16.S-3).
    """
    if args.all:
        print(
            "error: 'camera view' is single-target -- a live video "
            "window has no sensible multi-daemon meaning (UC-16 "
            "fan-out rule). Drop --all and name one camera.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        daemon = discovery_factory().connect(args.daemon)
    except AprilCamError as exc:
        return report_error(exc)

    try:
        run_view(args.camera, daemon)
    except viewer.NoDisplayAvailable as exc:
        print(
            f"error: no display available ({exc}) -- run 'aprilcam camera "
            f"image {args.camera}' instead (UC-16.S-3).",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except AprilCamError as exc:
        return report_error(exc)
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``camera list|probe|image|tags`` into the top-level parser."""
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

    p_tags = sub.add_parser("tags", help="list currently detected tags")
    p_tags.add_argument("camera", help="camera name (slug or persistent number)")
    add_daemon_option(p_tags)
    add_all_option(p_tags)
    p_tags.set_defaults(func=cmd_tags)

    p_config = sub.add_parser(
        "config", help="push a camera config document, or --reset it to the on-disk version"
    )
    p_config.add_argument(
        "file",
        help="path to a camera config JSON file to push; with --reset, the camera "
        "name (slug or persistent number) instead",
    )
    add_daemon_option(p_config)
    p_config.add_argument(
        "--reset",
        action="store_true",
        help="discard any live override and re-apply config_dir/cameras/<slug>/"
        "config.json (or the blank template if none is installed) -- no daemon "
        "restart (ticket 023-004)",
    )
    p_config.set_defaults(func=cmd_config)

    p_view = sub.add_parser(
        "view", help="open a live raw-video window (UC-16 -- 'raw means raw', no --all)"
    )
    p_view.add_argument("camera", help="camera name (slug or persistent number)")
    add_daemon_option(p_view)
    add_all_option(p_view, note=" (refused -- viewing is single-target)")
    p_view.set_defaults(func=cmd_view)

    # ``calibrate`` lives in its own module (cli/calibrate.py, ticket
    # 022-003) but is grouped under this ``camera`` subparser tree --
    # see that module's docstring for why.
    calibrate_cli.add_subparser(sub)
