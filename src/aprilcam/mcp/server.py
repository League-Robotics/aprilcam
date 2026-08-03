"""``FastMCP`` server assembly (ticket 026-001; sprint.md's Architecture
Step 3 ``mcp/server.py`` row: "Assembles the FastMCP instance, registers
every tool group, serves get_guide. Owns server instructions text and
tool registration; no daemon calls of its own except via the modules
above").

:func:`build_server` is the tool-registration entry point every later
ticket extends — tickets 002-005 add their own ``mcp.tools.*.register``
calls to this function's body, each handed the same ``connection``
(:class:`~aprilcam.mcp.connection.ConnectionManager`) and *temp_dir*
(:func:`~aprilcam.mcp.images.create_temp_dir`'s result) constructed
once, here, per server process. Ticket 001 registered exactly one tool,
``get_guide`` — the pattern-setter proving the whole stack end to end
with no daemon anywhere (UC-27.S-1). Ticket 002 adds the first
daemon-backed tool group, ``mcp.tools.perception`` — cameras, frames,
tags, ``where`` — via the exact
``mcp.tools.*.register(mcp, connection, temp_dir)`` shape ticket 001
left this function ready for; tickets 003-005 follow the same shape.

**Zero daemon RPCs during construction.** Neither
``ConnectionManager.__init__`` (see its module docstring) nor
``create_temp_dir`` (a local ``tempfile.mkdtemp`` call) touches a
daemon, so ``build_server`` always succeeds, with or without one
running anywhere (UC-22.S-2) — the structural property the rest of
this sprint's tools depend on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from mcp.server.fastmcp import FastMCP

from aprilcam.client.guides import Guides
from aprilcam.client.interfaces import Guides as GuidesBase
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.errors import handle_tool_errors
from aprilcam.mcp.images import create_temp_dir
from aprilcam.mcp.tools import configuration, mobile_tags, perception

# Aliased, not `from aprilcam.mcp.tools import annotations` alongside the
# rest -- this module's own (mandatory, must-be-first-statement) `from
# __future__ import annotations` above already binds the local name
# `annotations` to `__future__`'s `_Feature` object; unaliased, the
# second binding would still work at runtime (last assignment wins) but
# mypy correctly flags the two incompatible types for the same name
# (see `mcp/tools/__init__.py`'s docstring for the fuller story of this
# collision, first hit by ticket 026-005).
from aprilcam.mcp.tools import annotations as annotation_tools

__all__ = ["build_server"]

#: Orientation instructions surfaced to the agent as the server's own
#: MCP ``instructions`` field (sprint.md's Architecture Step 3: "Owns
#: server instructions text"). States the golden path once, in one
#: place, rather than leaving an agent to discover call order by trial
#: and error: read the guide first (works with no daemon at all), then
#: perceive, then configure.
_SERVER_INSTRUCTIONS = (
    "AprilCam: perception and configuration for a camera-observed "
    "playfield. This server starts unbound and resolves a daemon per "
    "tool call, so it works even before any daemon is running and "
    "survives a daemon restart mid-session -- a tool call may fail "
    "with a connection error, but the server itself stays up and the "
    "next call may succeed.\n\n"
    "Golden path: call get_guide() first -- it needs no daemon at all "
    "and explains world-frame conventions, tag addressing, and how "
    "this server's other tools fit together. Then use the perception "
    "tools to see the field (cameras, tags, frames) before using the "
    "configuration tools to change it.\n\n"
    "Every daemon-backed tool accepts an optional daemon parameter to "
    "target a specific host for one call; omitted, it uses the "
    "daemon this server was started against (aprilcam mcp -d HOST), "
    "or the local daemon if none was given."
)


def build_server(
    *,
    default_daemon_host: Optional[str] = None,
    connection_factory: Callable[..., ConnectionManager] = ConnectionManager,
    guides_factory: Callable[[], GuidesBase] = Guides,
    temp_dir_factory: Callable[[], Path] = create_temp_dir,
) -> FastMCP:
    """Assemble and return a ready-to-run ``FastMCP("aprilcam", ...)``
    instance. Does not start serving -- the caller (``cli/mcp.py``)
    decides the transport and blocks on ``FastMCP.run()``.

    Parameters
    ----------
    default_daemon_host:
        The session default daemon target -- ``aprilcam mcp -d HOST``'s
        value, or ``None`` for the local daemon. Seeds the
        ``ConnectionManager``'s default; any individual tool call's own
        ``daemon`` argument (tickets 002-005) overrides it for that one
        call.
    connection_factory:
        Builds the ``ConnectionManager`` every daemon-backed tool
        (tickets 002-005) resolves its ``Daemon`` through. Defaults to
        the real ``ConnectionManager``; tests inject a fake to script
        cache/failure behaviour without a real daemon.
    guides_factory:
        Builds the ``Guides`` accessor ``get_guide`` reads from
        directly -- see the module docstring and ``get_guide``'s own
        docstring for why this is the one tool that never goes through
        *connection_factory* at all.
    temp_dir_factory:
        Builds the per-process temp directory ``mcp/images.py``'s
        ``format="file"`` image responses (ticket 002's ``get_frame``)
        write under. Defaults to ``images.create_temp_dir``; tests
        inject a stub to avoid touching the real filesystem for tests
        that never exercise an image tool.
    """
    # Constructed once per server process, shared by every tool group
    # registered below -- see the module docstring. Neither call
    # performs a daemon RPC or requires one to exist (UC-22.S-2):
    # `ConnectionManager.__init__` only stores its arguments (its own
    # module docstring), and `temp_dir_factory`'s default is a local
    # `tempfile.mkdtemp` call.
    connection = connection_factory(default_host=default_daemon_host)
    temp_dir = temp_dir_factory()

    mcp = FastMCP("aprilcam", instructions=_SERVER_INSTRUCTIONS)

    @mcp.tool()
    @handle_tool_errors
    def get_guide(name: Optional[str] = None) -> str:
        """The packaged usage guide (UC-27) -- read this first. Reads
        the ``"agent"`` guide by default, or the guide *name* names
        (currently ``"agent"`` or ``"robot"``). Explains world-frame
        conventions, tag addressing, and how this server's tools fit
        together. Works with **no daemon running anywhere** -- unlike
        every other tool this server exposes, this one never resolves
        a daemon connection at all, so it is always safe to call first,
        even before ``aprilcam daemon start``.
        """
        # Deliberate exception to every other tool this package
        # registers (tickets 002-005): reads `client.guides.Guides`
        # directly, with no `ConnectionManager`, no `Discovery`, no
        # `Daemon` call of any kind -- mirroring `cli/guide.py`'s
        # identical, equally deliberate exception (UC-27.S-1: must work
        # before any daemon exists). Do not "fix" this to route through
        # `connection`/`Discovery` like a normal tool; that would defeat
        # the entire point of a guide that needs no daemon.
        return guides_factory().get(name)

    # Ticket 002: the read-only perception surface (cameras, frames,
    # tags, `where`) -- see `mcp/tools/perception.py`'s module
    # docstring. Every later ticket's tool group (003-005) extends this
    # function's body with its own
    # `mcp.tools.*.register(mcp, connection, temp_dir)` call against
    # these exact locals, matching this one.
    perception.register(mcp, connection, temp_dir)

    # Ticket 003: whole-document camera/playfield configuration and
    # calibration -- see `mcp/tools/configuration.py`'s module
    # docstring. Same `register(mcp, connection, temp_dir)` shape.
    configuration.register(mcp, connection, temp_dir)

    # Ticket 004: the mobile-tag mount registry -- see `mcp/tools/
    # mobile_tags.py`'s module docstring. Same `register(mcp,
    # connection, temp_dir)` shape; independent of tickets 002/003/005.
    mobile_tags.register(mcp, connection, temp_dir)

    # Ticket 005: the annotation/drawing surface -- see `mcp/tools/
    # annotations.py`'s module docstring. Same `register(mcp,
    # connection, temp_dir)` shape; independent of tickets 002/003/004 --
    # the last tool-group registration this function gains (ticket 006
    # is integration/acceptance only, no new tool group). Imported as
    # `annotation_tools` above, not `annotations` -- see that import's
    # own comment for why.
    annotation_tools.register(mcp, connection, temp_dir)

    return mcp
