"""``aprilcam camera intrinsics <camera...>`` (sprint 044 ticket 007).

A thin client of the daemon's intrinsics session RPCs: it starts one fresh
session per named camera, polls ``get_intrinsics_status`` and renders the
single status record through ``view/intrinsics_model.py``. No vision and no
cv2 here. The invariant this module owns: **every session it started is
aborted on the way out** (normal exit, Ctrl-C, error) unless a finish already
ended it, so no camera stays pinned.

Never auto-quits on green. A good result waits for Enter (or, with
``--install``, is installed once without asking). A not-good result is
installed (``force``) only on a confirming second Enter, so the operator can
always stop with what they have.
"""

from __future__ import annotations

import argparse
import select
import sys
import time
from typing import Callable, Optional

from aprilcam.cli._common import EXIT_ERROR, EXIT_OK, report_error
from aprilcam.client.discovery import Discovery
from aprilcam.errors import AprilCamError
from aprilcam.intrinsics.interfaces import BoardSpec
from aprilcam.view.intrinsics_model import (
    CONFIRM_WINDOW_S,
    decide_enter,
    finish_text_lines,
    status_text_lines,
)
from aprilcam.view.viewer import NoDisplayAvailable, _headless_reason

__all__ = ["add_subparser", "cmd_intrinsics", "run_gui"]

#: Status poll period (about 3 Hz).
POLL_INTERVAL_S = 0.33


def run_gui(daemon, cameras: list, *, install: bool) -> Optional[int]:
    """Launch the fullscreen Tk intrinsics window (ticket 044-008).

    Returns the GUI's exit code (0 only when every camera ended installed).
    Raises ``NoDisplayAvailable`` on a display-less host; ``cmd_intrinsics``
    catches that and falls back to text mode. The import is deferred so
    text mode never loads Tk.
    """
    from aprilcam.view.intrinsics_gui import run_intrinsics_gui

    return run_intrinsics_gui(daemon, cameras, install=install)


def _stdin_enter_ready(timeout: float) -> bool:
    """True if a line (Enter) arrives on stdin within *timeout* seconds."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        time.sleep(timeout)
        return False
    if not ready:
        return False
    line = sys.stdin.readline()
    if line == "":  # EOF: nothing will ever arrive; do not spin.
        time.sleep(timeout)
        return False
    return True


def _print_lines(lines) -> None:
    for line in lines:
        print(line, flush=True)


def _text_loop(daemon, cameras: list, owned: set, *, install: bool,
               enter_ready: Callable[[float], bool], interval: float) -> int:
    """Poll and print until every camera is installed (or the caller is
    interrupted). Mutates *owned*: cameras whose session a finish ended are
    removed so the caller's cleanup does not abort them."""
    last_text = None
    auto_tried: set = set()
    installed: set = set()
    statuses = []
    armed_at: Optional[float] = None  # first not-good Enter, awaiting a second
    while owned:
        statuses = [daemon.get_intrinsics_status(c) for c in cameras]
        text = status_text_lines(statuses)
        if text != last_text:
            _print_lines(text)
            last_text = text
        by_cam = {s.camera: s for s in statuses}
        if install:
            for cam in list(owned):
                st = by_cam.get(cam)
                if st is not None and st.good and cam not in auto_tried:
                    auto_tried.add(cam)
                    _finish(daemon, cam, owned, installed)
            if not owned:
                break
        if enter_ready(interval):
            now = time.monotonic()
            armed = armed_at is not None and now - armed_at < CONFIRM_WINDOW_S
            decision = decide_enter({c: by_cam[c].good for c in cameras if c in owned},
                                    armed=armed)
            armed_at = None
            if decision.action == "confirm":
                armed_at = now
                print(decision.notice, flush=True)
            for cam in decision.cameras:
                _finish(daemon, cam, owned, installed, force=decision.action == "force")
            last_text = None
    return EXIT_OK if installed == set(cameras) else EXIT_ERROR


def _finish(daemon, cam: str, owned: set, installed: set, force: bool = False) -> None:
    """Install a camera (``force`` only after a confirmed not-good Enter). A
    session the finish ended is no longer ours to abort; a refused install
    leaves it collecting (and owned)."""
    result = daemon.finish_intrinsics(cam, True, force)
    _print_lines(finish_text_lines(result))
    if result.installed:
        owned.discard(cam)
        installed.add(cam)


def cmd_intrinsics(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    run_gui: Callable[..., Optional[int]] = run_gui,
    headless_check: Callable[[], Optional[str]] = _headless_reason,
    enter_ready: Callable[[float], bool] = _stdin_enter_ready,
    interval: float = POLL_INTERVAL_S,
) -> int:
    """``aprilcam camera intrinsics <camera...> [--board SPEC] [--install]
    [--no-gui]``. Seams (all keyword) make it testable without a
    daemon, a display, or a blocking stdin."""
    if args.board is not None:
        try:
            BoardSpec.parse(args.board)
        except ValueError as exc:
            print(f"error: --board: {exc}", file=sys.stderr)
            return EXIT_ERROR
    cameras = list(dict.fromkeys(args.camera))  # de-dup, keep order

    owned: set = set()
    daemon = None
    try:
        daemon = discovery_factory().connect(args.daemon)
        for cam in cameras:
            daemon.start_intrinsics(cam, args.board)
            owned.add(cam)

        use_gui = not args.no_gui
        if use_gui:
            reason = headless_check()
            if reason:
                print(f"notice: no display ({reason}); using text mode", file=sys.stderr)
                use_gui = False
        if use_gui:
            try:
                rc = run_gui(daemon, cameras, install=args.install)
            except NoDisplayAvailable as exc:
                print(f"notice: no display ({exc}); using text mode", file=sys.stderr)
                rc = None
            if rc is not None:
                return rc
            if not args.no_gui:
                print("notice: GUI not available; using text mode", file=sys.stderr)

        print(f"intrinsics: {', '.join(cameras)} -- show the ChArUco board to the camera. "
              "Enter installs when GOOD ENOUGH (twice: accept as-is); Ctrl-C aborts.", flush=True)
        return _text_loop(daemon, cameras, owned, install=args.install,
                          enter_ready=enter_ready, interval=interval)
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return EXIT_ERROR
    except AprilCamError as exc:
        return report_error(exc)
    finally:
        if daemon is not None:
            for cam in list(owned):
                try:
                    daemon.abort_intrinsics(cam)
                except Exception:  # best effort: keep releasing the others
                    pass


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    """Wire ``intrinsics`` into ``cli/camera.py``'s ``camera`` subparsers."""
    p = sub.add_parser(
        "intrinsics",
        aliases=["lens"],
        help="measure the lens model with a ChArUco board (alias: 'camera lens'; sprint 044)",
    )
    p.add_argument("camera", nargs="+", help="camera name(s) (slug or persistent number)")
    p.add_argument("--board", default=None, metavar="SPEC",
                   help="board spec COLSxROWS:SQUARE_MM:MARKER_MM:DICT "
                   "(default: $APRILCAM_CHARUCO_BOARD or 5x7:37:28:DICT_5X5_100)")
    p.add_argument("--install", action="store_true",
                   help="install automatically, once, when GOOD ENOUGH (never installs a "
                   "not-good result)")
    p.add_argument("--no-gui", action="store_true",
                   help="text mode: print status lines; Enter installs, Ctrl-C aborts")
    p.set_defaults(func=cmd_intrinsics)
