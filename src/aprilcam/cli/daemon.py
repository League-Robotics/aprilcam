"""``aprilcam daemon start|stop|restart|status|list`` (ticket 020-006;
``list`` added by ticket 028-005).

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

**No ``--all`` here on start/stop/restart/status -- except ``list``,
which fans out inherently.** The sprint-020 architecture's Scope
section named exactly three fan-out-eligible commands for that sprint
(``camera list``, ``camera probe``, ``camera image``); ``daemon status
--all`` was sanctioned by ``cli.md``'s general fan-out table but out of
that sprint's slice -- there was no ``daemon list`` yet to make
multi-daemon status especially useful, and that sprint's Architecture
Decision 2 meant ``find_all()`` could not discover more than the local
daemon anyway. Recorded here as a scope decision, not an oversight.

Sprint 028 (ticket 005) adds ``daemon list``, resolving that gap now
that ``Discovery.find_all()`` is real (ticket 004). It is the first
command in this file with no single-target meaning at all -- that is
what ``daemon status -d HOST`` already does -- so, unlike ``mount
list``'s default-single/opt-in-``--all`` shape, it takes no
``-d``/``--all`` flags whatsoever: listing daemons is inherently
"every daemon". It reuses ``cli/_common.py``'s ``fan_out()``
unmodified and is built entirely on the existing ``Daemon.status()``
RPC -- no new RPC method, no ``.proto`` change (see ``cmd_list``'s own
docstring). ``start``/``stop``/``restart``/``status`` above are
untouched by this addition and keep the reasoning above.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from aprilcam.cli._common import (
    EXIT_OK,
    add_daemon_option,
    fan_out,
    format_camera_row,
    incomplete_command,
    report_error,
)
from aprilcam.client.discovery import Discovery
from aprilcam.client.lifecycle import DaemonLifecycle
from aprilcam.daemon.interfaces import DaemonStatus
from aprilcam.errors import AprilCamError

__all__ = [
    "add_subparser",
    "cmd_start",
    "cmd_stop",
    "cmd_restart",
    "cmd_status",
    "cmd_list",
]


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


def cmd_restart(
    args: argparse.Namespace,
    *,
    lifecycle_factory: Callable[[], DaemonLifecycle] = DaemonLifecycle,
) -> int:
    """``aprilcam daemon restart`` -- stop (if running), then start.

    Local-only for the same structural reason as ``start``/``stop``: it
    is those two operations, so it inherits their refusal of ``-d``.

    Stopping a daemon that is not running is a quiet no-op (UC-13), so
    ``restart`` doubles as "start it, whatever state it was in" -- the
    common case after a rebuild, where the running process predates the
    current code and answers ``Method not found!`` for new RPCs.

    Configuration is reloaded from disk on the way back up (UC-01), so
    a restart re-reads ``config_dir``: an installed playfield and camera
    link survive, while anything only ever pushed over RPC does not.
    """
    if args.daemon is not None:
        return _refuse_remote("restart", args.daemon)
    lifecycle = lifecycle_factory()
    was_running = lifecycle.status().running
    try:
        lifecycle.stop()
        lifecycle.start()
    except AprilCamError as exc:
        return report_error(exc)
    status = lifecycle.status()
    print(
        f"daemon {'restarted' if was_running else 'started'} "
        f"({len(status.cameras)} camera(s))"
    )
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


def _render_daemon_list_row(label: str, status: DaemonStatus) -> None:
    """One ``daemon list`` row: *label* (already ``fan_out()``-formatted,
    e.g. ``"localhost (local)"``), then the reachable daemon's camera
    count and linked playfields -- mirroring ``cli/tags.py``'s
    ``cmd_list`` (mount list) rendering shape (label line, then indented
    summary fields), the pattern this ticket follows.

    Linked playfields are the deduplicated, sorted, non-``None``
    ``CameraState.playfield`` values across *status*'s cameras, printed
    ``(none)`` when there are none -- this codebase's established
    ``(no X)``/``(none)`` convention rather than blank output. An
    unreachable daemon never reaches this function at all: ``fan_out()``
    prints its own ``"{label}: unreachable -- {exc}"`` line and skips
    *render* entirely (SUC-004's "listed and flagged, not filtered
    out").
    """
    print(f"{label}:")
    print(f"  cameras: {len(status.cameras)}")
    playfields = sorted({c.playfield for c in status.cameras if c.playfield is not None})
    print(f"  playfields: {', '.join(playfields) if playfields else '(none)'}")


def cmd_list(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
) -> int:
    """``aprilcam daemon list``. Lists every daemon
    ``Discovery.find_all()`` reports -- local and remote -- with each
    reachable one's camera count and linked playfields, built entirely
    from the existing ``Daemon.status()`` RPC (no new RPC method, no
    ``.proto`` change; sprint architecture).

    A thin call into ``fan_out()`` (see the module docstring's "No
    ``--all`` here -- except ``list``" section): that helper already
    implements every behavioural requirement this command has --
    ``"(no daemons found)"`` plus exit 0 on an empty result (SUC-005),
    aborting via ``report_error`` on ``DiscoveryFailed`` (SUC-006),
    ``f"{host} (local)"``/bare-``host`` labelling, and printing an
    unreachable daemon flagged rather than dropping it (SUC-004) --
    so none of that is reimplemented here. Takes no ``-d``/``--all``
    flags; *args* is accepted only to match every other ``cmd_*``
    function's ``(args, *, ...)`` dispatch signature (``cli/__init__.py``
    calls ``func(args)`` uniformly) and is otherwise unused.
    """
    return fan_out(discovery_factory(), lambda daemon: daemon.status(), _render_daemon_list_row)


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``daemon start|stop|restart|status`` into the top-level parser."""
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

    p_restart = sub.add_parser(
        "restart", help="stop then start the local daemon"
    )
    add_daemon_option(p_restart)
    p_restart.set_defaults(func=cmd_restart)

    p_status = sub.add_parser("status", help="report daemon readiness")
    add_daemon_option(p_status)
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser(
        "list", help="list every discovered daemon, local and remote"
    )
    p_list.set_defaults(func=cmd_list)
