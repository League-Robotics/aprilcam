"""HOST-N camera addressing through ``ConnectionManager.call_camera`` and the
MCP tools (sprint 048, ticket 005). Fakes only; no daemon or socket."""

from __future__ import annotations

from typing import Optional

import pytest
from mcp import types

from aprilcam.client.interfaces import DaemonInfo, Discovery
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import ConflictingTarget, ConnectionFailed, UnknownHost
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server
from aprilcam.types import CameraState, TagFrame

DONALD = "192.168.4.60:5280"
BUZZ = "192.168.4.61:5280"


def _state(name: str, number: int) -> CameraState:
    return CameraState(name=name, present=True, usable=True, playfield=None,
                       calibrated=False, number=number)


class FakeDaemon(Daemon):
    def __init__(self, label: str, cameras=()) -> None:
        self.label = label
        self.cameras = tuple(cameras)
        self.rpcs: list = []
        self.fail = False

    def list_cameras(self):
        return self.cameras

    def get_tags(self, camera):
        self.rpcs.append(("get_tags", camera))
        if self.fail:
            raise ConnectionFailed(self.label)
        return TagFrame(tags=(), timestamp=0.0, frame_index=0)

    def get_camera_config(self, camera):
        self.rpcs.append(("get_camera_config", camera))
        raise ConnectionFailed(self.label)


class FakeDiscovery(Discovery):
    def __init__(self) -> None:
        self.local = FakeDaemon("local", [_state("brio-501", 1)])
        self.donald = FakeDaemon("donald", [_state("cam-a", 4)])
        self.buzz = FakeDaemon("buzzkill", [_state("cam-b", 2)])
        self.connects: list[Optional[str]] = []
        self.local_connects = 0
        self.env_host: Optional[str] = None  # what connect(None) would honor

    def find_all(self):
        return (
            DaemonInfo(host="127.0.0.1:5280", port=5280, is_local=True,
                       reachable=True, name="gala"),
            DaemonInfo(host=DONALD, port=5280, is_local=False,
                       reachable=True, name="donald"),
            DaemonInfo(host=BUZZ, port=5280, is_local=False,
                       reachable=True, name="buzzkill"),
        )

    def _table(self, host):
        return {None: self.local, DONALD: self.donald, "donald": self.donald,
                BUZZ: self.buzz, "buzzkill": self.buzz}

    def connect(self, host=None):
        self.connects.append(host)
        if host is None and self.env_host is not None:
            host = self.env_host
        table = self._table(host)
        if host in table:
            return table[host]
        raise ConnectionFailed(str(host))

    def connect_local(self):
        self.local_connects += 1
        return self.local


def _manager(disc, default_host=None) -> ConnectionManager:
    return ConnectionManager(default_host=default_host, discovery_factory=lambda: disc)


def _get_tags(mgr, camera, **kw):
    return mgr.call_camera(camera, lambda d, cam: d.get_tags(cam), **kw)


# -- ConnectionManager.call_camera -------------------------------------------


def test_host_n_routes_to_named_daemon_with_number():
    disc = FakeDiscovery()
    _get_tags(_manager(disc), "donald-4")
    assert disc.donald.rpcs == [("get_tags", "4")]
    assert disc.local.rpcs == [] and disc.buzz.rpcs == []


def test_slug_and_number_unchanged_on_default_daemon():
    disc = FakeDiscovery()
    mgr = _manager(disc, default_host="buzzkill")
    _get_tags(mgr, "cam-b")
    _get_tags(mgr, "2")
    assert disc.buzz.rpcs == [("get_tags", "cam-b"), ("get_tags", "2")]


def test_per_call_daemon_conflicting_with_host_n():
    disc = FakeDiscovery()
    with pytest.raises(ConflictingTarget):
        _get_tags(_manager(disc), "donald-4", host="buzzkill")
    assert disc.donald.rpcs == [] and disc.buzz.rpcs == []


def test_per_call_daemon_matching_host_n_is_fine():
    disc = FakeDiscovery()
    _get_tags(_manager(disc), "donald-4", host="donald")
    assert disc.donald.rpcs == [("get_tags", "4")]


def test_session_default_is_overridden_by_host_n_not_a_conflict():
    disc = FakeDiscovery()
    _get_tags(_manager(disc, default_host="buzzkill"), "donald-4")
    assert disc.donald.rpcs == [("get_tags", "4")] and disc.buzz.rpcs == []


def test_session_default_with_explicit_other_daemon_conflicts():
    disc = FakeDiscovery()
    with pytest.raises(ConflictingTarget):
        _get_tags(_manager(disc, default_host="buzzkill"), "donald-4", host="buzzkill")


def test_unknown_host_raises():
    with pytest.raises(UnknownHost):
        _get_tags(_manager(FakeDiscovery()), "nowhere-4")


def test_local_host_n_uses_local_daemon_despite_session_default():
    disc = FakeDiscovery()
    _get_tags(_manager(disc, default_host="buzzkill"), "gala-1")
    assert disc.local.rpcs == [("get_tags", "1")]
    assert disc.buzz.rpcs == [] and disc.local_connects == 1


def test_local_host_n_uses_local_daemon_despite_env_default():
    disc = FakeDiscovery()
    disc.env_host = "buzzkill"  # connect(None) would go to buzzkill
    _get_tags(_manager(disc), "gala-1")
    assert disc.local.rpcs == [("get_tags", "1")]
    assert disc.buzz.rpcs == []


def test_cache_keyed_by_resolved_host_and_reused():
    disc = FakeDiscovery()
    mgr = _manager(disc)
    _get_tags(mgr, "donald-4")
    before = disc.connects.count(DONALD)
    _get_tags(mgr, "donald-4")
    assert disc.connects.count(DONALD) == before  # resolver may probe; cache hit adds none


def test_eviction_on_connection_failed_only_for_that_target():
    disc = FakeDiscovery()
    mgr = _manager(disc)
    _get_tags(mgr, "donald-4")
    _get_tags(mgr, "buzzkill-2")
    disc.donald.fail = True
    with pytest.raises(ConnectionFailed):
        _get_tags(mgr, "donald-4")
    assert DONALD not in mgr._cache and BUZZ in mgr._cache
    disc.donald.fail = False
    _get_tags(mgr, "donald-4")
    assert mgr._cache[DONALD] is disc.donald


def test_local_eviction_on_connection_failed():
    disc = FakeDiscovery()
    mgr = _manager(disc)
    disc.local.fail = True
    with pytest.raises(ConnectionFailed):
        _get_tags(mgr, "gala-1")
    assert mgr._cache == {}


# -- tool level ---------------------------------------------------------------


async def _call(server, name, arguments=None):
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    return await handler(req)


def _server(disc, default_host=None):
    return build_server(
        default_daemon_host=default_host,
        connection_factory=lambda default_host: _manager(disc, default_host),
    )


async def test_get_tags_host_n_reaches_named_daemon_without_daemon_arg():
    disc = FakeDiscovery()
    result = await _call(_server(disc), "get_tags", {"camera": "donald-4"})
    assert not result.root.isError
    assert disc.donald.rpcs == [("get_tags", "4")]


async def test_get_tags_conflicting_daemon_is_tool_error():
    disc = FakeDiscovery()
    result = await _call(_server(disc), "get_tags",
                         {"camera": "donald-4", "daemon": "buzzkill"})
    assert result.root.isError
    assert "donald" in result.root.content[0].text
    assert disc.donald.rpcs == [] and disc.buzz.rpcs == []


async def test_configuration_tool_host_n_and_unknown_host_error():
    disc = FakeDiscovery()
    server = _server(disc)
    await _call(server, "get_camera_config", {"camera": "donald-4"})
    assert disc.donald.rpcs == [("get_camera_config", "4")]
    result = await _call(server, "get_camera_config", {"camera": "nowhere-4"})
    assert result.root.isError and "nowhere" in result.root.content[0].text


async def test_session_default_applies_to_slug_but_not_host_n():
    disc = FakeDiscovery()
    server = _server(disc, default_host="buzzkill")
    await _call(server, "get_tags", {"camera": "cam-b"})
    await _call(server, "get_tags", {"camera": "donald-4"})
    assert disc.buzz.rpcs == [("get_tags", "cam-b")]
    assert disc.donald.rpcs == [("get_tags", "4")]


class _Stop(Exception):
    pass


class _SpyManager(ConnectionManager):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.camera_calls: list = []
        self.plain_calls = 0

    def call(self, fn, *, host=None):
        self.plain_calls += 1
        raise _Stop()

    def call_camera(self, camera, fn, *, host=None):
        self.camera_calls.append(camera)
        raise _Stop()


_SAMPLE = {"string": "x", "integer": 1, "number": 1.0, "boolean": False}


async def test_every_tool_with_a_camera_parameter_uses_call_camera():
    spies: list[_SpyManager] = []

    def factory(default_host):
        spies.append(_SpyManager(default_host=default_host))
        return spies[0]

    server = build_server(connection_factory=factory)
    tools = await server.list_tools()
    camera_tools = [t for t in tools if "camera" in t.inputSchema.get("properties", {})]
    assert len(camera_tools) >= 16
    for tool in camera_tools:
        props = tool.inputSchema["properties"]
        args = {"camera": "donald-4"}
        for name in tool.inputSchema.get("required", []):
            if name != "camera":
                args[name] = "apriltag" if name == "family" else _SAMPLE[props[name].get("type", "string")]
        spy = spies[0]
        spy.camera_calls.clear()
        spy.plain_calls = 0
        await _call(server, tool.name, args)
        assert spy.camera_calls == ["donald-4"], tool.name
        assert spy.plain_calls == 0, tool.name
