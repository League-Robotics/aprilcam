"""``aprilcam guide [name]`` (ticket 025-007, UC-27).

**A deliberate exception to every other command in this package.**
Every other ``cli/`` module (``camera``, ``daemon``, ``playfield``,
``tags``) connects to a daemon via ``client.discovery.Discovery`` and
takes a ``-d``/``--daemon`` targeting flag (``cli/_common.py``'s
``add_daemon_option``). This module does neither, on purpose:
UC-27.S-1 requires the guide to be readable with **no daemon running
anywhere** -- plausibly the very first thing a new user or agent asks
for, before any daemon has ever been started. So this module reads
straight from ``client.guides.Guides`` (ticket 025-006), which itself
touches nothing but the package's own packaged text -- no ``Discovery``,
no ``Daemon``, no gRPC channel, no socket. Do not "fix" this to route
through ``Discovery``/``Daemon`` like every other command; that would
defeat the entire point of a guide that needs no daemon.

Otherwise this is a thin argv-to-accessor shell, the same shape as
every other ``cli/`` module (``adapters.md``'s "adapters hold no
authoritative state" rule) -- just over ``client.guides.Guides``
instead of a ``Daemon``.
"""

from __future__ import annotations

import argparse
from typing import Callable

from aprilcam.cli._common import EXIT_OK, report_error
from aprilcam.client.guides import Guides
from aprilcam.client.interfaces import Guides as GuidesBase
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_guide"]


def cmd_guide(
    args: argparse.Namespace, *, guides_factory: Callable[[], GuidesBase] = Guides
) -> int:
    """``aprilcam guide [name]`` (UC-27). Prints one packaged guide's
    text verbatim -- ``print(text, end="")`` rather than the usual bare
    ``print(text)``, so stdout is byte-identical to the guide file's own
    content (no extra trailing newline appended on top of the file's
    own), which is what the RPC path (``Daemon.get_guide``) and this
    path being provably byte-identical (ticket 025-007's Definition of
    Done) actually means in practice.

    No daemon connection is made or attempted -- see the module
    docstring. An unknown *name* raises :class:`~aprilcam.errors.
    UnknownGuide` (naming the guides that exist, UC-27.S-3), reported
    the same way every other typed error in this package is.
    """
    try:
        text = guides_factory().get(args.name)
    except AprilCamError as exc:
        return report_error(exc)
    print(text, end="")
    return EXIT_OK


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire the top-level ``guide [name]`` command in. Deliberately no
    ``-d``/``--daemon`` option and no ``--all`` -- see the module
    docstring: this is the one command in this package that never
    targets, or needs, a daemon."""
    parser = subparsers.add_parser(
        "guide",
        help="print a packaged usage guide -- works with no daemon running (UC-27)",
    )
    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help='which guide to print: "agent" (default) or "robot"',
    )
    parser.set_defaults(func=cmd_guide)
