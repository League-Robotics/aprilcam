"""Tests for ``aprilcam.mcp.errors.handle_tool_errors`` (ticket
026-001).

Two levels. First, the decorator's own mapping behaviour, tested
directly against Python exceptions with no FastMCP involved -- fast,
and pins the exact table sprint.md's Architecture Design Rationale
states. Second, the SDK-level shape those mapped exceptions actually
produce once carried through a *real* ``FastMCP`` tool call --
proving the mechanism this module's docstring documents finding
(raising the SDK's own ``ToolError`` produces a clean
``CallToolResult(isError=True, ...)``, never a crashed transport, never
a leaked traceback) rather than merely asserting it.
"""

from __future__ import annotations

import logging

import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from aprilcam.errors import (
    AlreadyRunning,
    CameraNotPresent,
    ConnectionFailed,
    DiscoveryFailed,
    InvalidTagId,
    MalformedDocument,
    NoPlayfieldLinked,
    NotCalibrated,
    UnknownCamera,
    UnknownGuide,
    UnknownPlayfield,
    UnsupportedSetting,
)
from aprilcam.mcp.errors import handle_tool_errors

# ---------------------------------------------------------------------------
# every AprilCamError subtype -> ToolError(str(exc)) verbatim
# ---------------------------------------------------------------------------

_APRILCAM_ERROR_CASES = [
    ConnectionFailed("pi.local", reason="no answer"),
    DiscoveryFailed("multicast blocked"),
    UnknownCamera("front", ("back", "side")),
    UnknownPlayfield("arena9", ("arena1",)),
    UnknownGuide("nonexistent", ("agent", "robot")),
    CameraNotPresent("front"),
    NotCalibrated("camera not calibrated"),
    NoPlayfieldLinked("front"),
    UnsupportedSetting("gain_db"),
    MalformedDocument("document has no 'name' field"),
    InvalidTagId("aruco", 999, range(0, 50)),
    AlreadyRunning("pid 4242"),
]


@pytest.mark.parametrize("exc", _APRILCAM_ERROR_CASES, ids=lambda e: type(e).__name__)
def test_every_aprilcam_error_maps_to_a_tool_error_with_its_message_passed_through_verbatim(exc):
    @handle_tool_errors
    def boom():
        raise exc

    with pytest.raises(ToolError) as excinfo:
        boom()

    assert str(excinfo.value) == str(exc)
    assert excinfo.value.__cause__ is exc


# ---------------------------------------------------------------------------
# any other exception -> generic internal error; full traceback logged
# server-side; never echoed to the caller
# ---------------------------------------------------------------------------


def test_unexpected_exception_maps_to_a_generic_internal_error_naming_only_the_type():
    @handle_tool_errors
    def boom():
        raise ValueError("some internal detail an agent must never see")

    with pytest.raises(ToolError) as excinfo:
        boom()

    message = str(excinfo.value)
    assert message == "internal error: ValueError"
    assert "some internal detail" not in message


def test_unexpected_exception_is_logged_server_side_with_a_full_traceback(caplog):
    @handle_tool_errors
    def boom():
        raise RuntimeError("boom detail")

    with caplog.at_level(logging.ERROR, logger="aprilcam.mcp.errors"):
        with pytest.raises(ToolError):
            boom()

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.exc_info is not None
    assert "boom detail" in caplog.text


def test_success_path_passes_the_return_value_through_unchanged():
    @handle_tool_errors
    def double(x):
        return x * 2

    assert double(21) == 42


# ---------------------------------------------------------------------------
# async tool functions -- decorator transparency (tickets 002-005 may
# register either shape)
# ---------------------------------------------------------------------------


async def test_async_tool_function_maps_aprilcam_errors_the_same_way():
    @handle_tool_errors
    async def boom():
        raise UnknownCamera("front", ())

    with pytest.raises(ToolError, match="unknown camera 'front'"):
        await boom()


async def test_async_tool_function_success_path():
    @handle_tool_errors
    async def increment(x):
        return x + 1

    assert await increment(1) == 2


# ---------------------------------------------------------------------------
# decorator transparency to FastMCP's own argument-schema introspection
# ---------------------------------------------------------------------------


async def test_decorator_preserves_the_wrapped_functions_argument_schema():
    server = FastMCP("test")

    @server.tool()
    @handle_tool_errors
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    tools = await server.list_tools()
    (tool,) = [t for t in tools if t.name == "add"]
    assert set(tool.inputSchema["required"]) == {"a", "b"}


# ---------------------------------------------------------------------------
# the confirmed SDK mechanism, end to end: a real FastMCP tool call
# ---------------------------------------------------------------------------


async def _call(server: FastMCP, name: str, arguments=None):
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    return await handler(req)


async def test_raising_through_a_real_fastmcp_tool_call_produces_a_clean_is_error_result():
    """The exact SDK-level shape this module's docstring documents
    finding: no traceback, no crashed transport, isError=True, and the
    server stays usable for the next call."""
    server = FastMCP("test")

    @server.tool()
    @handle_tool_errors
    def unknown_camera_tool() -> str:
        raise UnknownCamera("front", ("back", "side"))

    @server.tool()
    @handle_tool_errors
    def unexpected_tool() -> str:
        raise ValueError("leak me not")

    @server.tool()
    @handle_tool_errors
    def ok_tool() -> str:
        return "fine"

    known = await _call(server, "unknown_camera_tool")
    assert known.root.isError is True
    known_text = known.root.content[0].text
    assert "unknown camera 'front'" in known_text
    assert "Traceback" not in known_text

    unexpected = await _call(server, "unexpected_tool")
    assert unexpected.root.isError is True
    unexpected_text = unexpected.root.content[0].text
    assert "leak me not" not in unexpected_text
    assert "internal error: ValueError" in unexpected_text
    assert "Traceback" not in unexpected_text

    ok = await _call(server, "ok_tool")
    assert ok.root.isError is False

    # the server/transport is still usable after two errors -- UC-22's
    # "the server stays up" failure-mode contract, at the tool-call
    # boundary this module owns.
    ok_again = await _call(server, "ok_tool")
    assert ok_again.root.isError is False
