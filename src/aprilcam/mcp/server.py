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
once, here, per server process. This ticket registers exactly one tool,
``get_guide`` — the pattern-setter proving the whole stack end to end
with no daemon anywhere (UC-27.S-1) — plus the two locals future
tickets' tool groups need.

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
    # Constructed once per server process, ready for tickets 002-005's
    # tool groups to use via `connection`/`temp_dir` -- see the module
    # docstring. Neither call performs a daemon RPC or requires one to
    # exist (UC-22.S-2): `ConnectionManager.__init__` only stores its
    # arguments (its own module docstring), and `temp_dir_factory`'s
    # default is a local `tempfile.mkdtemp` call.
    connection = connection_factory(default_host=default_daemon_host)  # noqa: F841
    temp_dir = temp_dir_factory()  # noqa: F841
    # Neither `connection` nor `temp_dir` is referenced by any tool in
    # *this* ticket -- `get_guide` below is the one deliberate
    # exception that needs neither (see its docstring). They are built
    # here, unconditionally, so that (a) this function proves
    # construction alone never touches a daemon, independent of which
    # tools happen to be registered, and (b) tickets 002-005 extend
    # this function's body with their own
    # `mcp.tools.*.register(mcp, connection, temp_dir)` calls against
    # these exact locals, rather than re-deriving them.

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
        # Deliberate exception to every other tool this package will
        # register (tickets 002-005): reads `client.guides.Guides`
        # directly, with no `ConnectionManager`, no `Discovery`, no
        # `Daemon` call of any kind -- mirroring `cli/guide.py`'s
        # identical, equally deliberate exception (UC-27.S-1: must work
        # before any daemon exists). Do not "fix" this to route through
        # `connection`/`Discovery` like a normal tool; that would defeat
        # the entire point of a guide that needs no daemon.
        return guides_factory().get(name)

    return mcp
