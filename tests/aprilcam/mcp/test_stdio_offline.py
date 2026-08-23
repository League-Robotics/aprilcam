"""Real ``aprilcam mcp`` subprocess, over real stdio (ticket 035-009,
UC-22).

**Why this module exists alongside ``test_integration.py``.**
``test_integration.py``'s own tests already prove the "zero daemon RPCs
during construction" invariant (``test_no_daemon_at_startup_then_recovers_once_one_starts``)
and drive full tool calls -- but only through
``mcp.shared.memory.create_connected_server_and_client_session``, an
in-memory transport wired directly to a ``FastMCP`` Python object built
in-process. That proves the *tool logic* and the *session protocol*,
but never exercises ``cli/mcp.py``'s ``cmd_mcp`` (the lazy ``mcp`` SDK
import, argv parsing, ``server.run(transport="stdio")``), the real
``aprilcam`` console-script entry point, or real stdio framing across a
genuine process boundary. This module is sprint 035's L1 layer: it
spawns the actual installed binary as a child process and speaks real
JSON-RPC-over-stdio to it, the way an external MCP client (an AI agent)
actually would.

**Client chosen: the ``mcp`` SDK's own ``mcp.client.stdio``, not the
v1 fallback.** The ticket's plan named a hand-rolled JSON-RPC client
(``git show 65510e4^:tests/system/test_mcp_subprocess.py`` in the
AprilTags repo) as a fallback for if ``asyncio_mode=auto`` bridging
proved awkward. It did not: ``mcp.client.stdio.stdio_client`` +
``mcp.ClientSession`` are themselves ``anyio``-based async context
managers, and this repo's ``tests/aprilcam/mcp/test_integration.py``
already runs structurally identical ``async def test_...`` functions
under this project's ``asyncio_mode = "auto"`` pytest-asyncio config
with no special handling. No fallback was needed.

**Finding: the server does NOT eagerly connect to a daemon.** A full
``initialize`` handshake plus ``tools/list`` against a real subprocess,
run with ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``/
``XDG_RUNTIME_DIR`` pointed at a private, freshly-created tmp root
(so no daemon could possibly be reachable at the resolved
``socket_dir``), completes successfully and leaves that socket_dir
with **no control-plane socket file** at any point -- proving, not
merely assuming from ``mcp/DESIGN.md``'s stated invariant, that
``build_server``/``cmd_mcp`` perform zero daemon RPCs up through
``tools/list``. This test is therefore left unmarked (no ``camera``,
no ``integration``) -- it needs no daemon and no hardware, matching
the ticket's Description.

**Isolation.** Never touches the stakeholder's real
``~/.config/aprilcam`` / ``~/.local/share/aprilcam`` roots or the real,
currently-running daemon's control socket. Follows
``test_camera_geometry_hardware.py:204-227``'s ``cli_env`` fixture
pattern exactly: a short top-level ``/tmp`` directory for
``XDG_RUNTIME_DIR`` (unix domain sockets have a ~103-character path
limit -- ``tmp_path``'s deep nesting would blow past it), plus a
skip-if-not-isolatable guard for sandboxes where ``/run/aprilcam`` is
itself writable and would win the resolution cascade over
``XDG_RUNTIME_DIR``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterator, Tuple

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import SOCKET_FILENAME

#: The pinned, explicit tool inventory (ticket 035-009's Acceptance
#: Criteria: "named in the test, not derived from mcp/DESIGN.md's
#: prose, so a change to either is caught"). Hand-enumerated against
#: every ``@mcp.tool()`` registration as of this ticket:
#: ``server.py`` (1) + ``tools/perception.py`` (7) +
#: ``tools/configuration.py`` (8) + ``tools/mobile_tags.py`` (3) +
#: ``tools/annotations.py`` (8) = 27. This is a SUPERSET assertion
#: against the live server's ``tools/list`` -- a server that grows a
#: 28th tool must not break this test, but one that loses any of these
#: 27 must.
_EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        # server.py -- the one tool that works with no daemon at all.
        "get_guide",
        # tools/perception.py -- read-only queries.
        "list_cameras",
        "probe_cameras",
        "get_frame",
        "get_tags",
        "get_tag",
        "get_tag_history",
        "where",
        # tools/configuration.py -- whole-document push/fetch + calibration.
        "get_camera_config",
        "set_camera_config",
        "reset_camera_config",
        "get_playfield",
        "set_playfield",
        "list_playfields",
        "calibrate",
        "get_calibration",
        # tools/mobile_tags.py -- mount-registry.
        "register_tag",
        "unregister_tag",
        "list_tag_parameters",
        # tools/annotations.py -- world-coordinate drawing.
        "draw_marker",
        "draw_path",
        "draw_polygon",
        "draw_circle",
        "draw_text",
        "remove_annotation",
        "clear_layer",
        "list_annotations",
    }
)


def test_pinned_inventory_is_the_27_tools_the_ticket_names() -> None:
    """A fast, no-subprocess guard on the constant above -- if someone
    edits ``_EXPECTED_TOOLS`` and miscounts, this fails immediately
    instead of surfacing as a confusing superset-assertion failure in
    the real subprocess test below."""
    assert len(_EXPECTED_TOOLS) == 27, sorted(_EXPECTED_TOOLS)


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests too deeply (matches
    ``test_camera_geometry_hardware.py``'s and ``test_integration.py``'s
    identically-named helper)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def _aprilcam_bin() -> Path:
    """The real, installed ``aprilcam`` console script next to the
    interpreter running this test -- not ``python -m aprilcam`` or any
    other indirection, so this test spawns exactly what an operator or
    agent actually invokes. Skips cleanly (not a failure) if this
    environment has no such script, matching every other
    real-subprocess module's convention."""
    candidate = Path(sys.executable).parent / "aprilcam"
    if not candidate.exists():  # pragma: no cover - environment-dependent
        pytest.skip(f"no aprilcam console script next to {sys.executable}")
    return candidate


@pytest.fixture
def isolated_mcp_env(tmp_path, monkeypatch) -> Iterator[Tuple[Dict[str, str], Path]]:
    """Isolated environment for a REAL ``aprilcam mcp`` subprocess --
    private ``config``/``state`` roots and a private ``socket_dir``, so
    this test can never reach the stakeholder's real
    ``~/.config/aprilcam`` roots or his real, currently-running
    daemon's control socket (matches
    ``test_camera_geometry_hardware.py:204-227``'s ``cli_env`` fixture
    and ``test_integration.py``'s ``real_directories`` fixture).

    Yields ``(env, socket_path)`` -- the full environment dict to hand
    the subprocess (via ``mcp.client.stdio.StdioServerParameters.env``)
    and the control-plane socket path *this* isolated ``socket_dir``
    would resolve to, so the test can assert that path never comes
    into existence.
    """
    short_dir = _short_tmp_dir("ac-mcp-stdio-")
    try:
        monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))

        directories = Directories()
        if directories.socket_dir != short_dir / "aprilcam":
            pytest.skip(
                "this sandbox's /run/aprilcam is available and writable, "
                "so socket_dir cannot be isolated down to a private tmp "
                "root -- cannot safely isolate a real aprilcam mcp "
                "subprocess from the stakeholder's real, currently-running "
                "daemon here"
            )

        socket_path = directories.socket_dir / SOCKET_FILENAME
        yield dict(os.environ), socket_path
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


async def test_stdio_handshake_and_tool_inventory(isolated_mcp_env) -> None:
    """Spawns a real ``aprilcam mcp`` subprocess over stdio, completes
    the MCP ``initialize`` handshake, calls ``tools/list``, and asserts
    the result is a superset of the pinned 27-tool inventory -- while
    proving, at every checkpoint, that no daemon socket ever appears
    under this test's isolated ``socket_dir``."""
    env, socket_path = isolated_mcp_env
    aprilcam_bin = _aprilcam_bin()

    assert not socket_path.exists(), (
        "sanity check on the isolation fixture itself: a freshly created "
        "socket_dir must start with no control-plane socket -- if this "
        "fails, the isolation is broken, not the server under test"
    )

    params = StdioServerParameters(command=str(aprilcam_bin), args=["mcp"], env=env)

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=timedelta(seconds=20)
        ) as session:
            with anyio.fail_after(20):
                init_result = await session.initialize()
            assert init_result.serverInfo.name == "aprilcam", init_result.serverInfo

            with anyio.fail_after(20):
                tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}

            # The core acceptance check: no eager daemon connect as a
            # side effect of a full initialize + tools/list round trip,
            # verified while the session (and subprocess) is still
            # alive -- not merely assumed from mcp/DESIGN.md's stated
            # "zero daemon RPCs during construction" invariant.
            assert not socket_path.exists(), (
                "aprilcam mcp created a control-plane socket during "
                "initialize+tools/list with no daemon involved -- this "
                "contradicts mcp/DESIGN.md's 'zero daemon RPCs during "
                "construction' invariant; per this ticket's acceptance "
                "criteria this test must be re-marked @pytest.mark.integration "
                "and the finding recorded here rather than silently "
                "worked around"
            )

    # ``async with stdio_client(...)`` has now run its full MCP-spec
    # shutdown sequence (close stdin -> wait -> SIGTERM -> SIGKILL) --
    # see mcp.client.stdio's own docstring/source. No orphaned
    # `aprilcam mcp` process survives this test, in the pass, fail, or
    # error case alike, because that teardown lives in the context
    # manager's `finally` and always runs when the `async with` block
    # is exited, exception or not.

    missing = _EXPECTED_TOOLS - tool_names
    assert not missing, f"pinned tool inventory regressed -- missing: {sorted(missing)}"
    # Deliberately no assertion that `tool_names == _EXPECTED_TOOLS` or
    # that there is nothing extra: this is a superset assertion by
    # design (ticket 035-009's Acceptance Criteria) -- a server that
    # grows a new tool must not fail this test.

    assert not socket_path.exists(), (
        "no daemon socket must exist even after the subprocess has been "
        "fully torn down -- confirms nothing was created and abandoned "
        "on the way out either"
    )
