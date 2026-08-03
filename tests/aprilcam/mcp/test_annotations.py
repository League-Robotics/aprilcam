"""Tests for ``aprilcam.mcp.tools.annotations`` (ticket 026-005).

Headless throughout, following ``test_mobile_tags.py``'s pattern
(ticket 004): every tool is driven as a *real* MCP tool call through
``FastMCP``'s own low-level ``CallToolRequest`` handler --
``build_server``'s ``connection_factory`` seam is handed a
``ConnectionManager`` wired to a fake ``client.interfaces.Discovery``,
which in turn hands back a fake ``Daemon`` -- no real daemon, socket, or
gRPC channel anywhere in this file.

``_FakeDaemon`` here is a genuine (if tiny) in-memory annotation store --
not just a call-counter -- so the round-trip and no-op tests (UC-28's
"clearing a nonexistent layer is a no-op", "how a robot reads another
robot's plan") exercise real upsert/remove/clear semantics, the same way
``daemon/service.py``'s own store behaves, rather than merely asserting
a mock was called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mcp import types

from aprilcam.errors import UnknownPlayfield
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    Circle,
    Marker,
    PathShape,
    Polygon,
    Style,
    Text,
    WorldPoint,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDaemon:
    known_playfields: Tuple[str, ...] = ("main-playfield",)
    put_annotation_raises: Optional[Exception] = None
    remove_annotation_raises: Optional[Exception] = None
    clear_annotations_raises: Optional[Exception] = None
    get_annotations_raises: Optional[Exception] = None

    put_calls: List[Tuple[str, Annotation]] = field(default_factory=list)
    remove_calls: List[Tuple[str, str, str]] = field(default_factory=list)
    clear_calls: List[Tuple[str, Optional[str]]] = field(default_factory=list)

    # playfield -> layer -> id -> Annotation
    _store: Dict[str, Dict[str, Dict[str, Annotation]]] = field(default_factory=dict)
    _revision: int = 0

    def put_annotation(self, playfield: str, annotation: Annotation) -> None:
        self.put_calls.append((playfield, annotation))
        if self.put_annotation_raises is not None:
            raise self.put_annotation_raises
        self._revision += 1
        self._store.setdefault(playfield, {}).setdefault(annotation.layer, {})[
            annotation.id
        ] = annotation

    def remove_annotation(self, playfield: str, layer: str, id: str) -> None:
        self.remove_calls.append((playfield, layer, id))
        if self.remove_annotation_raises is not None:
            raise self.remove_annotation_raises
        self._revision += 1
        self._store.get(playfield, {}).get(layer, {}).pop(id, None)

    def clear_annotations(self, playfield: str, layer: Optional[str] = None) -> None:
        self.clear_calls.append((playfield, layer))
        if self.clear_annotations_raises is not None:
            raise self.clear_annotations_raises
        self._revision += 1
        if layer is None:
            self._store.pop(playfield, None)
        else:
            self._store.get(playfield, {}).pop(layer, None)

    def get_annotations(self, playfield: str, layer: Optional[str] = None) -> AnnotationSet:
        if self.get_annotations_raises is not None:
            raise self.get_annotations_raises
        layers = self._store.get(playfield, {})
        if layer is not None:
            entries: Tuple[Annotation, ...] = tuple(layers.get(layer, {}).values())
        else:
            entries = tuple(a for layer_map in layers.values() for a in layer_map.values())
        return AnnotationSet(annotations=entries, revision=self._revision, timestamp=123.0)


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

    def find_all(self):  # pragma: no cover - annotation tools never fan out
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
# draw_marker -- AC: builds Marker + Style, wraps in Annotation, calls put_annotation
# ---------------------------------------------------------------------------


async def test_draw_marker_builds_marker_and_calls_put_annotation(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_marker",
        {
            "playfield": "main-playfield",
            "layer": "robot-a",
            "id": "goal",
            "x": 10.0,
            "y": -5.0,
            "label": "goal post",
            "color": "#ff0000",
            "line_width": 2.0,
            "ttl_seconds": 30.0,
        },
    )

    assert result.root.isError is False
    assert len(daemon.put_calls) == 1
    playfield, annotation = daemon.put_calls[0]
    assert playfield == "main-playfield"
    assert annotation == Annotation(
        id="goal",
        layer="robot-a",
        shape=Marker(at=WorldPoint(10.0, -5.0), label="goal post"),
        style=Style(color="#ff0000", line_width=2.0),
        ttl_seconds=30.0,
    )


async def test_draw_marker_defaults_label_style_and_ttl_to_none(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_marker",
        {"playfield": "main-playfield", "layer": "l", "id": "m1", "x": 1.0, "y": 2.0},
    )

    assert result.root.isError is False
    _, annotation = daemon.put_calls[0]
    assert annotation.shape == Marker(at=WorldPoint(1.0, 2.0), label=None)
    assert annotation.style == Style()
    assert annotation.ttl_seconds is None


# ---------------------------------------------------------------------------
# draw_path -- AC: PathShape from a list of [x, y] pairs
# ---------------------------------------------------------------------------


async def test_draw_path_builds_path_shape_from_point_pairs(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_path",
        {
            "playfield": "main-playfield",
            "layer": "robot-a",
            "id": "route",
            "points": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]],
        },
    )

    assert result.root.isError is False
    _, annotation = daemon.put_calls[0]
    assert annotation.shape == PathShape(
        points=(WorldPoint(0.0, 0.0), WorldPoint(10.0, 0.0), WorldPoint(10.0, 10.0))
    )


# ---------------------------------------------------------------------------
# draw_polygon -- AC: Polygon from a list of [x, y] pairs
# ---------------------------------------------------------------------------


async def test_draw_polygon_builds_polygon_from_point_pairs(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_polygon",
        {
            "playfield": "main-playfield",
            "layer": "zones",
            "id": "keep-out",
            "points": [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0]],
        },
    )

    assert result.root.isError is False
    _, annotation = daemon.put_calls[0]
    assert annotation.shape == Polygon(
        points=(
            WorldPoint(0.0, 0.0),
            WorldPoint(5.0, 0.0),
            WorldPoint(5.0, 5.0),
            WorldPoint(0.0, 5.0),
        )
    )


# ---------------------------------------------------------------------------
# draw_circle -- AC: Circle from center_x/center_y/radius_cm
# ---------------------------------------------------------------------------


async def test_draw_circle_builds_circle(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_circle",
        {
            "playfield": "main-playfield",
            "layer": "zones",
            "id": "radius-marker",
            "center_x": 3.0,
            "center_y": 4.0,
            "radius_cm": 12.5,
        },
    )

    assert result.root.isError is False
    _, annotation = daemon.put_calls[0]
    assert annotation.shape == Circle(center=WorldPoint(3.0, 4.0), radius_cm=12.5)


# ---------------------------------------------------------------------------
# draw_text -- AC: Text from x/y/text
# ---------------------------------------------------------------------------


async def test_draw_text_builds_text(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_text",
        {
            "playfield": "main-playfield",
            "layer": "labels",
            "id": "note-1",
            "x": 1.0,
            "y": 1.0,
            "text": "start here",
        },
    )

    assert result.root.isError is False
    _, annotation = daemon.put_calls[0]
    assert annotation.shape == Text(at=WorldPoint(1.0, 1.0), text="start here")


# ---------------------------------------------------------------------------
# remove_annotation -- AC: calls Daemon.remove_annotation; absent key no-op
# ---------------------------------------------------------------------------


async def test_remove_annotation_calls_daemon_remove_annotation(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)
    await _call(
        server,
        "draw_marker",
        {"playfield": "main-playfield", "layer": "l", "id": "m1", "x": 0.0, "y": 0.0},
    )

    result = await _call(
        server, "remove_annotation", {"playfield": "main-playfield", "layer": "l", "id": "m1"}
    )

    assert result.root.isError is False
    assert daemon.remove_calls == [("main-playfield", "l", "m1")]
    listed = await _call(server, "list_annotations", {"playfield": "main-playfield"})
    assert listed.root.structuredContent["annotations"] == []


async def test_remove_annotation_absent_key_is_a_no_op_not_an_error(tmp_path):
    daemon = _FakeDaemon()  # nothing drawn
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "remove_annotation", {"playfield": "main-playfield", "layer": "l", "id": "ghost"}
    )

    assert result.root.isError is False
    assert daemon.remove_calls == [("main-playfield", "l", "ghost")]


# ---------------------------------------------------------------------------
# clear_layer -- AC: calls Daemon.clear_annotations; empty/nonexistent no-op
# ---------------------------------------------------------------------------


async def test_clear_layer_calls_daemon_clear_annotations_and_removes_contents(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)
    await _call(
        server,
        "draw_marker",
        {"playfield": "main-playfield", "layer": "l", "id": "m1", "x": 0.0, "y": 0.0},
    )

    result = await _call(server, "clear_layer", {"playfield": "main-playfield", "layer": "l"})

    assert result.root.isError is False
    assert daemon.clear_calls == [("main-playfield", "l")]
    listed = await _call(server, "list_annotations", {"playfield": "main-playfield"})
    assert listed.root.structuredContent["annotations"] == []


async def test_clear_layer_on_nonexistent_layer_is_a_clean_no_op(tmp_path):
    daemon = _FakeDaemon()  # playfield known, nothing ever drawn on it
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "clear_layer", {"playfield": "main-playfield", "layer": "never-drawn"}
    )

    assert result.root.isError is False
    assert daemon.clear_calls == [("main-playfield", "never-drawn")]


async def test_clear_layer_omitted_clears_every_layer_on_the_playfield(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)
    await _call(
        server,
        "draw_marker",
        {"playfield": "main-playfield", "layer": "a", "id": "m1", "x": 0.0, "y": 0.0},
    )
    await _call(
        server,
        "draw_marker",
        {"playfield": "main-playfield", "layer": "b", "id": "m2", "x": 1.0, "y": 1.0},
    )

    result = await _call(server, "clear_layer", {"playfield": "main-playfield"})

    assert result.root.isError is False
    assert daemon.clear_calls == [("main-playfield", None)]
    listed = await _call(server, "list_annotations", {"playfield": "main-playfield"})
    assert listed.root.structuredContent["annotations"] == []


# ---------------------------------------------------------------------------
# list_annotations -- AC: current AnnotationSet via get_annotations, round trip
# ---------------------------------------------------------------------------


async def test_list_annotations_round_trips_exactly_what_was_drawn(tmp_path):
    """UC-28.S-3: "how a robot reads another robot's plan" -- draw_marker
    then list_annotations finds exactly what was drawn."""
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    await _call(
        server,
        "draw_marker",
        {
            "playfield": "main-playfield",
            "layer": "robot-a",
            "id": "goal",
            "x": 10.0,
            "y": -5.0,
            "label": "goal post",
        },
    )

    result = await _call(server, "list_annotations", {"playfield": "main-playfield"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured["revision"] == 1
    (entry,) = structured["annotations"]
    assert entry["id"] == "goal"
    assert entry["layer"] == "robot-a"
    assert entry["shape"] == {"at": {"x": 10.0, "y": -5.0}, "label": "goal post"}


async def test_list_annotations_filters_by_layer_when_given(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)
    await _call(
        server,
        "draw_marker",
        {"playfield": "main-playfield", "layer": "a", "id": "m1", "x": 0.0, "y": 0.0},
    )
    await _call(
        server,
        "draw_marker",
        {"playfield": "main-playfield", "layer": "b", "id": "m2", "x": 1.0, "y": 1.0},
    )

    result = await _call(server, "list_annotations", {"playfield": "main-playfield", "layer": "a"})

    assert result.root.isError is False
    (entry,) = result.root.structuredContent["annotations"]
    assert entry["id"] == "m1"
    assert entry["layer"] == "a"


async def test_list_annotations_empty_playfield_returns_empty_set(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "list_annotations", {"playfield": "main-playfield"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured["annotations"] == []
    assert structured["revision"] == 0


# ---------------------------------------------------------------------------
# unknown playfield -- AC: rejected on any write, naming what the daemon holds
# ---------------------------------------------------------------------------


async def test_draw_marker_unknown_playfield_is_rejected_naming_what_exists(tmp_path):
    daemon = _FakeDaemon(
        put_annotation_raises=UnknownPlayfield("no-such-field", ("main-playfield",))
    )
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_marker",
        {"playfield": "no-such-field", "layer": "l", "id": "m1", "x": 0.0, "y": 0.0},
    )

    assert result.root.isError is True
    text = result.root.content[0].text
    assert "no-such-field" in text
    assert "main-playfield" in text


async def test_remove_annotation_unknown_playfield_is_rejected(tmp_path):
    daemon = _FakeDaemon(
        remove_annotation_raises=UnknownPlayfield("no-such-field", ("main-playfield",))
    )
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "remove_annotation", {"playfield": "no-such-field", "layer": "l", "id": "m1"}
    )

    assert result.root.isError is True
    text = result.root.content[0].text
    assert "no-such-field" in text
    assert "main-playfield" in text


async def test_clear_layer_unknown_playfield_is_rejected(tmp_path):
    daemon = _FakeDaemon(
        clear_annotations_raises=UnknownPlayfield("no-such-field", ("main-playfield",))
    )
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "clear_layer", {"playfield": "no-such-field"})

    assert result.root.isError is True
    text = result.root.content[0].text
    assert "no-such-field" in text
    assert "main-playfield" in text


# ---------------------------------------------------------------------------
# daemon parameter -- AC: late-binding, per-call override of the session default
# ---------------------------------------------------------------------------


async def test_daemon_parameter_targets_a_specific_host_for_one_call(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)

    result = await _call(
        server,
        "draw_marker",
        {
            "playfield": "main-playfield",
            "layer": "l",
            "id": "m1",
            "x": 0.0,
            "y": 0.0,
            "daemon": "pi-field.local",
        },
    )

    assert result.root.isError is False
    assert discovery.connect_calls == ["pi-field.local"]
