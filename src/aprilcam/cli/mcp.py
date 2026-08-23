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

**The ``mcp`` SDK import is lazy, deliberately (OOP fix, base-install
crash).** ``aprilcam.mcp.server`` (and everything under
``aprilcam.mcp.tools``) imports ``mcp.server.fastmcp`` -- the Model
Context Protocol Python SDK -- at module top level, which is exactly
right for that package: it *is* the MCP server implementation. The bug
this module previously had was importing that whole chain from here,
at *this* module's own top level, via
``from aprilcam.mcp.server import build_server``. Since
``cli/__init__.py`` imports every subcommand module unconditionally at
CLI startup (before ``main()`` even runs), that one eager import meant
an environment where the ``mcp`` package was missing or broken (a
stale/partial install, not merely "the daemon extra wasn't installed")
could not run *any* ``aprilcam`` subcommand -- not ``--version``, not
``--help``, not ``camera``/``daemon``/``playfield``/``tags``/``guide``
-- because the crash happened during module import, before argparse
even dispatches. ``_import_build_server`` below defers that whole
import until ``aprilcam mcp`` is actually invoked (:func:`cmd_mcp`),
and converts a missing/broken ``mcp`` install into a typed
``errors.MissingOptionalDependency`` -- reported the same way every
other subcommand reports a typed error (``cli/_common.py``'s
``report_error``) -- instead of a raw ``ModuleNotFoundError``
traceback. Every other subcommand is unaffected either way; this
module is the only place that needs the ``mcp`` SDK at all.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Callable, Optional

from aprilcam.cli._common import add_daemon_option, report_error
from aprilcam.errors import AprilCamError, MissingOptionalDependency

if TYPE_CHECKING:
    # Only for type-checkers: `from __future__ import annotations` makes
    # every annotation in this file a deferred string, so this import
    # never runs at CLI startup or at `aprilcam mcp` invocation time --
    # see the module docstring for why that must stay true.
    from mcp.server.fastmcp import FastMCP

__all__ = ["add_subparser", "cmd_mcp"]


def _is_missing_mcp_sdk(exc: ModuleNotFoundError) -> bool:
    """True if *exc* names the ``mcp`` SDK itself (or one of its
    submodules, e.g. ``mcp.server.fastmcp``) as the module that could
    not be found, as opposed to some unrelated import failure that
    happened to occur partway through importing
    ``aprilcam.mcp.server``. Split out from :func:`_import_build_server`
    so this classification is unit-testable against constructed
    exceptions, without needing a real broken ``mcp`` install to
    exercise it.
    """
    name = exc.name or ""
    return name == "mcp" or name.startswith("mcp.")


def _import_build_server() -> Callable[..., "FastMCP"]:
    """Import ``aprilcam.mcp.server.build_server`` lazily, at call time.

    Raises :class:`~aprilcam.errors.MissingOptionalDependency` -- not a
    raw ``ModuleNotFoundError`` -- if the ``mcp`` SDK is missing or
    lacks the ``mcp.server.fastmcp`` module the way this project's
    ``mcp>=1.0`` base dependency expects (e.g. a stale or partial
    install: ``mcp`` itself present but an old enough version that it
    never shipped ``fastmcp``). Any other ``ModuleNotFoundError`` --
    one unrelated to the ``mcp`` package (see :func:`_is_missing_mcp_sdk`)
    -- is a real bug and is left to propagate rather than being
    misreported as a missing dependency.
    """
    try:
        from aprilcam.mcp.server import build_server
    except ModuleNotFoundError as exc:
        if not _is_missing_mcp_sdk(exc):
            raise
        raise MissingOptionalDependency(
            "aprilcam mcp",
            "mcp",
            install_hint=(
                "install or upgrade the MCP Python SDK with "
                "'pip install --upgrade mcp' (it ships as a base "
                "aprilcam dependency, so reinstalling aprilcam -- "
                "'pip install --upgrade aprilcam' / 'pipx reinstall "
                "aprilcam' -- should also pull in a working copy)"
            ),
        ) from exc
    return build_server


def cmd_mcp(
    args: argparse.Namespace,
    *,
    server_factory: Optional[Callable[..., "FastMCP"]] = None,
) -> int:
    """``aprilcam mcp [-d HOST]``. Builds the server and serves it over
    stdio. ``FastMCP.run()`` blocks for the life of the process -- this
    only returns once the transport closes (the agent disconnects, or
    the process receives a signal), so a successful run has no
    meaningful exit code beyond 0.

    *server_factory* defaults to ``None`` rather than binding
    ``build_server`` directly as the parameter default, specifically so
    the ``mcp`` SDK import stays deferred to inside this function body
    (a default value is evaluated at function-definition time, i.e. at
    module import) -- see the module docstring. Tests inject their own
    fake factory here exactly as before; that path never touches the
    real import at all.
    """
    factory = server_factory
    try:
        if factory is None:
            factory = _import_build_server()
        server = factory(default_daemon_host=args.daemon)
    except AprilCamError as exc:
        return report_error(exc)
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
