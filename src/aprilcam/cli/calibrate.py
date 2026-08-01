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

from aprilcam.cli._common import EXIT_ERROR, EXIT_OK, add_all_option, add_daemon_option, report_error
from aprilcam.client.discovery import Discovery
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_calibrate"]


def cmd_calibrate(
    args: argparse.Namespace, *, discovery_factory: Callable[[], Discovery] = Discovery
) -> int:
    """``aprilcam camera calibrate <camera> [-d HOST]``.

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
        daemon = discovery_factory().connect(args.daemon)
        result = daemon.calibrate(args.camera)
    except AprilCamError as exc:
        return report_error(exc)

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
        return EXIT_OK

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
    add_daemon_option(p_calibrate)
    add_all_option(p_calibrate, note=" (refused -- calibration is single-target)")
    p_calibrate.set_defaults(func=cmd_calibrate)
