"""Shared CLI plumbing (ticket 020-006): the ``-d``/``--all`` flags, an
already-typed-error-to-exit-code mapping, and the camera-row formatting
``cli/daemon.py`` and ``cli/camera.py`` both need.

Zero business logic lives here, same as the rest of ``cli/`` --
``adapters.md``'s "adapters hold no authoritative state" rule. Every
function below either shapes argv, turns an already-typed
``aprilcam.errors`` value into a stderr line and an exit code, or
formats a value the client library already returned. Nothing here
talks to a daemon.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Callable, Optional, Tuple, TypeVar

from aprilcam.client.addressing import ResolvedCamera, resolve_camera
from aprilcam.client.interfaces import Discovery
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import AprilCamError
from aprilcam.types import CameraState, ProbeReport

__all__ = [
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_PLACEHOLDER",
    "add_daemon_option",
    "add_all_option",
    "add_verbose_option",
    "verbosity",
    "configure_logging",
    "incomplete_command",
    "report_error",
    "format_camera_row",
    "print_cameras",
    "print_probe_report",
    "fan_out",
    "resolve_playfield_camera",
    "connect_for_camera",
    "connect_resolved",
]

#: Exit codes every subcommand function returns. Argparse's own parse
#: failures (bad flag, missing required positional) use its standard
#: code 2, untouched here -- these two are only for a command that
#: parsed cleanly and then either succeeded or hit a typed error.
EXIT_OK = 0
EXIT_ERROR = 1
#: ``camera image`` / ``playfield image`` got a daemon-served NO SIGNAL
#: test card instead of a camera frame (sprint 047). The card is still
#: written to the requested destination; this code tells a script it is
#: not a real frame.
EXIT_PLACEHOLDER = 3

T = TypeVar("T")


def add_daemon_option(parser: argparse.ArgumentParser) -> None:
    """``-d``/``--daemon HOST`` -- the daemon every subcommand targets.
    Added once, to the root parser (``aprilcam -d HOST camera list``),
    never to a subcommand: a subcommand-level copy would also shadow the
    root value with its own ``None`` default. Omitted means
    ``APRILCAM_DAEMON_HOST``, else local. Deliberately not ``-h``, which
    stays ``--help`` everywhere."""
    parser.add_argument(
        "-d",
        "--daemon",
        metavar="HOST",
        default=None,
        help="target daemon HOST[:PORT] for any command "
        "(default: $APRILCAM_DAEMON_HOST, else the local daemon)",
    )


def add_verbose_option(parser: argparse.ArgumentParser) -> None:
    """``-v``/``--verbose`` -- a repeatable verbosity count, on the root
    parser. ``-v`` reveals detail commands normally hide (unusable
    cameras in ``camera list``, daemon addresses in fan-out labels,
    ``camera calibrate``'s per-marker table); ``-vv`` also logs at INFO
    and ``-vvv`` at DEBUG (see :func:`configure_logging`).

    Subparsers get a ``sub_verbose`` copy of the flag via
    :func:`add_verbose_to_subparsers` so ``camera list -v`` works too;
    ``cli.main`` sums the two counts into ``verbose``.
    """
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="more detail: -v shows hidden cameras and daemon addresses, "
        "-vv logs at INFO, -vvv at DEBUG",
    )


def add_verbose_to_subparsers(
    parser: argparse.ArgumentParser, _seen: Optional[set] = None
) -> None:
    """Give every subparser under *parser* (recursively) a ``-v`` that
    counts into ``sub_verbose``, so the flag is accepted after the
    subcommand as well as before it. A separate ``dest`` (rather than
    reusing ``verbose``) keeps a subparser's default from clobbering a
    count the root parser already parsed. Aliases (``camera lens``) map
    several names to one parser, so each is visited once."""
    seen = _seen if _seen is not None else set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for sub in action.choices.values():
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            sub.add_argument(
                "-v",
                "--verbose",
                dest="sub_verbose",
                action="count",
                default=0,
                help="more detail (same as the global -v)",
            )
            add_verbose_to_subparsers(sub, seen)


def verbosity(args: argparse.Namespace) -> int:
    """The merged ``-v`` count from *args* (0 when the namespace has none)."""
    return getattr(args, "verbose", 0) or 0


def configure_logging(verbose: int) -> None:
    """Level wiring only: ``-vv`` sets the root logger to INFO, ``-vvv``
    (or more) to DEBUG; below that the logging config is left alone."""
    if verbose >= 3:
        logging.getLogger().setLevel(logging.DEBUG)
    elif verbose == 2:
        logging.getLogger().setLevel(logging.INFO)


def add_all_option(parser: argparse.ArgumentParser, *, note: str = "") -> None:
    """``--all`` -- fan out to every daemon ``Discovery.find_all()``
    reports. Only added to the commands ``cli.md``'s fan-out table
    allows (read-only ones, per this ticket's scope); a command kept
    single-daemon-only this sprint simply never gets this flag, so
    attempting ``--all`` there is argparse's standard
    unrecognised-argument error rather than a hand-rolled message."""
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"fan out to every discovered daemon{note}",
    )


def incomplete_command(parser: argparse.ArgumentParser) -> Callable[[argparse.Namespace], int]:
    """A ``func`` default for a subparser whose own sub-subcommand was
    omitted -- e.g. ``aprilcam daemon`` with no ``start``/``stop``/
    ``status``. Prints *parser*'s own help and fails, rather than
    silently doing nothing (argparse would otherwise happily "succeed"
    at parsing a command that named no actual action).

    Relies on argparse's default-resolution order: this is set as
    *parser*'s default before its sub-subparsers are added, so it
    survives into the final namespace only when none of them was
    selected -- any chosen sub-subcommand's own ``set_defaults(func=…)``
    overrides it.
    """

    def _run(_args: argparse.Namespace) -> int:
        parser.print_help()
        return EXIT_ERROR

    return _run


def report_error(exc: AprilCamError) -> int:
    """Print *exc*'s already-human-readable message to stderr and
    return the exit code every command's typed-error path uses.

    One place, so the CLI surface never re-derives "is this fatal" or
    reconstructs a message from a string -- the whole point of
    ``errors.py``'s taxonomy (its module docstring: "an adapter can
    only honour those contracts if the client library raises something
    it can dispatch on... no adapter parses message text"). ``str(exc)``
    is exactly the message each typed error's own ``__init__`` built
    (holder name, unknown-camera-plus-available-list, etc.).
    """
    print(f"error: {exc}", file=sys.stderr)
    return EXIT_ERROR


def format_camera_row(state: CameraState) -> str:
    """One ``CameraState`` as a single aligned line -- shared by
    ``camera list``/``camera probe`` and ``daemon status``'s camera
    listing, so the same camera reads identically everywhere it is
    printed.

    Presence, usability, and calibration are printed as separate words
    rather than collapsed into one flag: ``present`` true with
    ``usable`` false ("plugged in and broken") must stay visibly
    distinct from ``present`` false ("not plugged in") -- UC-25.S-4's
    contract, and the same distinction UC-14 asks for absent cameras
    (listed and flagged, never omitted).

    Flat-field gain-map presence/staleness (sprint 037 ticket 005) is
    printed alongside calibration the same way: a ``flatfield=present``/
    ``flatfield=absent`` word, plus a distinct ``flatfield-stale`` flag
    in the trailing parenthetical -- kept a different word than
    calibration's own ``stale`` flag so the two independent staleness
    signals never read as one.

    Lens-model provenance (sprint 044 ticket 010) is a ``lens=measured|
    estimated|legacy|absent`` word appended after the flat-field word
    (before the trailing flags; the row prefix is unchanged), plus a
    ``lens-pending`` flag when the lens store holds a model the current
    calibration does not yet embed -- run ``camera calibrate`` to apply.
    """
    number = f"[{state.number}]" if state.number is not None else "[-]"
    presence = "closed" if state.closed and state.present else ("present" if state.present else "absent")
    usability = "usable" if state.usable else "unusable"
    playfield = state.playfield if state.playfield is not None else "-"
    calibration = "calibrated" if state.calibrated else "uncalibrated"
    if state.calibrated and state.calibration_method:
        # Sprint 045 ticket 007: how it was solved and how far to trust it.
        detail = (
            f"{state.calibration_method}, {state.calibration_confidence}"
            if state.calibration_confidence
            else state.calibration_method
        )
        calibration += f" ({detail})"
    flatfield = "flatfield=present" if state.flatfield_present else "flatfield=absent"
    lens = f"lens={state.lens_model}"
    flags = []
    if state.calibration_stale:
        flags.append("stale")
    if state.flatfield_stale:
        flags.append("flatfield-stale")
    if state.lens_pending:
        flags.append("lens-pending")
    if state.frame_dark:
        flags.append("dark")
    flag_suffix = f"  ({', '.join(flags)})" if flags else ""
    return (
        f"{number:<6} {state.name:<32} {presence:<8} {usability:<9} "
        f"playfield={playfield:<20} {calibration}  {flatfield}  {lens}{flag_suffix}"
    )


def label_host(label: str) -> Optional[str]:
    """The bare host name from a :func:`fan_out` label (``"donald
    (192.168.4.60:5280)"`` -> ``"donald"``), or ``None`` when the label
    is just an address (a nameless daemon) and so cannot prefix a
    ``HOST-N`` camera address."""
    host = label.split(" (", 1)[0]
    return None if ":" in host or host.replace(".", "").isdigit() else host


def print_cameras(
    cameras: Tuple[CameraState, ...], *, verbose: int = 0, host: Optional[str] = None
) -> None:
    """A camera list/probe result, one row per camera. An empty tuple
    prints an explicit ``(no cameras)`` rather than nothing at all, so
    a genuinely empty daemon looks deliberate rather than like the
    command produced no output by accident (UC-01.S-2: empty is a
    normal, successful state, not an error).

    With ``verbose < 1`` rows whose camera is not ``usable`` are hidden
    and a ``(N unusable camera(s) hidden; -v to show)`` note says so --
    a daemon whose cameras are all unusable prints just the note.
    ``camera probe`` passes ``verbose=1`` (via :func:`print_probe_report`)
    because it reports discovery state and must show everything.

    With *host* (the daemon's short name, from a fan-out label) each
    numbered row ends with ``address=HOST-N`` -- the copyable form
    ``camera image`` and friends accept (sprint 048)."""
    if not cameras:
        print("  (no cameras)")
        return
    shown = [c for c in cameras if c.usable] if verbose < 1 else list(cameras)
    for state in shown:
        row = format_camera_row(state)
        if host and state.number is not None:
            row += f"  address={host}-{state.number}"
        print(f"  {row}")
    hidden = len(cameras) - len(shown)
    if hidden:
        noun = "camera" if hidden == 1 else "cameras"
        print(f"  ({hidden} unusable {noun} hidden; -v to show)")


def print_probe_report(report: ProbeReport) -> None:
    """A probe result: the refreshed cameras, then any backends the
    probe could not interrogate -- printed explicitly, never silently
    dropped, so a partial probe can never read as a complete one
    (``ProbeReport.skipped``'s contract, UC-25.S-7)."""
    print_cameras(report.cameras, verbose=1)
    if report.skipped:
        print("  skipped:")
        for skip in report.skipped:
            print(f"    {skip.backend}: {skip.reason}")


def fan_out(
    discovery: Discovery,
    call: Callable[[Daemon], T],
    render: Callable[[str, T], None],
    *,
    verbose: int = 0,
) -> int:
    """Run *call* against every daemon ``discovery.find_all()`` reports.

    Implements ``cli.md``'s fan-out contract: every row is labelled
    with its daemon, and one unreachable daemon is reported alongside
    the results that did come back rather than failing the whole
    command (UC-14.S-4). *call* takes a connected ``Daemon`` and
    returns whatever *render* needs to print one daemon's block;
    *render*'s first argument is that daemon's label.

    ``find_all()`` itself failing (``DiscoveryFailed`` -- discovery
    could not even enumerate candidates, distinct from "enumerated
    zero", UC-13.S-5) aborts the whole command, since there is nothing
    left to iterate; a per-daemon ``connect``/RPC failure during the
    loop does not.

    At ``verbose >= 1`` a remote daemon's label also carries its address,
    ``donald (192.168.4.60:5280)``; the local label stays ``gala (local)``
    (no address worth showing for the local socket). A nameless
    daemon is already labelled by its address, so nothing is added.

    Returns :data:`EXIT_ERROR` if any discovered daemon failed to
    answer, so a script can detect a partial result even though the
    successful rows were still printed; :data:`EXIT_OK` if every
    discovered daemon answered -- including the trivial case of
    finding zero daemons, which is success, not failure (UC-13.S-2's
    "empty is not an error", extended to the fan-out case).
    """
    try:
        infos = discovery.find_all()
    except AprilCamError as exc:
        return report_error(exc)

    if not infos:
        print("(no daemons found)")
        return EXIT_OK

    had_failure = False
    for info in infos:
        base = info.name or info.host
        if info.is_local:
            label = f"{base} (local)"
        elif verbose >= 1 and base != info.host:
            # `info.host` already carries the port ("ip:port").
            label = f"{base} ({info.host})"
        else:
            label = base
        try:
            daemon = discovery.connect(None if info.is_local else info.host)
            result = call(daemon)
        except AprilCamError as exc:
            had_failure = True
            print(f"{label}: unreachable -- {exc}", file=sys.stderr)
            continue
        render(label, result)

    return EXIT_ERROR if had_failure else EXIT_OK


def connect_for_camera(
    discovery: Discovery, daemon_flag: Optional[str], camera: str
) -> Tuple[Daemon, str]:
    """Connect to the daemon a camera argument addresses.

    *camera* may be a slug, a number, or a fan-out ``HOST-N`` address
    (sprint 048); :func:`aprilcam.client.addressing.resolve_camera`
    decides the daemon and the camera string to send it. *daemon_flag*
    is ``args.daemon`` (the explicit ``-d``) -- a ``HOST-N`` naming a
    different daemon raises ``ConflictingTarget``. The
    ``APRILCAM_DAEMON_HOST`` default is not passed: ``connect(None)``
    applies it, and ``HOST-N`` overrides it. Typed errors propagate for
    the caller's usual ``report_error`` path.
    """
    resolved = resolve_camera(camera, discovery=discovery, target=daemon_flag)
    return connect_resolved(discovery, resolved), resolved.camera


def connect_resolved(discovery: Discovery, resolved: ResolvedCamera) -> Daemon:
    """Connect to the daemon a :class:`ResolvedCamera` names: the local
    socket explicitly when ``HOST-N`` named this machine (so
    ``APRILCAM_DAEMON_HOST`` cannot redirect it), else ``connect(host)``."""
    if resolved.local:
        return discovery.connect_local()
    return discovery.connect(resolved.host)


def connect_optional_camera(
    discovery_factory: Callable[[], Discovery],
    daemon_flag: Optional[str],
    camera: Optional[str],
) -> Tuple[Daemon, Optional[str]]:
    """The daemon for a playfield-addressed command and the camera string
    to send it. A named camera may be a fan-out ``HOST-N`` address
    (``angst-4``), resolved like the ``camera`` commands do
    (:func:`connect_for_camera`); with no camera, the ``-d`` daemon, for
    :func:`resolve_playfield_camera` to pick from.
    """
    if camera is None:
        return discovery_factory().connect(daemon_flag), None
    return connect_for_camera(discovery_factory(), daemon_flag, camera)


def resolve_playfield_camera(
    daemon: Daemon, *, camera: Optional[str] = None, hint: str = "--camera"
) -> Tuple[Optional[str], Optional[str]]:
    """The camera a ``playfield``-addressed command (``image``, ``view``,
    ``tags``) should target, per ``cli.md``'s "playfield commands resolve
    their camera" rule: the daemon API is camera-addressed, always (no
    daemon-side "the playfield's camera" shortcut), so this resolution is
    the CLI's own, client-side, reading ``CameraState.playfield`` links
    from a fresh :meth:`~aprilcam.daemon.interfaces.Daemon.list_cameras`
    call.

    Returns ``(resolved_camera, error_message)`` -- exactly one is
    ``None``. *camera*, when given (``--camera``), is returned verbatim
    without any lookup: an explicit choice always wins and is never
    second-guessed against what is currently linked. Otherwise: exactly
    one linked, present camera resolves silently; zero or several is an
    error naming what is linked and pointing at ``--camera`` to
    disambiguate -- never an arbitrary pick, the same "ambiguity is
    reported, not silently resolved" discipline
    ``FeatureQuery.find``'s docstring states for feature lookups.
    """
    if camera is not None:
        return camera, None

    cameras = daemon.list_cameras()
    linked = tuple(sorted(c.name for c in cameras if c.playfield is not None and c.present))

    if len(linked) == 1:
        return linked[0], None
    if not linked:
        return None, (
            "no camera is linked to a playfield -- push one with "
            f"'aprilcam camera config <file>', or pass {hint}"
        )
    return None, (
        f"multiple cameras are linked to a playfield ({', '.join(linked)}) "
        f"-- pass {hint} to disambiguate"
    )
