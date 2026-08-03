"""Tests for ``aprilcam.mcp.server.build_server`` (ticket 026-001).

UC-27.S-1's "no daemon contact" is proven the same way
``test_guide_cli.py`` proves it for the CLI's ``guide`` command: a
dynamic, mechanical poison test -- a ``Discovery`` whose ``connect()``
(and ``find_all()``) unconditionally raise, reachable only through
``connection_factory -> ConnectionManager -> Discovery.connect``. If
``get_guide``'s tool body ever resolved a daemon connection, these
tests would fail with the poison's own ``AssertionError`` instead of a
clean tool result.

``get_guide`` itself is exercised as a real MCP tool call through the
same low-level ``CallToolRequest`` handler ``test_errors.py`` uses --
proving the whole stack (``build_server`` -> ``@mcp.tool()`` ->
``handle_tool_errors`` -> ``Guides``) end to end, not just argument
wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from mcp import types

from aprilcam.client.guides import Guides
from aprilcam.errors import UnknownGuide
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server


class _PoisonedDiscovery:
    """A ``Discovery`` whose ``connect``/``find_all`` unconditionally
    raise -- proves ``get_guide``'s runtime path never reaches
    ``Discovery.connect``, however it might get there."""

    def connect(self, host: Optional[str] = None):
        raise AssertionError(
            "get_guide must never resolve a daemon connection (UC-27.S-1)"
        )

    def find_all(self):
        raise AssertionError("get_guide must never call Discovery.find_all either")


def _poisoned_connection_factory(*, default_host: Optional[str] = None) -> ConnectionManager:
    return ConnectionManager(default_host=default_host, discovery_factory=_PoisonedDiscovery)


class _FakeGuides:
    """A minimal ``client.interfaces.Guides``-shaped double, matching
    ``test_guide_cli.py``'s own fake-injection convention -- isolates
    the ``get_guide`` tool's argv-to-call wiring from the real packaged
    text."""

    def __init__(self, text: str = "fake guide text", raises: Optional[Exception] = None) -> None:
        self._text = text
        self._raises = raises
        self.requested_names: List[Optional[str]] = []

    def get(self, name: Optional[str] = None) -> str:
        self.requested_names.append(name)
        if self._raises is not None:
            raise self._raises
        return self._text

    def available(self):
        return ("agent", "robot")


def _fake_temp_dir_factory(tmp_path: Path):
    def _factory() -> Path:
        return tmp_path

    return _factory


async def _call_tool(server, name: str, arguments=None):
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    return await handler(req)


# ---------------------------------------------------------------------------
# construction -- never touches a daemon (UC-22.S-2)
# ---------------------------------------------------------------------------


def test_build_server_succeeds_even_with_a_poisoned_connection_factory(tmp_path):
    server = build_server(
        connection_factory=_poisoned_connection_factory,
        temp_dir_factory=_fake_temp_dir_factory(tmp_path),
    )
    assert server is not None


def test_build_server_calls_its_factories_exactly_once_each():
    connection_calls: List[Optional[str]] = []
    temp_dir_calls: List[bool] = []

    def connection_factory(*, default_host: Optional[str] = None) -> ConnectionManager:
        connection_calls.append(default_host)
        return ConnectionManager(default_host=default_host, discovery_factory=_PoisonedDiscovery)

    def temp_dir_factory() -> Path:
        temp_dir_calls.append(True)
        return Path("/tmp/aprilcam-mcp-test-does-not-need-to-exist")

    build_server(
        default_daemon_host="pi.local",
        connection_factory=connection_factory,
        temp_dir_factory=temp_dir_factory,
    )

    assert connection_calls == ["pi.local"]
    assert len(temp_dir_calls) == 1


def test_build_server_returns_a_fastmcp_instance_with_get_guide_registered(tmp_path):
    import asyncio

    server = build_server(
        connection_factory=_poisoned_connection_factory,
        temp_dir_factory=_fake_temp_dir_factory(tmp_path),
    )

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "get_guide" in names


# ---------------------------------------------------------------------------
# get_guide -- UC-27.S-1: zero daemon contact, real tool call
# ---------------------------------------------------------------------------


async def test_get_guide_succeeds_with_a_poisoned_connection_factory(tmp_path):
    server = build_server(
        connection_factory=_poisoned_connection_factory,
        temp_dir_factory=_fake_temp_dir_factory(tmp_path),
    )

    result = await _call_tool(server, "get_guide")

    assert result.root.isError is False


async def test_get_guide_no_name_defaults_to_none(tmp_path):
    fake = _FakeGuides()
    server = build_server(
        guides_factory=lambda: fake,
        connection_factory=_poisoned_connection_factory,
        temp_dir_factory=_fake_temp_dir_factory(tmp_path),
    )

    await _call_tool(server, "get_guide")

    assert fake.requested_names == [None]


async def test_get_guide_passes_a_given_name_through(tmp_path):
    fake = _FakeGuides()
    server = build_server(
        guides_factory=lambda: fake,
        connection_factory=_poisoned_connection_factory,
        temp_dir_factory=_fake_temp_dir_factory(tmp_path),
    )

    await _call_tool(server, "get_guide", {"name": "robot"})

    assert fake.requested_names == ["robot"]


async def test_get_guide_returns_the_guide_text(tmp_path):
    fake = _FakeGuides(text="hello there")
    server = build_server(
        guides_factory=lambda: fake,
        connection_factory=_poisoned_connection_factory,
        temp_dir_factory=_fake_temp_dir_factory(tmp_path),
    )

    result = await _call_tool(server, "get_guide")

    assert result.root.content[0].text == "hello there"


async def test_get_guide_unknown_name_maps_to_a_clean_tool_error(tmp_path):
    fake = _FakeGuides(raises=UnknownGuide("nonexistent", ("agent", "robot")))
    server = build_server(
        guides_factory=lambda: fake,
        connection_factory=_poisoned_connection_factory,
        temp_dir_factory=_fake_temp_dir_factory(tmp_path),
    )

    result = await _call_tool(server, "get_guide", {"name": "nonexistent"})

    assert result.root.isError is True
    text = result.root.content[0].text
    assert "agent" in text
    assert "robot" in text
    assert "Traceback" not in text


async def test_get_guide_against_the_real_guides_accessor_returns_real_content(tmp_path):
    """No fakes at all -- proves the default wiring round-trips through
    the real packaged guide text, matching ``test_guide_cli.py``'s own
    "real content" tests."""
    server = build_server(temp_dir_factory=_fake_temp_dir_factory(tmp_path))

    result = await _call_tool(server, "get_guide")

    assert result.root.isError is False
    assert result.root.content[0].text == Guides().get("agent")


async def test_get_guide_agent_name_matches_the_real_agent_guide(tmp_path):
    server = build_server(temp_dir_factory=_fake_temp_dir_factory(tmp_path))

    result = await _call_tool(server, "get_guide", {"name": "agent"})

    assert result.root.content[0].text == Guides().get("agent")


async def test_get_guide_robot_name_matches_the_real_robot_guide(tmp_path):
    server = build_server(temp_dir_factory=_fake_temp_dir_factory(tmp_path))

    result = await _call_tool(server, "get_guide", {"name": "robot"})

    assert result.root.content[0].text == Guides().get("robot")
