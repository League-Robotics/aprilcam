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

**``camera tags --follow``/``--json`` (sprint 039, ticket 005).**
``cmd_tags`` gains a continuous mode: instead of one ``get_tags`` burst,
``--follow`` opens ``Daemon.stream_tags`` (``client.streams.
SocketTagStream``, already used by nothing in this package before this
ticket) and prints rows until Ctrl-C. This is the mode the daemon's
multi-frame confirmation filter (``_confirm_detections``) was designed
for -- a one-shot call after any gap reports ``(no tags detected)``
even with tags plainly visible, because the confirmation streak has to
rebuild from zero each isolated call, while a continuous stream lets it
build up and stay confirmed. ``--follow`` is read-only but is still
refused together with ``--all`` -- a documented, narrow exception to
"fan-out is restricted to read-only commands" (``cli-DESIGN.md`` §3):
``fan_out``'s per-target "call, render, move on" shape cannot interleave
several concurrently-blocking streams, so the combination is refused up
front, before any connection, mirroring ``cmd_view``'s single-target
refusal. ``--json`` is this package's first machine-readable output
mode, scoped narrowly to ``camera tags``: one ``json.dumps(...)`` line
per frame (JSON Lines with ``--follow``; a single line for the one-shot
burst without it), never changing the human-formatted default's output.
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
    fan_out,
    incomplete_command,
    print_cameras,
    print_probe_report,
    report_error,
)
from aprilcam.cli import calibrate as calibrate_cli
from aprilcam.cli import flatfield as flatfield_cli
from aprilcam.cli import intrinsics as intrinsics_cli
from aprilcam.cli import locate as locate_cli
from aprilcam.client.discovery import Discovery
from aprilcam.config.interfaces import ConfigLoader
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.daemon.interfaces import Daemon, TagStream
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

    The uncalibrated branch (ticket 039-002) prints ``record.
    image_yaw_deg`` -- a *different* field from ``yaw_rad``, in a
    different (image-space, clockwise) convention (see ``TagRecord``'s
    docstring) -- since an uncalibrated camera has no world frame to
    report ``yaw_rad`` in at all. It is never printed alongside the
    calibrated branch's ``yaw=`` field; that would be two conflicting
    "yaw" numbers on the same line.
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
    image_yaw = "-" if record.image_yaw_deg is None else f"{record.image_yaw_deg:.1f}°"
    return f"  {header:<14} UNCALIBRATED -- pixel={pixel} yaw={image_yaw}"


def _tag_record_to_json(record: TagRecord) -> dict:
    """One ``TagRecord`` as a JSON-safe ``dict`` -- ``--json``'s per-tag
    payload, nested under a frame's ``"tags"`` list (see
    ``_frame_to_json``). Carries both the calibrated fields
    (``world``/``yaw_rad``/``heading_rad``/``speed``) and the
    uncalibrated ones (``pixel``/``image_yaw_deg``) unconditionally,
    each ``None`` when not applicable -- unlike ``_format_tag_row``'s
    either/or human rendering, a machine consumer gets every field the
    record actually carries and decides for itself what to use, keyed
    off ``calibrated`` the same way the record itself is."""
    return {
        "tag": {"family": record.tag.family.value, "number": record.tag.number},
        "calibrated": record.calibrated,
        "pixel": None if record.pixel is None else {"x": record.pixel.x, "y": record.pixel.y},
        "image_yaw_deg": record.image_yaw_deg,
        "world": None if record.world is None else {"x": record.world.x, "y": record.world.y},
        "yaw_rad": record.yaw_rad,
        "heading_rad": record.heading_rad,
        "speed": record.speed,
    }


def _frame_to_json(frame: TagFrame) -> dict:
    """One ``TagFrame`` as a JSON-safe ``dict`` -- ``--json``'s unit of
    output. One of these becomes one printed line (``json.dumps``, no
    indentation) -- JSON Lines with ``--follow`` (one line per streamed
    frame), or a single line for the one-shot burst without it
    (``cli-DESIGN.md`` §3's "this package's first machine-readable
    output mode"). ``calibration_stale`` rides along here exactly as it
    does in-band on ``TagFrame`` itself (UC-11.S-3) -- a stream consumer
    has no other side channel to learn calibration went stale."""
    return {
        "timestamp": frame.timestamp,
        "frame_index": frame.frame_index,
        "calibration_stale": frame.calibration_stale,
        "tags": [_tag_record_to_json(record) for record in frame.tags],
    }


def _format_follow_row(record: TagRecord, timestamp: float) -> str:
    """One ``--follow`` row: a frame timestamp (seconds, millisecond
    precision) prefixed onto ``_format_tag_row``'s existing line, so a
    recording session can tell which frame each row came from without
    changing that helper's own column layout."""
    return f"{timestamp:.3f}  {_format_tag_row(record)}"


def _print_tags(frame: TagFrame, *, as_json: bool = False) -> None:
    """A ``get_tags`` result, one row per detected tag -- or, with
    ``as_json``, one ``json.dumps(...)`` line for the whole frame
    (``_frame_to_json``). An empty frame in the default (human) mode
    prints an explicit ``(no tags detected)`` rather than nothing at
    all -- an empty field is a normal, successful state, not an error
    (UC-20's failure mode: "nothing detected" must never read as a
    stack trace or a blank screen someone has to guess about). ``--json``
    has no such placeholder: an empty frame is simply
    ``{"tags": [], ...}``, itself already an explicit, parseable "empty"
    signal."""
    if as_json:
        print(json.dumps(_frame_to_json(frame)))
        return
    if not frame.tags:
        print("  (no tags detected)")
        return
    for record in frame.tags:
        print(_format_tag_row(record))


def _cmd_tags_follow(args: argparse.Namespace, discovery: Discovery) -> int:
    """The ``--follow`` loop body of ``cmd_tags`` (sprint 039, ticket
    005): a continuous subscription via ``Daemon.stream_tags`` (UC-11)
    instead of one ``get_tags`` burst, printing one row per tag per
    frame (or one JSON line per frame with ``--json``) until Ctrl-C.

    Connecting and opening the stream share one ``try``/``except``:
    either can raise a typed ``AprilCamError`` (an unknown camera, a
    connection failure) and both go through the same ``report_error``
    path every other ``cmd_*`` uses -- only the interrupt loop itself
    treats Ctrl-C specially.

    ``KeyboardInterrupt`` ends the loop cleanly: the stream is closed
    (``TagStream.close()``) and ``EXIT_OK`` is returned -- the same
    "Ctrl-C is a normal way to end a following command" convention
    ``camera view``'s window-close path already establishes, never a
    traceback. ``stream.close()`` runs in a ``finally`` so it fires
    whether the loop ended via Ctrl-C or ran to completion (a stream
    that closes itself, e.g. a clean daemon-side disconnect).
    """
    try:
        stream: TagStream = discovery.connect(args.daemon).stream_tags(args.camera)
    except AprilCamError as exc:
        return report_error(exc)

    try:
        for frame in stream:
            if args.json:
                print(json.dumps(_frame_to_json(frame)))
            else:
                for record in frame.tags:
                    print(_format_follow_row(record, frame.timestamp))
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()

    return EXIT_OK


def cmd_tags(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera tags <camera> [-d HOST] [--all] [--follow] [--json]``.

    ``--follow``/``--json`` are sprint 039 ticket 005 additions -- see
    this module's docstring and ``_cmd_tags_follow`` for the design.
    ``--follow --all`` is refused before ``discovery_factory`` is even
    called, matching ``cmd_view``'s "refuse before any connection"
    shape for its own single-target restriction. The pre-existing
    one-shot path below (no ``--follow``) is otherwise unchanged --
    ``--json`` is the only new behavior it gains, gated on the flag.
    """
    if args.follow and args.all:
        print(
            "error: 'camera tags --follow' is single-target -- a blocking "
            "stream loop cannot fan out the way a one-shot query can (UC-11 "
            "fan-out rule). Drop --all and run --follow once per daemon.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    discovery = discovery_factory()

    if args.follow:
        return _cmd_tags_follow(args, discovery)

    if args.all:
        def render(label: str, frame: TagFrame) -> None:
            print(f"{label}:")
            _print_tags(frame, as_json=args.json)

        return fan_out(discovery, lambda daemon: daemon.get_tags(args.camera), render)

    try:
        frame = discovery.connect(args.daemon).get_tags(args.camera)
    except AprilCamError as exc:
        return report_error(exc)
    _print_tags(frame, as_json=args.json)
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
    was last pushed). ``aprilcam camera config <camera> --dump [-d
    HOST]`` (ticket 023-005, UC-24) prints the live document to stdout
    in exact on-disk JSON, via the already-existing ``GetCameraConfig``
    RPC (``Daemon.get_camera_config``) -- nothing to push, this is the
    read half of the same noun.

    All three modes share the positional argument but are mutually
    exclusive in meaning: with ``--reset`` or ``--dump`` it names a
    *camera* (slug or persistent number), not a file path -- there is
    no document to load client-side, the daemon answers from its own
    memory (and, for ``--reset``, its own disk). ``--reset``/``--dump``
    are themselves mutually exclusive with each other at the argparse
    level (``add_subparser`` below), and each is checked here **before**
    ``loader.load_camera_config`` ever runs, so neither mode risks
    treating the camera token as a file path.

    Pushing: ``name``/``playfield`` take effect (the camera is linked)
    and ``settings`` are applied to the camera's hardware immediately
    when it is currently held open (``DaemonService.set_camera_config``
    -> ``Camera.apply_config``). An unsupported key or a value the
    hardware will not take fails the **whole** push
    (``UnsupportedSetting``, surfaced here through ``report_error`` like
    any other typed error) and leaves both the hardware and the
    previously-pushed document unchanged (UC-03.S-3).
    """
    if args.dump:
        try:
            config = discovery_factory().connect(args.daemon).get_camera_config(args.file)
        except AprilCamError as exc:
            return report_error(exc)
        print(json.dumps(config.to_dict(), indent=2, sort_keys=True))
        return EXIT_OK

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

    Prints a startup line naming the view raw (no deskew/homography)
    and pointing at ``playfield view`` as the deskewed alternative
    (sprint 031 ticket 004, SUC-004) -- before the daemon connection is
    attempted, so the signposting is seen even if the connection
    subsequently fails. UC-16's "raw means raw" guarantee itself is
    unchanged by this ticket; only the visibility of that fact is new
    (see ``view/viewer.py``'s ``run_raw_view`` for the window-title
    half of the same signposting).

    **Sprint 038, SUC-004**: if no frame ever arrives, ``run_view``
    raises ``view.viewer.FirstFrameTimeout`` (a client-local, bounded
    timeout -- see that module) instead of the process hanging forever
    with the window open and no error. Caught here alongside
    ``NoDisplayAvailable``/``AprilCamError``: prints a diagnostic naming
    the camera, the likely cause (a wedged capture session), and the
    remedy (``aprilcam camera probe`` or a daemon restart, until the
    worker-process fix lands), then returns ``EXIT_ERROR`` -- never an
    unhandled exception, never a silent hang.
    """
    if args.all:
        print(
            "error: 'camera view' is single-target -- a live video "
            "window has no sensible multi-daemon meaning (UC-16 "
            "fan-out rule). Drop --all and name one camera.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print(
        f"aprilcam camera view: RAW feed for {args.camera!r} -- no deskew "
        "or homography applied. For the deskewed, calibrated view, use "
        "'aprilcam playfield view' instead (UC-16)."
    )

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
    except viewer.FirstFrameTimeout as exc:
        print(
            f"error: no frame received from {args.camera!r} -- the capture "
            "session may be wedged. Try 'aprilcam camera probe' or restart "
            "the daemon (sprint 038's per-camera worker-process fix targets "
            f"this) ({exc}).",
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
    add_all_option(p_tags, note=" (refused with --follow -- a stream cannot fan out)")
    p_tags.add_argument(
        "--follow",
        action="store_true",
        help="subscribe to the tag stream and print rows continuously until "
        "Ctrl-C, instead of one snapshot -- the daemon's multi-frame "
        "confirmation filter needs consecutive frames to reliably report a "
        "tag, so this is the mode that actually sees tags reliably; "
        "single-target, refused together with --all",
    )
    p_tags.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object per line instead of human-aligned text "
        "(one line for a one-shot snapshot; one line per frame with --follow)",
    )
    p_tags.set_defaults(func=cmd_tags)

    p_config = sub.add_parser(
        "config",
        help="push a camera config document, or --reset/--dump the live one",
    )
    p_config.add_argument(
        "file",
        help="path to a camera config JSON file to push; with --reset/--dump, the "
        "camera name (slug or persistent number) instead",
    )
    add_daemon_option(p_config)
    mode_group = p_config.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--reset",
        action="store_true",
        help="discard any live override and re-apply config_dir/cameras/<slug>/"
        "config.json (or the blank template if none is installed) -- no daemon "
        "restart (ticket 023-004)",
    )
    mode_group.add_argument(
        "--dump",
        action="store_true",
        help="print the live config document to stdout in exact on-disk JSON, "
        "instead of pushing (ticket 023-005, UC-24)",
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

    # ``flatfield`` lives in its own module (cli/flatfield.py, sprint
    # 037 ticket 004) but is grouped under this ``camera`` subparser
    # tree -- see that module's docstring for why.
    flatfield_cli.add_subparser(sub)
    intrinsics_cli.add_subparser(sub)

    # ``locate`` lives in its own module (cli/locate.py, sprint 029
    # ticket 003) but is grouped under this ``camera`` subparser tree --
    # see that module's docstring for why.
    locate_cli.add_subparser(sub)
