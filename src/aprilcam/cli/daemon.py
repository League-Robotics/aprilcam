"""``aprilcam daemon start|stop|status`` (ticket 020-006).

Thin argv-to-client-call shells, per ``adapters.md``'s "adapters hold
no authoritative state" rule -- every line here either shapes argv,
formats a ``DaemonStatus``, or maps a typed ``aprilcam.errors``
exception to an exit code. No camera or daemon logic lives here; all
of it is already in ``client/lifecycle.py`` and ``client/discovery.py``
(ticket 020-005).

**start/stop are local-only, structurally.**
``client.lifecycle.DaemonLifecycle`` takes no host parameter anywhere
in its surface (see that module's docstring). This is where
UC-13.S-4's refusal actually happens: ``-d`` is still accepted by
these two subparsers -- so ordinary argparse behaviour (``-h``,
unknown-flag errors) stays normal -- but any value given to it on
``start``/``stop`` is refused before ``DaemonLifecycle`` is even
constructed, with a message pointing at SSH rather than a bare
"unsupported option" or, worse, silently starting a local daemon while
the operator believes they targeted a remote one.

**status is the one command here that reads remotely.** Per
``cli.md``'s drafted UC-13 answer ("yes, status accepts -d, lifecycle
does not"), ``-d HOST`` on ``status`` goes through
``client.discovery.Discovery().connect(host).status()`` -- the
RPC-answered service view -- while a bare ``status`` uses
``client.lifecycle.DaemonLifecycle().status()``, the local process
view that correctly reports "not running" before any daemon exists
(unlike a bare RPC connect attempt, which would just fail to connect
and look identical to "daemon crashed").

**No ``--all`` here.** The sprint architecture's Scope section names
exactly three fan-out-eligible commands this sprint (``camera list``,
``camera probe``, ``camera image``); ``daemon status --all`` is
sanctioned by ``cli.md``'s general fan-out table but out of this
sprint's slice -- there is no ``daemon list`` yet to make multi-daemon
status especially useful, and Architecture Decision 2 means
``find_all()`` cannot discover more than the local daemon this sprint
regardless. Recorded here as a scope decision, not an oversight.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from aprilcam.cli._common import (
    EXIT_OK,
    add_daemon_option,
    format_camera_row,
    incomplete_command,
    report_error,
)
from aprilcam.client.discovery import Discovery
from aprilcam.client.lifecycle import DaemonLifecycle
from aprilcam.daemon.interfaces import DaemonStatus
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_start", "cmd_stop", "cmd_status"]


def _refuse_remote(command: str, host: str) -> int:
    print(
        f"error: 'aprilcam daemon {command}' cannot target a remote host "
        f"({host!r}); process lifecycle is local-only by design -- SSH to "
        f"that host and run 'aprilcam daemon {command}' there instead "
        "(UC-13.S-4).",
        file=sys.stderr,
    )
    return 1


def _print_status(label: str, status: DaemonStatus) -> None:
    print(f"{label}: {'running' if status.running else 'not running'}")
    print(f"  cameras: {len(status.cameras)}")
    for camera in status.cameras:
        print(f"    {format_camera_row(camera)}")
    print(f"  config_dir: {status.config_dir}")
    print(f"  state_dir: {status.state_dir}")


def cmd_start(
    args: argparse.Namespace,
    *,
    lifecycle_factory: Callable[[], DaemonLifecycle] = DaemonLifecycle,
) -> int:
    """``aprilcam daemon start``. See the module docstring for why any
    ``-d`` value refuses rather than being interpreted."""
    if args.daemon is not None:
        return _refuse_remote("start", args.daemon)
    lifecycle = lifecycle_factory()
    try:
        lifecycle.start()
    except AprilCamError as exc:
        return report_error(exc)
    status = lifecycle.status()
    print(f"daemon started ({len(status.cameras)} camera(s))")
    return EXIT_OK


def cmd_stop(
    args: argparse.Namespace,
    *,
    lifecycle_factory: Callable[[], DaemonLifecycle] = DaemonLifecycle,
) -> int:
    """``aprilcam daemon stop``. ``DaemonLifecycle.stop()`` itself is a
    quiet no-op when nothing is running (UC-13: "not an error either
    way") -- the before/after status check here exists only to make
    that outcome legible to the operator, not to change the behaviour."""
    if args.daemon is not None:
        return _refuse_remote("stop", args.daemon)
    lifecycle = lifecycle_factory()
    was_running = lifecycle.status().running
    try:
        lifecycle.stop()
    except AprilCamError as exc:
        return report_error(exc)
    print("daemon stopped" if was_running else "daemon not running")
    return EXIT_OK


def cmd_status(
    args: argparse.Namespace,
    *,
    lifecycle_factory: Callable[[], DaemonLifecycle] = DaemonLifecycle,
    discovery_factory: Callable[[], Discovery] = Discovery,
) -> int:
    """``aprilcam daemon status [-d HOST]``. See the module docstring
    for the local-process-view vs. remote-RPC-view split."""
    try:
        if args.daemon is None:
            status = lifecycle_factory().status()
            label = "local"
        else:
            status = discovery_factory().connect(args.daemon).status()
            label = args.daemon
    except AprilCamError as exc:
        return report_error(exc)
    _print_status(label, status)
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``daemon start|stop|status`` into the top-level parser."""
    parser = subparsers.add_parser(
        "daemon", help="manage the local daemon process"
    )
    parser.set_defaults(func=incomplete_command(parser))
    sub = parser.add_subparsers(dest="daemon_command", metavar="<command>")

    p_start = sub.add_parser("start", help="start the local daemon")
    add_daemon_option(p_start)
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop the local daemon")
    add_daemon_option(p_stop)
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="report daemon readiness")
    add_daemon_option(p_status)
    p_status.set_defaults(func=cmd_status)
