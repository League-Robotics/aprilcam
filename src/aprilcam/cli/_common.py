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
import sys
from typing import Callable, Optional, Tuple, TypeVar

from aprilcam.client.interfaces import Discovery
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import AprilCamError
from aprilcam.types import CameraState, ProbeReport

__all__ = [
    "EXIT_OK",
    "EXIT_ERROR",
    "add_daemon_option",
    "add_all_option",
    "incomplete_command",
    "report_error",
    "format_camera_row",
    "print_cameras",
    "print_probe_report",
    "fan_out",
]

#: Exit codes every subcommand function returns. Argparse's own parse
#: failures (bad flag, missing required positional) use its standard
#: code 2, untouched here -- these two are only for a command that
#: parsed cleanly and then either succeeded or hit a typed error.
EXIT_OK = 0
EXIT_ERROR = 1

T = TypeVar("T")


def add_daemon_option(parser: argparse.ArgumentParser) -> None:
    """``-d``/``--daemon HOST`` -- every daemon-talking command's target
    flag (``cli.md``'s "Daemon targeting" convention: omitted means
    local). Deliberately not ``-h``, which stays ``--help`` everywhere
    -- including here, so ``camera list -h`` prints help rather than
    erroring about a missing hostname."""
    parser.add_argument(
        "-d",
        "--daemon",
        metavar="HOST",
        default=None,
        help="target daemon (default: the local daemon)",
    )


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
    """
    number = f"[{state.number}]" if state.number is not None else "[-]"
    presence = "present" if state.present else "absent"
    usability = "usable" if state.usable else "unusable"
    playfield = state.playfield if state.playfield is not None else "-"
    calibration = "calibrated" if state.calibrated else "uncalibrated"
    flags = []
    if state.calibration_stale:
        flags.append("stale")
    if state.frame_dark:
        flags.append("dark")
    flag_suffix = f"  ({', '.join(flags)})" if flags else ""
    return (
        f"{number:<6} {state.name:<32} {presence:<8} {usability:<9} "
        f"playfield={playfield:<20} {calibration}{flag_suffix}"
    )


def print_cameras(cameras: Tuple[CameraState, ...]) -> None:
    """A camera list/probe result, one row per camera. An empty tuple
    prints an explicit ``(no cameras)`` rather than nothing at all, so
    a genuinely empty daemon looks deliberate rather than like the
    command produced no output by accident (UC-01.S-2: empty is a
    normal, successful state, not an error)."""
    if not cameras:
        print("  (no cameras)")
        return
    for state in cameras:
        print(f"  {format_camera_row(state)}")


def print_probe_report(report: ProbeReport) -> None:
    """A probe result: the refreshed cameras, then any backends the
    probe could not interrogate -- printed explicitly, never silently
    dropped, so a partial probe can never read as a complete one
    (``ProbeReport.skipped``'s contract, UC-25.S-7)."""
    print_cameras(report.cameras)
    if report.skipped:
        print("  skipped:")
        for skip in report.skipped:
            print(f"    {skip.backend}: {skip.reason}")


def fan_out(
    discovery: Discovery,
    call: Callable[[Daemon], T],
    render: Callable[[str, T], None],
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
        label = f"{info.host} (local)" if info.is_local else info.host
        try:
            daemon = discovery.connect(None if info.is_local else info.host)
            result = call(daemon)
        except AprilCamError as exc:
            had_failure = True
            print(f"{label}: unreachable -- {exc}", file=sys.stderr)
            continue
        render(label, result)

    return EXIT_ERROR if had_failure else EXIT_OK
