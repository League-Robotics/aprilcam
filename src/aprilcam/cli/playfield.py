"""``aprilcam playfield config <file>`` (ticket 022-001); ``aprilcam
playfield image`` (ticket 022-005, UC-06/UC-19); ``aprilcam playfield
view`` (ticket 022-006, UC-18); ``aprilcam playfield annotations``
(ticket 024-004, UC-28); ``aprilcam playfield where`` (ticket 025-002,
UC-08).

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

**``playfield view`` (ticket 022-006, UC-18; annotated-by-default and
``--bare`` added sprint 032 ticket 003).** ``cmd_view`` makes no RPC
call directly -- like ``camera.py``'s ``cmd_view``, it resolves the
camera (this module's own convention, above) and connects, then hands
the connected ``Daemon`` to ``view/viewer.py``'s ``run_playfield_view``,
which subscribes to the daemon's image stream -- it does not poll,
ticket 027-007 -- and owns the ``client/renderer.py`` rendering. The
window opens **annotated by default**: tag ids, orientation "hats", and
world-position crosshairs drawn over the deskewed frame, plus the tag
sidebar (sprint 032, tickets 001/002). ``--bare`` opts out of both --
the plain deskewed stream, no overlay, no sidebar -- and is passed
straight through as ``run_view(camera, daemon, bare=args.bare)``. Two
exceptions get a UC-18-specific message rather than the generic
``report_error`` text: ``view.viewer.NoDisplayAvailable`` (pointing at
``playfield image``, mirroring UC-16.S-3) and ``errors.NotCalibrated``
(pointing at ``camera calibrate``, UC-18.S-2 -- **never** caught to
substitute a raw frame; ``run_playfield_view`` itself never falls back,
this only adds the pointer to the message). ``--all`` is refused up
front, before any connection is attempted, matching ``camera view``'s
single-target rule -- unaffected by ``--bare``, which only changes what
is drawn once a window is already open, not whether/how the camera or
connection are resolved.

**``playfield annotations`` (ticket 024-004, UC-28).** List/clear only
-- authoring (``put``/``draw``) stays Python-client-only this sprint,
per the issue's explicit scope and sprint.md's resolved open question
7; ``cmd_annotations`` never calls ``put_annotation``/``replace_layer``.
Bare form lists the current :class:`~aprilcam.types.AnnotationSet` via
``Daemon.get_annotations``; ``--clear [--layer NAME]`` clears one layer
or everything via ``Daemon.clear_annotations`` instead -- one flag on
the noun changing which RPC runs, mirroring ``cmd_config``'s ``--dump``
shape exactly, per the ticket's own implementation-plan note.

**Playfield resolution -- decided, not deferred.** Unlike ``image``/
``view``, annotations are **playfield-addressed directly**
(``get_annotations(playfield: str, ...)``), so there is no
camera-to-playfield indirection for ``cli._common.resolve_playfield_camera``
to resolve here at all -- the daemon API simply never mentions a
camera. ``playfield`` is therefore a **required positional** naming the
playfield outright, with no implicit single-playfield default: every
worked example in the ticket (its Acceptance Criteria and Builds
section alike) passes an explicit name, and -- unlike camera
resolution, where ``CameraState.playfield`` links give an established,
already-documented convention to reuse (the "Playfield commands
resolve their camera" rule in ``cli.md``) -- there is no analogous,
already-agreed default-playfield convention on record here. Inventing
one now would be new design the ticket explicitly warns against
("implements exactly what is already documented, not a new design"),
not a gap to close silently.

``--clear`` and ``--all`` are mutually exclusive: a write does not fan
out (``cli.md``'s fan-out table), refused up front -- before any
``discovery_factory()``/``connect()`` call -- mirroring ``cmd_view``'s
own up-front ``--all`` refusal shape and wording exactly.

**``playfield where`` (ticket 025-002, UC-08).** "Where is the blue
dot" -- answered entirely from the named playfield's surveyed
definition via ``Daemon.find_feature``: no camera opened, no
calibration required. ``playfield`` is a required positional naming the
target directly, exactly like ``cmd_annotations`` above (this module's
own "decided, not deferred" precedent) -- ``find_feature(playfield,
query)`` has no camera to resolve a default from, so there is nothing
for ``cli._common.resolve_playfield_camera`` to do here. ``query`` is a
second positional, a free-text string (quote multi-word queries, e.g.
``"the blue dot"``). Read-only, so it fans out like ``cmd_list``/bare
``cmd_annotations`` (``cli.md``'s fan-out table). An empty result
prints an explicit ``(no matches)`` rather than nothing at all, matching
this file's ``(no playfields)``/``(no annotations)`` convention
(UC-08.S-3: no match is not an error).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Tuple

from aprilcam.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    add_all_option,
    add_daemon_option,
    fan_out,
    incomplete_command,
    report_error,
    resolve_playfield_camera,
)
from aprilcam.client.discovery import Discovery
from aprilcam.config.interfaces import ConfigLoader, Feature
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.errors import AprilCamError, NotCalibrated
from aprilcam.playfield.interfaces import Playfield
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    Circle,
    Marker,
    PathShape,
    Polygon,
    Text,
)
from aprilcam.view import viewer

__all__ = [
    "add_subparser",
    "cmd_list",
    "cmd_config",
    "cmd_image",
    "cmd_view",
    "cmd_annotations",
    "cmd_where",
]


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
    run_view: Callable[..., None] = viewer.run_playfield_view,
) -> int:
    """``aprilcam playfield view [CAMERA] [--bare] [-d HOST]``
    (UC-18, ticket 022-006; ``--bare`` added sprint 032 ticket 003;
    ``CAMERA`` positional added out-of-process 2026-08-23 -- ``--camera``
    remains as a compatibility alias).

    Opens a streaming window (``view/viewer.py``'s
    ``run_playfield_view``, subscribed to the daemon's image stream --
    it does not poll, ticket 027-007) showing the deskewed, calibrated
    field -- north up, east right. By default the view is
    **annotated**: detected tags' boundaries, ids, and orientation
    "hats" (``client/renderer.py``) drawn over the frame, plus the tag
    sidebar (sprint 032). ``--bare`` opts out of both, showing the
    plain deskewed stream unmodified -- passed straight through as
    ``run_view(camera, daemon, bare=args.bare)``. The camera comes from
    the optional positional (``aprilcam playfield view 5``), the
    ``--camera`` compatibility alias, or, when both are omitted,
    ``cli._common.resolve_playfield_camera`` -- the auto-resolve
    convention :func:`cmd_image` still uses. Giving both the positional
    and ``--camera`` is refused up front. (:func:`cmd_image` keeps its
    flag-only shape for now -- unifying both commands on a positional
    that also accepts a playfield name is the parked design issue
    ``playfield-view-should-accept-a-camera-number-or-playfield-name``.)

    ``--all`` is refused up front, before any connection is attempted --
    a live video window has no sensible multi-daemon meaning, matching
    ``camera view``'s single-target rule; unaffected by ``--bare``,
    which only changes what is drawn once a window is already open. A
    headless host fails with a message pointing at ``playfield image``
    instead of a GUI-toolkit backtrace (mirroring UC-16.S-3). An
    uncalibrated camera's :class:`~aprilcam.errors.NotCalibrated` --
    raised from the stream subscription itself
    (``daemon.stream_images(camera, deskewed=True)``), never caught by
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

    if args.camera is not None and args.camera_flag is not None:
        print(
            "error: camera given both as a positional and via --camera "
            "-- pass it once.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    camera_request = args.camera if args.camera is not None else args.camera_flag

    try:
        daemon = discovery_factory().connect(args.daemon)
    except AprilCamError as exc:
        return report_error(exc)

    camera, problem = resolve_playfield_camera(daemon, camera=camera_request)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_ERROR
    assert camera is not None  # resolve_playfield_camera's contract: exactly one of the pair is None

    try:
        run_view(camera, daemon, bare=args.bare)
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


def _format_shape(shape: object) -> str:
    """One :class:`~aprilcam.types.AnnotationShape` as a human-readable
    string -- geometry only, world-cm always (``WorldPoint``'s own
    contract), one branch per member of the closed union in
    ``types.py``. The ``AssertionError`` fallback can only fire if a
    sixth shape type is ever added without updating this function --
    the union is closed by construction, so every real value takes one
    of the branches above it."""
    if isinstance(shape, Marker):
        at = f"({shape.at.x:.2f}, {shape.at.y:.2f})"
        label = f" {shape.label!r}" if shape.label is not None else ""
        return f"marker at {at}{label}"
    if isinstance(shape, PathShape):
        points = " -> ".join(f"({p.x:.2f}, {p.y:.2f})" for p in shape.points)
        return f"path [{points}]"
    if isinstance(shape, Polygon):
        points = " -> ".join(f"({p.x:.2f}, {p.y:.2f})" for p in shape.points)
        return f"polygon [{points}]"
    if isinstance(shape, Circle):
        center = f"({shape.center.x:.2f}, {shape.center.y:.2f})"
        return f"circle center={center} radius={shape.radius_cm:.2f}cm"
    if isinstance(shape, Text):
        at = f"({shape.at.x:.2f}, {shape.at.y:.2f})"
        return f"text {shape.text!r} at {at}"
    raise AssertionError(f"unknown annotation shape: {shape!r}")  # pragma: no cover


def _print_annotations(annotation_set: AnnotationSet) -> None:
    """A ``get_annotations`` result, printed to invent no separate
    truth from what the daemon actually holds (the acceptance
    criterion's own phrase, echoing ``cmd_config --dump``'s "print the
    daemon's document verbatim" discipline) -- every annotation in
    *annotation_set* gets one row, nothing added or summarised away.

    ``revision`` prints first: it is how someone debugging a push can
    tell whether it landed, without diffing rows by eye. Grouped by
    layer -- the organising concept the viewer's own panels toggle by
    (``cli.md``'s UC-18 open-question note on the panel model) -- rather
    than one flat list. An empty set still reads as explicitly empty
    (UC-28.S-4: a fresh daemon's empty set is a normal state, not an
    error), matching ``_common.print_cameras``'s ``(no cameras)``
    convention rather than printing nothing at all.
    """
    print(f"revision {annotation_set.revision}")
    if not annotation_set.annotations:
        print("  (no annotations)")
        return

    by_layer: dict[str, list[Annotation]] = {}
    for annotation in annotation_set.annotations:
        by_layer.setdefault(annotation.layer, []).append(annotation)

    for layer in sorted(by_layer):
        print(f"  layer {layer!r}:")
        for annotation in by_layer[layer]:
            kind = type(annotation.shape).__name__
            print(f"    {annotation.id:<12} {kind:<10} {_format_shape(annotation.shape)}")


def _format_playfield_row(playfield: Playfield) -> str:
    """One line per playfield: name, field size, and what is linked.

    Deliberately shows ``linked_cameras()`` -- that link is **derived at
    serve time** from the daemon's held camera configs, never stored in
    the definition (see ``playfield/playfield.py``), so it is the one
    fact here a developer cannot get by reading the JSON on disk. It is
    also the exact thing that has to be true before ``playfield image``
    or ``playfield view`` can resolve a camera at all.
    """
    definition = playfield.definition
    cameras = playfield.linked_cameras()
    linked = ", ".join(cameras) if cameras else "-"
    return (
        f"  {playfield.name:<28} "
        f"{definition.width_cm:g} x {definition.height_cm:g} cm   "
        f"aruco={len(definition.required_aruco_ids())}  "
        f"features={len(definition.features())}  "
        f"cameras={linked}"
    )


def _print_playfields(playfields: Tuple[Playfield, ...]) -> None:
    if not playfields:
        print("  (no playfields)")
        return
    for playfield in playfields:
        print(_format_playfield_row(playfield))


def cmd_list(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam playfield list [-d HOST] [--all]``.

    The playfield counterpart to ``camera list``: what definitions does
    this daemon actually hold right now? Finding none is an **empty
    result, not an error** -- a daemon started before any playfield was
    pushed or installed is a normal state (UC-01), and the same
    reasoning that makes an empty camera list normal applies here.

    Read-only, so it fans out (``cli.md``'s fan-out table: nothing that
    changes state or streams interactively fans out; a listing does).
    """
    discovery = discovery_factory()
    if args.all:
        def render(label: str, playfields) -> None:
            print(f"{label}:")
            _print_playfields(playfields)

        return fan_out(discovery, lambda daemon: daemon.list_playfields(), render)

    try:
        playfields = discovery.connect(args.daemon).list_playfields()
    except AprilCamError as exc:
        return report_error(exc)
    _print_playfields(playfields)
    return EXIT_OK


def cmd_annotations(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam playfield annotations <playfield> [-d HOST] [--all]``
    (UC-28, ticket 024-004) lists the current annotation set.
    ``aprilcam playfield annotations <playfield> --clear [--layer
    NAME] [-d HOST]`` clears one layer or everything instead -- the
    same ``--dump``-style "flag on the noun picks the RPC" shape
    ``cmd_config`` uses, per the module docstring above.

    ``playfield`` is a required positional naming the target directly
    (module docstring's "decided, not deferred" note) -- there is no
    camera to resolve here, unlike ``cmd_image``/``cmd_view``.

    ``--clear --all`` is refused **before** ``discovery_factory()`` is
    even called -- an annotation write does not fan out (``cli.md``'s
    fan-out table), mirroring ``cmd_view``'s own up-front ``--all``
    refusal exactly, including checking before any connection is
    attempted so a doomed multi-daemon write never touches the
    network.
    """
    if args.clear and args.all:
        print(
            "error: 'playfield annotations --clear' is single-target -- "
            "an annotation write does not fan out (UC-28, matching "
            "'playfield view'/'playfield config'). Drop --all and "
            "target one daemon.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    discovery = discovery_factory()

    if args.clear:
        try:
            discovery.connect(args.daemon).clear_annotations(args.playfield, args.layer)
        except AprilCamError as exc:
            return report_error(exc)
        if args.layer is not None:
            print(f"cleared layer {args.layer!r} on playfield {args.playfield!r}")
        else:
            print(f"cleared all annotations on playfield {args.playfield!r}")
        return EXIT_OK

    if args.all:
        def render(label: str, annotation_set: AnnotationSet) -> None:
            print(f"{label}:")
            _print_annotations(annotation_set)

        return fan_out(
            discovery,
            lambda daemon: daemon.get_annotations(args.playfield, args.layer),
            render,
        )

    try:
        annotation_set = discovery.connect(args.daemon).get_annotations(args.playfield, args.layer)
    except AprilCamError as exc:
        return report_error(exc)
    _print_annotations(annotation_set)
    return EXIT_OK


def _format_feature_row(feature: Feature) -> str:
    """One ``find_feature`` match: slug, kind, colour (or ``-`` when
    unset), and surveyed world position -- everything the acceptance
    criteria ask for, nothing invented (there is no detection status to
    report here; the definition is ground truth, not observation, per
    ``FeatureQuery``'s own docstring)."""
    colour = feature.colour if feature.colour is not None else "-"
    return (
        f"  {feature.slug:<28} {feature.kind:<10} {colour:<8} "
        f"({feature.position.x:.2f}, {feature.position.y:.2f})"
    )


def _print_features(features: Tuple[Feature, ...]) -> None:
    """A ``find_feature`` result, printed verbatim. An empty result
    reads as an explicit ``(no matches)`` (UC-08.S-3: no match is a
    normal, non-error answer), matching this module's ``(no
    playfields)``/``(no annotations)`` convention rather than silent
    empty output."""
    if not features:
        print("  (no matches)")
        return
    for feature in features:
        print(_format_feature_row(feature))


def cmd_where(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam playfield where <playfield> <query> [-d HOST]
    [--all]`` (ticket 025-002, UC-08) -- "where is the blue dot,"
    answered entirely from the named playfield's surveyed definition via
    ``Daemon.find_feature``: no camera opened, no calibration required.

    ``playfield`` is a required positional naming the target directly,
    exactly like ``cmd_annotations`` above (module docstring's
    "decided, not deferred" note) -- there is no camera to resolve a
    default from here, unlike ``cmd_image``/``cmd_view``.

    Read-only, so it fans out (``cli.md``'s fan-out table): nothing
    changes state, so ``--all`` runs the same query against every
    discovered daemon, one labelled block per daemon, mirroring
    ``cmd_list``/bare ``cmd_annotations`` exactly.
    """
    discovery = discovery_factory()

    if args.all:
        def render(label: str, features: Tuple[Feature, ...]) -> None:
            print(f"{label}:")
            _print_features(features)

        return fan_out(
            discovery, lambda daemon: daemon.find_feature(args.playfield, args.query), render
        )

    try:
        features = discovery.connect(args.daemon).find_feature(args.playfield, args.query)
    except AprilCamError as exc:
        return report_error(exc)
    _print_features(features)
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``playfield list``/``config``/``image``/``view``/
    ``annotations``/``where`` into the top-level parser."""
    parser = subparsers.add_parser("playfield", help="playfield definitions")
    parser.set_defaults(func=incomplete_command(parser))
    sub = parser.add_subparsers(dest="playfield_command", metavar="<command>")

    p_list = sub.add_parser(
        "list", help="list the playfield definitions this daemon holds"
    )
    add_daemon_option(p_list)
    add_all_option(p_list)
    p_list.set_defaults(func=cmd_list)

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
        "camera",
        nargs="?",
        default=None,
        help="camera name (slug or persistent number) -- resolved automatically "
        "when omitted and exactly one camera is linked to a playfield",
    )
    p_view.add_argument(
        "--camera",
        dest="camera_flag",
        default=None,
        metavar="CAMERA",
        help="compatibility alias for the camera positional",
    )
    add_daemon_option(p_view)
    add_all_option(p_view, note=" (refused -- viewing is single-target)")
    p_view.add_argument(
        "--bare",
        action="store_true",
        help="show the deskewed stream unmodified -- no tag overlay, no "
        "sidebar (default: the annotated view with the tag overlay and "
        "sidebar)",
    )
    p_view.set_defaults(func=cmd_view)

    p_annotations = sub.add_parser(
        "annotations",
        help="list or clear the daemon-held annotation store (UC-28, list/clear only)",
    )
    p_annotations.add_argument("playfield", help="playfield name")
    add_daemon_option(p_annotations)
    add_all_option(p_annotations, note=" (list only -- refused with --clear)")
    p_annotations.add_argument(
        "--clear",
        action="store_true",
        help="clear annotations instead of listing them (refuses --all)",
    )
    p_annotations.add_argument(
        "--layer",
        metavar="NAME",
        default=None,
        help="restrict to one layer; with --clear, clear only that layer "
        "(clearing an absent layer is a no-op, not an error)",
    )
    p_annotations.set_defaults(func=cmd_annotations)

    p_where = sub.add_parser(
        "where",
        help="find a surveyed feature by free-text query (UC-08, no camera/calibration needed)",
    )
    p_where.add_argument("playfield", help="playfield name")
    p_where.add_argument("query", help="free-text query, e.g. 'the blue dot' (quote multi-word queries)")
    add_daemon_option(p_where)
    add_all_option(p_where)
    p_where.set_defaults(func=cmd_where)
