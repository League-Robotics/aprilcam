"""``aprilcam camera calibrate <camera>`` (ticket 022-003, UC-04, UC-15).

A **pure RPC orchestrator**: every vision/geometry decision (multi-frame
averaged detection, id matching, all-or-nothing acceptance, homography
solve, degenerate-geometry rejection) happens inside the daemon
(``daemon.service.DaemonService.calibrate`` ->
``calibration.calibration.Calibrator``). This module does no vision
work of its own -- it triggers the ``Calibrate`` RPC and reports
whatever ``CalibrationResult`` comes back, matching ``adapters.md``'s
"adapters hold no authoritative state, adapters do no vision" rule
exactly the way ``cli/camera.py`` and ``cli/playfield.py`` already do.

**Success/failure is data, not an exception.** A ``CalibrationResult``
with ``succeeded=False`` (missing ids, or a named degenerate-geometry
``reason``) is a normal RPC return value -- UC-04's "explicit failure,"
not an error the client library raises. Only a *precondition* failure
(no playfield linked -- :class:`~aprilcam.errors.NoPlayfieldLinked`) or
a transport/addressing problem (unknown/absent camera, connection
failure) comes back as a typed :class:`~aprilcam.errors.AprilCamError`,
handled the same way every other CLI command handles one
(:func:`~aprilcam.cli._common.report_error`). This mirrors UC-15's own
text: "a precondition failure, not a calibration failure."

**Wired as ``camera calibrate``, not a top-level ``calibrate``
command** -- ``cli.md``'s existing command-surface convention groups
calibration under the camera it targets. Because ``cli/camera.py``
already owns the ``camera`` subparser tree, :func:`add_subparser` here
takes *that* tree's own subparsers action (the ``sub`` object
``cli/camera.py``'s ``add_subparser`` builds), not the top-level one --
``cli/camera.py`` calls this module's :func:`add_subparser` once its
own ``list``/``probe``/``image``/``tags``/``config``/``view``
sub-subcommands are wired, the same "one thin module, hooked in by its
parent" shape ``cli/playfield.py`` uses at the top level.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from aprilcam.cli._common import EXIT_ERROR, EXIT_OK, add_all_option, connect_for_camera, report_error, verbosity
from aprilcam.client.discovery import Discovery
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_calibrate"]


def cmd_calibrate(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera calibrate <camera>``.

    ``--all`` is refused up front, before any connection is attempted --
    calibration is single-daemon by the fan-out rule (UC-15.S-4), the
    same "refuse before connecting" shape ``cmd_view`` (``cli/camera.py``,
    ticket 022-002) already established and named this exact refusal as
    a forward pointer to.
    """
    if args.all:
        print(
            "error: 'camera calibrate' is single-target -- calibration "
            "targets exactly one daemon (UC-15.S-4 fan-out rule). Drop "
            "--all and name one camera.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        daemon, args.camera = connect_for_camera(discovery_factory(), args.daemon, args.camera)
        result = daemon.calibrate(args.camera)
    except AprilCamError as exc:
        return report_error(exc)

    # Sprint 044 ticket 010: a resolution-mismatched lens store was
    # ignored -- say so on every outcome, stderr like other caveats.
    if result.lens_warning:
        print(f"warning: {result.lens_warning}", file=sys.stderr)

    if result.succeeded:
        matched = ", ".join(str(i) for i in result.matched_ids)
        error_note = (
            f", reprojection error {result.reprojection_error_cm:.3f} cm"
            if result.reprojection_error_cm is not None
            else ""
        )
        print(
            f"calibration succeeded for {args.camera!r}: "
            f"{len(result.matched_ids)} ArUco id(s) matched ({matched}){error_note}"
        )
        print(_lens_line(result.lens_model_source))
        _print_pose_report(result, verbose=verbosity(args) >= 1)
        return EXIT_OK

    if result.method == "pose":
        # A pose attempt that failed a gate: show what was measured, so the
        # reason's threshold can be read against the value.
        _print_pose_report(result, verbose=verbosity(args) >= 1, to_stderr=True)
    if result.missing_ids:
        missing = ", ".join(str(i) for i in result.missing_ids)
        print(
            f"calibration failed for {args.camera!r}: missing ArUco id(s): {missing}",
            file=sys.stderr,
        )
    else:
        reason = result.reason or "calibration failed for an unspecified reason"
        print(f"calibration failed for {args.camera!r}: {reason}", file=sys.stderr)
    return EXIT_ERROR


_LENS_LINES = {
    "measured": "lens model: measured (from the per-camera lens store, carried forward)",
    "estimated": "lens model: estimated (fitted from the field ground truth this call)",
    "legacy": "lens model: legacy (embedded in the previous calibration, carried forward)",
    "none": "lens model: none (no lens correction applied)",
}


def _fmt(value: "float | None", unit: str = "", places: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{places}f}{unit}"


def _print_pose_report(result, *, verbose: bool, to_stderr: bool = False) -> None:
    """The pose-provenance numbers of a calibrate result (sprint 045 ticket 007).

    Numbers go to stdout on success (stderr when reporting a failed
    attempt's measurements); caveats -- ``reduced``/``low`` confidence, the
    centre-point fallback reason, helpful markers not seen -- always go to
    stderr, like every other caveat.
    """
    out = sys.stderr if to_stderr else sys.stdout
    if result.method == "centre-point":
        print(f"method: centre-point ({result.method_reason or 'no reason reported'})", file=out)
        if not to_stderr:
            print(
                f"warning: used the centre-point fit instead of the marker-corner "
                f"pose solve: {result.method_reason or 'no reason reported'}",
                file=sys.stderr,
            )
    elif result.method == "pose":
        print("method: pose", file=out)
        if result.confidence:
            print(f"confidence: {result.confidence}", file=out)
        print(f"corners used: {result.corner_count if result.corner_count is not None else 'n/a'}", file=out)
        print(
            f"reprojection error: {_fmt(result.reprojection_error_px, ' px')}, "
            f"{_fmt(result.reprojection_error_cm, ' cm')}",
            file=out,
        )
        print(f"scale agreement: {_fmt(result.scale_agreement, '', 3)}", file=out)
        print(f"angular spread: {_fmt(result.angular_spread_deg, ' deg', 1)}", file=out)
        if result.camera_height_cm is not None:
            nadir = result.camera_nadir
            nadir_text = f"({nadir.x:.1f}, {nadir.y:.1f}) cm" if nadir is not None else "n/a"
            print(
                f"camera: height {result.camera_height_cm:.1f} cm, nadir {nadir_text}, "
                f"tilt {_fmt(result.camera_tilt_deg, ' deg', 1)}",
                file=out,
            )
        if result.leave_one_out_worst_id is not None:
            print(
                f"leave-one-out worst: marker {result.leave_one_out_worst_id}, "
                f"{_fmt(result.leave_one_out_worst_cm, ' cm')}",
                file=out,
            )
        if verbose and result.leave_one_out_cm:
            print("leave-one-out residuals:", file=out)
            for marker_id, cm in result.leave_one_out_cm:
                print(f"  marker {marker_id}: {cm:.2f} cm", file=out)
    if to_stderr:
        return
    if result.confidence in ("reduced", "low"):
        print(f"warning: calibration confidence is {result.confidence}", file=sys.stderr)
        for note in result.notes:
            print(f"warning: {note}", file=sys.stderr)
    if result.helpful_missing_ids:
        ids = ", ".join(str(i) for i in result.helpful_missing_ids)
        print(
            f"warning: helpful marker id(s) not detected: {ids} "
            "(not required, but they improve the solve)",
            file=sys.stderr,
        )


def _lens_line(source: "str | None") -> str:
    """One line naming which lens model a successful calibrate applied."""
    if source is None:
        return "lens model: unknown (daemon did not report provenance)"
    return _LENS_LINES.get(source, f"lens model: {source}")


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    """Wire ``calibrate`` into *sub* -- ``cli/camera.py``'s own ``camera``
    subparsers action (**not** the top-level ``aprilcam`` one). See the
    module docstring for why this command is grouped under ``camera``
    while living in its own file.
    """
    p_calibrate = sub.add_parser(
        "calibrate", help="calibrate a camera against its linked playfield (UC-04, UC-15)"
    )
    p_calibrate.add_argument("camera", help="camera name (slug or persistent number)")
    add_all_option(p_calibrate, note=" (refused -- calibration is single-target)")
    p_calibrate.set_defaults(func=cmd_calibrate)
