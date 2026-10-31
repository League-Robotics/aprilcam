"""``aprilcam camera flatfield <camera>`` (sprint 037 ticket 004).

A **pure RPC orchestrator**, mirroring ``cli/calibrate.py``'s shape
exactly: every vision/quality-gate decision (deriving the field-interior
mask, capturing the burst, measuring p98/p2 spread, accepting or
refusing) happens inside the daemon
(``daemon.service.DaemonService.capture_flat_field`` ->
``calibration.interfaces.FlatFieldCapturer``). This module does no
vision work of its own -- it triggers the ``CaptureFlatField``/
``ClearFlatField`` RPCs and reports whatever ``FlatFieldResult`` comes
back, matching ``adapters.md``'s "adapters hold no authoritative state,
adapters do no vision" rule the same way ``cli/calibrate.py`` and
``cli/camera.py`` already do.

**Success/refusal is data, not an exception.** A ``FlatFieldResult``
with ``succeeded=False`` (a failing quality-gate check, named by
:attr:`~aprilcam.calibration.interfaces.FlatFieldResult.reason`) is a
normal RPC return value, not an error the client library raises --
mirroring ``CalibrationResult``'s identical "explicit failure is data"
contract. Only a *precondition* failure (no derivable field-interior
mask -- :class:`~aprilcam.errors.NoFieldMask`) or a transport/addressing
problem comes back as a typed :class:`~aprilcam.errors.AprilCamError`,
handled the same way every other CLI command handles one
(:func:`~aprilcam.cli._common.report_error`).

**The daemon restores camera config even on failure.** ``exposure``,
when given, is pushed as a temporary override for the capture burst
only -- ``Daemon.capture_flat_field``'s own docstring guarantees the
camera's prior stored/live configuration is restored afterward, on
success, a refusal, or an exception alike. This module does not
reimplement or re-verify that guarantee; it only ever sends the
override and reports the result.

**Wired as ``camera flatfield``, not a top-level ``flatfield``
command** -- ``cli.md``'s existing command-surface convention groups a
camera-targeted operation under the camera it targets, the same
"grouped under ``camera``, living in its own file" shape
``cli/calibrate.py`` already established. Because ``cli/camera.py``
already owns the ``camera`` subparser tree, :func:`add_subparser` here
takes *that* tree's own subparsers action, not the top-level one.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from aprilcam.cli._common import EXIT_ERROR, EXIT_OK, add_all_option, report_error
from aprilcam.client.discovery import Discovery
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_flatfield"]


def _format_spread(values) -> str:
    """Per-channel p98/p2 spread values, comma-joined -- one number per
    channel (length 1 for mono, 3 for colour), matching
    ``FlatFieldResult.spread_before``/``.spread_after``'s own shape."""
    return ", ".join(f"{v:.3f}" for v in values)


def _print_result(camera: str, result, *, dry_run: bool) -> None:
    """A ``FlatFieldResult`` as human-readable text -- the measured
    per-channel spread before/after is always printed, whether the
    attempt succeeded, was refused, or was only evaluated (``--dry-run``,
    UC-01..03): never omitted just because the gate failed, mirroring
    ``FlatFieldResult``'s own "report the number even on a refused
    attempt" contract."""
    if dry_run:
        headline = (
            f"flat-field capture for {camera!r}: evaluated but NOT PERSISTED (--dry-run)"
        )
    elif result.succeeded:
        headline = f"flat-field capture succeeded for {camera!r}"
    else:
        headline = f"flat-field capture refused for {camera!r}"
    print(headline)
    print(f"  spread before: {_format_spread(result.spread_before)}")
    print(f"  spread after:  {_format_spread(result.spread_after)}")
    if result.forced_checks:
        print(f"  forced check(s) overridden: {', '.join(result.forced_checks)}")
    if result.reason is not None:
        print(f"  reason: {result.reason}")
    if dry_run and not result.succeeded:
        print("  (would have been refused even without --dry-run)")


def cmd_flatfield(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera flatfield <camera> [--exposure N]
    [--frames K] [--force] [--dry-run] [--clear]``.

    ``--all`` is refused up front, before any connection is attempted --
    flat-field capture is single-daemon, the same "refuse before
    connecting" shape ``cmd_calibrate`` (``cli/calibrate.py``) already
    established for the identical reason (a per-daemon capture has no
    sensible fan-out meaning).

    ``--clear`` combined with any capture-only flag (``--exposure``/
    ``--frames``/``--dry-run``/``--force``) is refused client-side,
    before any RPC -- a usage error, not a daemon round trip: clearing
    and capturing are mutually exclusive operations, so combining them
    with capture-only options can never mean anything coherent.
    """
    if args.all:
        print(
            "error: 'camera flatfield' is single-target -- flat-field capture "
            "targets exactly one daemon (mirrors calibration's fan-out rule). "
            "Drop --all and name one camera.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.clear and (
        args.exposure is not None or args.frames is not None or args.dry_run or args.force
    ):
        print(
            "error: --clear cannot be combined with --exposure/--frames/--dry-run/--force "
            "-- clearing and capturing are mutually exclusive.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        daemon = discovery_factory().connect(args.daemon)
        if args.clear:
            daemon.clear_flat_field(args.camera)
            print(f"cleared flat-field gain map for {args.camera!r}")
            return EXIT_OK
        result = daemon.capture_flat_field(
            args.camera,
            exposure=args.exposure,
            frames=args.frames,
            force=args.force,
            dry_run=args.dry_run,
        )
    except AprilCamError as exc:
        return report_error(exc)

    _print_result(args.camera, result, dry_run=args.dry_run)
    return EXIT_OK if result.succeeded else EXIT_ERROR


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    """Wire ``flatfield`` into *sub* -- ``cli/camera.py``'s own ``camera``
    subparsers action (**not** the top-level ``aprilcam`` one). See the
    module docstring for why this command is grouped under ``camera``
    while living in its own file.
    """
    p_flatfield = sub.add_parser(
        "flatfield",
        help="capture (or clear) a per-camera flat-field gain-map correction (sprint 037)",
    )
    p_flatfield.add_argument("camera", help="camera name (slug or persistent number)")
    p_flatfield.add_argument(
        "--exposure",
        type=float,
        default=None,
        metavar="N",
        help="temporary exposure override for the capture burst only -- the camera's prior "
        "stored/live configuration is restored afterward, on success, a refusal, or an "
        "exception alike; default: whatever this camera is currently configured for",
    )
    p_flatfield.add_argument(
        "--frames",
        type=int,
        default=None,
        metavar="K",
        help="number of frames to average for the burst; default: the daemon's own default",
    )
    p_flatfield.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="persist the built gain map even if a quality-gate check fails, naming what "
        "was overridden",
    )
    p_flatfield.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="evaluate and report without writing anything to disk or changing the "
        "live-applied map",
    )
    p_flatfield.add_argument(
        "--clear",
        action="store_true",
        default=False,
        help="delete this camera's stored gain map and metadata sidecar instead of "
        "capturing a new one (refused together with any capture-only flag)",
    )
    add_all_option(p_flatfield, note=" (refused -- flat-field capture is single-target)")
    p_flatfield.set_defaults(func=cmd_flatfield)
