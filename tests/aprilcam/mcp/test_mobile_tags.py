"""Tests for ``aprilcam.mcp.tools.mobile_tags`` (ticket 026-004).

Headless throughout, following ``test_perception.py``'s pattern (ticket
002): every tool is driven as a *real* MCP tool call through
``FastMCP``'s own low-level ``CallToolRequest`` handler --
``build_server``'s ``connection_factory`` seam is handed a
``ConnectionManager`` wired to a fake ``client.interfaces.Discovery``,
which in turn hands back a fake ``Daemon`` -- no real daemon, socket, or
gRPC channel anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from mcp import types

from aprilcam.errors import NotCalibrated
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server
from aprilcam.types import MountParameters, TagFamily, TagId

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDaemon:
    tag_parameters: Dict[TagId, MountParameters] = field(default_factory=dict)
    register_tag_raises: Optional[Exception] = None
    unregister_tag_raises: Optional[Exception] = None
    list_tag_parameters_raises: Optional[Exception] = None

    registered_tag: Optional[TagId] = None
    registered_params: Optional[MountParameters] = None
    unregistered_tag: Optional[TagId] = None
    unregister_calls: int = 0

    def register_tag(self, tag: TagId, params: MountParameters) -> None:
        self.registered_tag = tag
        self.registered_params = params
        if self.register_tag_raises is not None:
            raise self.register_tag_raises
        self.tag_parameters[tag] = params

    def unregister_tag(self, tag: TagId) -> None:
        self.unregistered_tag = tag
        self.unregister_calls += 1
        if self.unregister_tag_raises is not None:
            raise self.unregister_tag_raises
        self.tag_parameters.pop(tag, None)

    def list_tag_parameters(self) -> Dict[TagId, MountParameters]:
        if self.list_tag_parameters_raises is not None:
            raise self.list_tag_parameters_raises
        return dict(self.tag_parameters)


@dataclass
class _FakeDiscovery:
    daemon: Optional[_FakeDaemon] = None
    connect_raises: Optional[Exception] = None
    connect_calls: list = field(default_factory=list)

    def connect(self, host: Optional[str] = None) -> _FakeDaemon:
        self.connect_calls.append(host)
        if self.connect_raises is not None:
            raise self.connect_raises
        assert self.daemon is not None, "test forgot to set _FakeDiscovery.daemon"
        return self.daemon

    def find_all(self):  # pragma: no cover - mobile-tag tools never fan out
        return ()


def _build(daemon: _FakeDaemon, tmp_path: Path):
    discovery = _FakeDiscovery(daemon=daemon)

    def connection_factory(*, default_host: Optional[str] = None) -> ConnectionManager:
        return ConnectionManager(default_host=default_host, discovery_factory=lambda: discovery)

    server = build_server(connection_factory=connection_factory, temp_dir_factory=lambda: tmp_path)
    return server, discovery


async def _call(server, name: str, arguments=None):
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    return await handler(req)


# ---------------------------------------------------------------------------
# register_tag -- AC: builds TagId + MountParameters, calls Daemon.register_tag
# ---------------------------------------------------------------------------


async def test_register_tag_builds_tag_id_and_mount_parameters(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "register_tag",
        {
            "family": "apriltag",
            "number": 7,
            "mount_x": 8.0,
            "mount_y": 3.0,
            "mount_z": 0.0,
            "mount_yaw_rad": 1.5707963267948966,
            "size_cm": 5.0,
        },
    )

    assert result.root.isError is False
    assert daemon.registered_tag == TagId(TagFamily.APRILTAG, 7)
    assert daemon.registered_params == MountParameters(
        size_cm=5.0, mount_x=8.0, mount_y=3.0, mount_z=0.0, mount_yaw_rad=1.5707963267948966
    )


async def test_register_tag_defaults_to_zero_offset_and_default_size(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "register_tag", {"family": "aruco", "number": 5})

    assert result.root.isError is False
    assert daemon.registered_tag == TagId(TagFamily.ARUCO, 5)
    assert daemon.registered_params == MountParameters()


async def test_register_tag_nonzero_mount_z_uncalibrated_surfaces_not_calibrated(tmp_path):
    daemon = _FakeDaemon(
        register_tag_raises=NotCalibrated(
            "cannot register mount_z for TagId(family=<TagFamily.APRILTAG: 'apriltag'>, number=7): "
            "this daemon holds no calibration for any camera"
        )
    )
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "register_tag", {"family": "apriltag", "number": 7, "mount_z": 20.0}
    )

    assert result.root.isError is True
    assert "calibration" in result.root.content[0].text


# ---------------------------------------------------------------------------
# unregister_tag -- AC: calls Daemon.unregister_tag; absent tag is a no-op
# ---------------------------------------------------------------------------


async def test_unregister_tag_calls_daemon_unregister(tmp_path):
    tag = TagId(TagFamily.APRILTAG, 7)
    daemon = _FakeDaemon(tag_parameters={tag: MountParameters(mount_x=1.0)})
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "unregister_tag", {"family": "apriltag", "number": 7})

    assert result.root.isError is False
    assert daemon.unregistered_tag == tag
    assert daemon.tag_parameters == {}


async def test_unregister_absent_tag_is_a_no_op_not_an_error(tmp_path):
    daemon = _FakeDaemon()  # nothing registered
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "unregister_tag", {"family": "aruco", "number": 99})

    assert result.root.isError is False
    assert daemon.unregister_calls == 1


# ---------------------------------------------------------------------------
# list_tag_parameters -- AC: every registered tag's mount parameters, keyed by tag
# ---------------------------------------------------------------------------


async def test_list_tag_parameters_round_trips_registered_entries(tmp_path):
    tag1 = TagId(TagFamily.APRILTAG, 7)
    tag2 = TagId(TagFamily.ARUCO, 5)
    params1 = MountParameters(mount_x=8.0, mount_y=3.0, mount_z=15.0)
    params2 = MountParameters(size_cm=6.0)
    daemon = _FakeDaemon(tag_parameters={tag1: params1, tag2: params2})
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "list_tag_parameters")

    assert result.root.isError is False
    entries = result.root.structuredContent["result"]
    assert len(entries) == 2
    by_tag = {(e["tag"]["family"], e["tag"]["number"]): e["params"] for e in entries}
    assert by_tag[("apriltag", 7)] == {
        "size_cm": None, "mount_x": 8.0, "mount_y": 3.0, "mount_z": 15.0, "mount_yaw_rad": 0.0
    }
    assert by_tag[("aruco", 5)] == {
        "size_cm": 6.0, "mount_x": 0.0, "mount_y": 0.0, "mount_z": 0.0, "mount_yaw_rad": 0.0
    }


async def test_list_tag_parameters_empty_registry_returns_empty_tuple(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "list_tag_parameters")

    assert result.root.isError is False
    assert result.root.structuredContent["result"] == []


# ---------------------------------------------------------------------------
# daemon parameter -- AC: late-binding, per-call override of the session default
# ---------------------------------------------------------------------------


async def test_daemon_parameter_targets_a_specific_host_for_one_call(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)

    result = await _call(
        server, "register_tag", {"family": "apriltag", "number": 1, "daemon": "pi-field.local"}
    )

    assert result.root.isError is False
    assert discovery.connect_calls == ["pi-field.local"]
