"""Tests for ``aprilcam.mcp.tools.intrinsics`` (sprint 044 ticket 009).

Same headless pattern as ``test_configuration.py``: real MCP tool calls
through FastMCP's handler, a fake discovery handing back a fake daemon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from mcp import types

from aprilcam.errors import UnknownCamera
from aprilcam.intrinsics.interfaces import (
    BANNER_GOOD_ENOUGH,
    BoardLayout,
    IntrinsicsFinishResult,
    IntrinsicsSessionActive,
    IntrinsicsStatus,
    NoIntrinsicsSession,
)
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server

_CELLS = {k: 0 for k in ("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE")}


def _status(**kw: Any) -> IntrinsicsStatus:
    base: dict[str, Any] = dict(
        camera="cam0",
        board_spec="5x7:37:28:DICT_5X5_100",
        layout=BoardLayout.CURRENT,
        image_size=(1280, 720),
        board_visible=True,
        view_count=12,
        tilted_count=3,
        cell_coverage={**_CELLS, "C": 4},
        tilt_coverage={"N": 1, "E": 0, "S": 2, "W": 0},
        rms_px=np.float64(0.4),
        residual_px=None,
        correction_px=2.5,
        banner=BANNER_GOOD_ENOUGH,
        next_hint="tilt toward east",
        errors=("oops",),
        started_at=10.0,
    )
    base.update(kw)
    return IntrinsicsStatus(**base)


@dataclass
class _FakeDaemon:
    status: IntrinsicsStatus = field(default_factory=_status)
    finish: Optional[IntrinsicsFinishResult] = None
    raises: Optional[Exception] = None
    calls: list = field(default_factory=list)

    def _go(self, *call: Any) -> None:
        self.calls.append(call)
        if self.raises is not None:
            raise self.raises

    def start_intrinsics(self, camera, board=None):
        self._go("start", camera, board)
        return self.status

    def get_intrinsics_status(self, camera):
        self._go("status", camera)
        return self.status

    def finish_intrinsics(self, camera, install, force=False):
        self._go("finish", camera, install, force)
        assert self.finish is not None
        return self.finish

    def abort_intrinsics(self, camera):
        self._go("abort", camera)
        return self.status


@dataclass
class _FakeDiscovery:
    daemon: _FakeDaemon

    def connect(self, host: Optional[str] = None) -> _FakeDaemon:
        return self.daemon

    def find_all(self):  # pragma: no cover
        return ()


def _build(daemon: _FakeDaemon, tmp_path: Path):
    discovery = _FakeDiscovery(daemon)

    def factory(*, default_host: Optional[str] = None) -> ConnectionManager:
        return ConnectionManager(default_host=default_host, discovery_factory=lambda: discovery)

    return build_server(connection_factory=factory, temp_dir_factory=lambda: tmp_path)


async def _call(server, name: str, arguments=None):
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    return await handler(req)


def _data(result) -> Any:
    assert result.root.isError is False, result.root.content
    data = result.root.structuredContent
    return json.loads(json.dumps(data))  # must round-trip as JSON


async def test_tools_are_listed(tmp_path):
    server = _build(_FakeDaemon(), tmp_path)
    names = {t.name for t in await server.list_tools()}
    assert {
        "start_intrinsics",
        "get_intrinsics_status",
        "finish_intrinsics",
        "abort_intrinsics",
    } <= names


async def test_start_forwards_board_and_serialises(tmp_path):
    d = _FakeDaemon()
    out = _data(
        await _call(
            _build(d, tmp_path), "start_intrinsics", {"camera": "cam0", "board": "4x6:30:22:DICT_4X4_50"}
        )
    )
    assert d.calls == [("start", "cam0", "4x6:30:22:DICT_4X4_50")]
    assert out["banner"] == "good_enough"
    assert out["next_hint"] == "tilt toward east"
    assert out["image_size"] == [1280, 720]
    assert out["cell_coverage"]["C"] == 4
    assert out["tilt_coverage"] == {"N": 1, "E": 0, "S": 2, "W": 0}
    assert out["rms_px"] == 0.4
    assert out["residual_px"] is None
    assert out["layout"] == "current"
    assert out["errors"] == ["oops"]


async def test_start_board_defaults_and_none_sentinel(tmp_path):
    d = _FakeDaemon()
    server = _build(d, tmp_path)
    await _call(server, "start_intrinsics", {"camera": "cam0"})
    await _call(server, "start_intrinsics", {"camera": "cam0", "board": "NONE"})
    assert d.calls == [("start", "cam0", None), ("start", "cam0", None)]


async def test_status_and_abort_forward(tmp_path):
    d = _FakeDaemon()
    server = _build(d, tmp_path)
    assert _data(await _call(server, "get_intrinsics_status", {"camera": "cam0"}))["camera"] == "cam0"
    _data(await _call(server, "abort_intrinsics", {"camera": "cam0"}))
    assert d.calls == [("status", "cam0"), ("abort", "cam0")]


async def test_finish_forwards_flags_and_preserves_nones(tmp_path):
    d = _FakeDaemon(
        finish=IntrinsicsFinishResult(
            installed=False,
            reason="not good enough",
            status=_status(),
            lens={"camera_matrix": np.eye(3), "fx": np.float64(800.0), "dist": (0.1, None)},
        )
    )
    server = _build(d, tmp_path)
    out = _data(
        await _call(server, "finish_intrinsics", {"camera": "cam0", "install": True, "force": True})
    )
    assert d.calls == [("finish", "cam0", True, True)]
    assert out["installed"] is False
    assert out["reason"] == "not good enough"
    assert out["lens"]["camera_matrix"][0] == [1.0, 0.0, 0.0]
    assert out["lens"]["dist"] == [0.1, None]
    assert out["status"]["residual_px"] is None

    d.calls.clear()
    d.finish = IntrinsicsFinishResult(installed=True, reason=None, status=_status(), lens=None)
    out = _data(await _call(server, "finish_intrinsics", {"camera": "cam0"}))
    assert d.calls == [("finish", "cam0", False, False)]
    assert out["reason"] is None and out["lens"] is None


async def test_error_mapping(tmp_path):
    cases = [
        ("start_intrinsics", IntrinsicsSessionActive("cam0"), "already active"),
        ("get_intrinsics_status", NoIntrinsicsSession("cam0"), "no intrinsics session"),
        ("abort_intrinsics", UnknownCamera("cam9", ("cam0",)), "cam9"),
    ]
    for tool, exc, needle in cases:
        server = _build(_FakeDaemon(raises=exc), tmp_path)
        result = await _call(server, tool, {"camera": "cam9"})
        assert result.root.isError is True
        assert needle in result.root.content[0].text


def test_guide_mentions_the_tools():
    from aprilcam.client.guides import Guides

    text = Guides().get()
    for name in ("start_intrinsics", "get_intrinsics_status", "finish_intrinsics", "abort_intrinsics"):
        assert name in text
