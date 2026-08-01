"""``aprilcam playfield config <file>`` (ticket 022-001).

Thin argv-to-client-call shell over ``config/loader.py`` (client-side
document validation) and ``client/daemon_client.py`` (the push RPC) --
``adapters.md``'s "adapters hold no authoritative state, adapters do no
vision" rules apply here exactly as they do in ``cli/camera.py``.

**Push only, this ticket.** Fetching a playfield back
(``get_playfield``/``list_playfields``) is exercised through the daemon
API directly (tests, and any future MCP/Python caller) -- sprint.md's
Builds section for this ticket names only the push command; a
``playfield get``/``list`` CLI verb is not part of this slice.

**Validate before any RPC.** ``ConfigLoader.load_playfield`` raises
``MalformedDocument`` (a no-``name`` document, invalid JSON, a
duplicate ArUco id, ...) *before* ``set_playfield`` is ever called --
UC-23.S-4's "a bad file never reaches the daemon."
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from aprilcam.cli._common import add_daemon_option, incomplete_command, report_error
from aprilcam.client.discovery import Discovery
from aprilcam.config.interfaces import ConfigLoader
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.errors import AprilCamError

__all__ = ["add_subparser", "cmd_config"]


def cmd_config(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    loader_factory: Callable[[], ConfigLoader] = JsonConfigLoader,
) -> int:
    """``aprilcam playfield config <file> [-d HOST]``."""
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


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``playfield config`` into the top-level parser."""
    parser = subparsers.add_parser("playfield", help="playfield definitions")
    parser.set_defaults(func=incomplete_command(parser))
    sub = parser.add_subparsers(dest="playfield_command", metavar="<command>")

    p_config = sub.add_parser("config", help="push a playfield definition document")
    p_config.add_argument("file", help="path to a playfield definition JSON file")
    add_daemon_option(p_config)
    p_config.set_defaults(func=cmd_config)
