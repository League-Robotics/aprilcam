"""``aprilcam mcp [-d HOST]`` -- starts the MCP stdio server (ticket
026-001, UC-22).

Thin argv-to-server shell, the same shape as every other ``cli/``
module (``adapters.md``'s "adapters hold no authoritative state" rule):
parses ``-d``/``--daemon`` via ``cli/_common.py``'s
``add_daemon_option`` and passes it straight through as
``mcp.server.build_server``'s ``default_daemon_host`` -- the connection
manager's session default (sprint.md's Design Rationale: a flag-seeded
default, plus an optional per-tool-call ``daemon`` parameter later
tickets add, resolving UC-22's own open question).

**No daemon is contacted during argument parsing or server
construction.** ``build_server`` performs no RPC of its own (see its
module docstring), so ``aprilcam mcp`` always starts cleanly, with or
without a daemon running anywhere (UC-22.S-2) -- unlike
``daemon start``/``stop``, there is nothing here to refuse or defer.
"""

from __future__ import annotations

import argparse
from typing import Callable

from mcp.server.fastmcp import FastMCP

from aprilcam.cli._common import add_daemon_option
from aprilcam.mcp.server import build_server

__all__ = ["add_subparser", "cmd_mcp"]


def cmd_mcp(
    args: argparse.Namespace, *, server_factory: Callable[..., FastMCP] = build_server
) -> int:
    """``aprilcam mcp [-d HOST]``. Builds the server and serves it over
    stdio. ``FastMCP.run()`` blocks for the life of the process -- this
    only returns once the transport closes (the agent disconnects, or
    the process receives a signal), so a successful run has no
    meaningful exit code beyond 0.
    """
    server = server_factory(default_daemon_host=args.daemon)
    server.run(transport="stdio")
    return 0


def add_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Wire ``aprilcam mcp [-d HOST]`` into the top-level parser."""
    parser = subparsers.add_parser(
        "mcp",
        help="run the MCP stdio server for AI agents (UC-22)",
    )
    add_daemon_option(parser)
    parser.set_defaults(func=cmd_mcp)
