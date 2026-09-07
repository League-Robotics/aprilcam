"""Tests for ``aprilcam.mcp.tools.perception`` (ticket 026-002).

Headless throughout, following ``test_server.py``'s pattern (ticket
001): every tool is driven as a *real* MCP tool call through
``FastMCP``'s own low-level ``CallToolRequest`` handler --
``build_server``'s ``connection_factory`` seam is handed a
``ConnectionManager`` wired to a fake ``client.interfaces.Discovery``
(``tests/aprilcam/mcp/test_connection.py``'s own fake-injection style),
which in turn hands back a fake ``Daemon`` shaped like
``tests/aprilcam/cli/test_camera_cli.py``'s ``_FakeDaemon`` -- no real
daemon, socket, or gRPC channel anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from mcp import types

from aprilcam.config.interfaces import Feature
from aprilcam.errors import NotCalibrated, UnknownCamera
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server
from aprilcam.types import (
    CameraState,
    ImageFrame,
    ProbeReport,
    ProbeSkip,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    WorldPoint,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDaemon:
    cameras: Tuple[CameraState, ...] = ()
    probe_report: Optional[ProbeReport] = None
    frame: Optional[ImageFrame] = None
    get_frame_raises: Optional[Exception] = None
    tag_frame: Optional[TagFrame] = None
    get_tags_raises: Optional[Exception] = None
    tag_record: Optional[TagRecord] = None
    get_tag_raises: Optional[Exception] = None
    tag_history: Tuple[TagFrame, ...] = ()
    get_tag_history_raises: Optional[Exception] = None
    features: Tuple[Feature, ...] = ()
    find_feature_raises: Optional[Exception] = None

    requested_camera: Optional[str] = None
    requested_deskewed: Optional[bool] = None
    requested_tag: Optional[TagId] = None
    requested_history_tag: Optional[TagId] = None
    requested_history_frames: Optional[int] = None
    requested_playfield: Optional[str] = None
    requested_query: Optional[str] = None

    def list_cameras(self) -> Tuple[CameraState, ...]:
        return self.cameras

    def probe_cameras(self) -> ProbeReport:
        return self.probe_report if self.probe_report is not None else ProbeReport(cameras=self.cameras)

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        self.requested_camera = camera
        self.requested_deskewed = deskewed
        if self.get_frame_raises is not None:
            raise self.get_frame_raises
        return self.frame if self.frame is not None else ImageFrame(
            data=b"\xff\xd8\xff\xd9", width=4, height=4, encoding=encoding, timestamp=0.0
        )

    def get_tags(self, camera: str) -> TagFrame:
        self.requested_camera = camera
        if self.get_tags_raises is not None:
            raise self.get_tags_raises
        return self.tag_frame if self.tag_frame is not None else TagFrame(tags=(), timestamp=0.0, frame_index=0)

    def get_tag(self, camera: str, tag: TagId) -> Optional[TagRecord]:
        self.requested_camera = camera
        self.requested_tag = tag
        if self.get_tag_raises is not None:
            raise self.get_tag_raises
        return self.tag_record

    def get_tag_history(
        self, camera: str, tag: Optional[TagId] = None, frames: Optional[int] = None
    ) -> Tuple[TagFrame, ...]:
        self.requested_camera = camera
        self.requested_history_tag = tag
        self.requested_history_frames = frames
        if self.get_tag_history_raises is not None:
            raise self.get_tag_history_raises
        return self.tag_history

    def find_feature(self, playfield: str, query: str) -> Tuple[Feature, ...]:
        self.requested_playfield = playfield
        self.requested_query = query
        if self.find_feature_raises is not None:
            raise self.find_feature_raises
        return self.features


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

    def find_all(self):  # pragma: no cover - perception tools never fan out
        return ()


def _camera(name: str = "arducam-ov9782-usb-camera", number: int = 1, **overrides) -> CameraState:
    fields = dict(name=name, present=True, usable=True, playfield=None, calibrated=False, number=number)
    fields.update(overrides)
    return CameraState(**fields)


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
# list_cameras -- AC: every CameraState, absent-but-registered never omitted
# ---------------------------------------------------------------------------


async def test_list_cameras_returns_every_camera_structured(tmp_path):
    cameras = (
        _camera("arducam-ov9782-usb-camera", 1, playfield="main-playfield", calibrated=True),
        _camera("hd-usb-camera", 2, present=False, usable=False),
    )
    server, _ = _build(_FakeDaemon(cameras=cameras), tmp_path)

    result = await _call(server, "list_cameras")

    assert result.root.isError is False
    names = {c["name"] for c in result.root.structuredContent["result"]}
    assert names == {"arducam-ov9782-usb-camera", "hd-usb-camera"}
    absent = next(c for c in result.root.structuredContent["result"] if c["name"] == "hd-usb-camera")
    assert absent["present"] is False  # absent camera listed and flagged, never omitted


# ---------------------------------------------------------------------------
# probe_cameras -- AC: full ProbeReport, skipped backends named
# ---------------------------------------------------------------------------


async def test_probe_cameras_returns_the_full_report_with_skipped_backends(tmp_path):
    report = ProbeReport(
        cameras=(_camera(),),
        skipped=(ProbeSkip(backend="hd-usb-camera", reason="in use by a live subscriber"),),
    )
    server, _ = _build(_FakeDaemon(probe_report=report), tmp_path)

    result = await _call(server, "probe_cameras")

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured["skipped"][0]["backend"] == "hd-usb-camera"
    assert structured["skipped"][0]["reason"] == "in use by a live subscriber"


# ---------------------------------------------------------------------------
# get_frame -- AC: base64/file per-request, NotCalibrated never substituted
# ---------------------------------------------------------------------------


async def test_get_frame_base64_returns_inline_image_content(tmp_path):
    daemon = _FakeDaemon(frame=ImageFrame(data=b"raw-jpeg-bytes", width=4, height=4, encoding="jpeg", timestamp=0.0))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_frame", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    (content,) = result.root.content
    assert content.type == "image"
    assert content.mimeType == "image/jpeg"
    import base64 as _b64

    assert _b64.b64decode(content.data) == b"raw-jpeg-bytes"
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    assert daemon.requested_deskewed is False


async def test_get_frame_file_writes_under_the_servers_temp_dir_and_returns_only_the_path(tmp_path):
    daemon = _FakeDaemon(frame=ImageFrame(data=b"raw-jpeg-bytes", width=4, height=4, encoding="jpeg", timestamp=0.0))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_frame", {"camera": "arducam-ov9782-usb-camera", "format": "file"})

    assert result.root.isError is False
    (content,) = result.root.content
    assert content.type == "text"
    written_path = Path(content.text)
    assert written_path.parent == tmp_path
    assert written_path.read_bytes() == b"raw-jpeg-bytes"


async def test_get_frame_deskewed_on_uncalibrated_camera_surfaces_not_calibrated(tmp_path):
    daemon = _FakeDaemon(get_frame_raises=NotCalibrated("camera not calibrated"))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_frame", {"camera": "arducam-ov9782-usb-camera", "deskewed": True})

    assert result.root.isError is True
    assert "camera not calibrated" in result.root.content[0].text
    assert daemon.requested_deskewed is True  # the request really did ask for deskewed


# ---------------------------------------------------------------------------
# get_tags -- AC: world-first TagFrame, UnknownCamera surfaces cleanly
# ---------------------------------------------------------------------------


async def test_get_tags_returns_the_tag_frame(tmp_path):
    record = TagRecord(
        tag=TagId(TagFamily.APRILTAG, 7),
        world=WorldPoint(10.0, -5.0),
        heading_rad=0.5,
        world_velocity=None,
        speed=1.0,
        timestamp=1.0,
    )
    daemon = _FakeDaemon(tag_frame=TagFrame(tags=(record,), timestamp=1.0, frame_index=3))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_tags", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured["frame_index"] == 3
    (tag,) = structured["tags"]
    assert tag["tag"] == {"family": "apriltag", "number": 7}
    assert tag["world"] == {"x": 10.0, "y": -5.0}
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"


async def test_get_tags_carries_image_yaw_deg_through_the_mcp_round_trip(tmp_path):
    """Ticket 039-002 acceptance criterion: ``image_yaw_deg`` must
    survive the real MCP round trip (FastMCP's ``CallToolRequest``
    handler and its pydantic-based structured-output serialization),
    not merely exist on the source dataclass. This is a stub/fake
    ``Daemon`` -- it does not exercise the separate gRPC proto boundary
    ``daemon/rpc.py``/``client/models.py`` own (covered by
    ``tests/aprilcam/client/test_daemon_client.py``'s real round trip)."""
    record = TagRecord(
        tag=TagId(TagFamily.APRILTAG, 7),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        calibrated=False,
        image_yaw_deg=42.5,
    )
    daemon = _FakeDaemon(tag_frame=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_tags", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    (tag,) = result.root.structuredContent["tags"]
    assert tag["image_yaw_deg"] == 42.5


async def test_get_tags_unknown_camera_surfaces_the_typed_error_with_available_list(tmp_path):
    daemon = _FakeDaemon(get_tags_raises=UnknownCamera("no-such-cam", ("arducam-ov9782-usb-camera",)))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_tags", {"camera": "no-such-cam"})

    assert result.root.isError is True
    text = result.root.content[0].text
    assert "no-such-cam" in text
    assert "arducam-ov9782-usb-camera" in text


# ---------------------------------------------------------------------------
# get_tag -- AC: family+number addressing, None (not a fabricated (0,0))
# ---------------------------------------------------------------------------


async def test_get_tag_returns_the_matching_record(tmp_path):
    record = TagRecord(
        tag=TagId(TagFamily.ARUCO, 5),
        world=WorldPoint(1.0, 2.0),
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
    )
    daemon = _FakeDaemon(tag_record=record)
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "get_tag", {"camera": "arducam-ov9782-usb-camera", "family": "aruco", "number": 5}
    )

    assert result.root.isError is False
    assert result.root.structuredContent["result"]["tag"] == {"family": "aruco", "number": 5}
    assert daemon.requested_tag == TagId(TagFamily.ARUCO, 5)


async def test_get_tag_carries_image_yaw_deg_through_the_mcp_round_trip(tmp_path):
    """Ticket 039-002 acceptance criterion, ``get_tag`` variant."""
    record = TagRecord(
        tag=TagId(TagFamily.ARUCO, 5),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        calibrated=False,
        image_yaw_deg=-17.25,
    )
    daemon = _FakeDaemon(tag_record=record)
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "get_tag", {"camera": "arducam-ov9782-usb-camera", "family": "aruco", "number": 5}
    )

    assert result.root.isError is False
    assert result.root.structuredContent["result"]["image_yaw_deg"] == -17.25


async def test_get_tag_not_detected_returns_null_never_a_fabricated_origin(tmp_path):
    daemon = _FakeDaemon(tag_record=None)
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "get_tag", {"camera": "arducam-ov9782-usb-camera", "family": "apriltag", "number": 99}
    )

    assert result.root.isError is False
    assert result.root.structuredContent["result"] is None
    assert result.root.content == []


# ---------------------------------------------------------------------------
# get_tag_history -- AC: filter by family+number together, UC-26 ring buffer
# ---------------------------------------------------------------------------


async def test_get_tag_history_returns_every_frame(tmp_path):
    frames = (
        TagFrame(tags=(), timestamp=0.0, frame_index=0),
        TagFrame(tags=(), timestamp=0.1, frame_index=1),
    )
    daemon = _FakeDaemon(tag_history=frames)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_tag_history", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert len(result.root.structuredContent["result"]) == 2
    assert daemon.requested_history_tag is None


async def test_get_tag_history_carries_image_yaw_deg_through_the_mcp_round_trip(tmp_path):
    """Ticket 039-002 acceptance criterion, ``get_tag_history`` variant --
    the field must survive per historical frame, not just on the
    current-state ``get_tags``/``get_tag`` tools."""
    record = TagRecord(
        tag=TagId(TagFamily.APRILTAG, 7),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        calibrated=False,
        image_yaw_deg=88.0,
    )
    frames = (TagFrame(tags=(record,), timestamp=0.0, frame_index=0),)
    daemon = _FakeDaemon(tag_history=frames)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_tag_history", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    (frame,) = result.root.structuredContent["result"]
    (tag,) = frame["tags"]
    assert tag["image_yaw_deg"] == 88.0


async def test_get_tag_history_filters_by_family_and_number_together(tmp_path):
    daemon = _FakeDaemon(tag_history=())
    server, _ = _build(daemon, tmp_path)

    await _call(
        server,
        "get_tag_history",
        {"camera": "arducam-ov9782-usb-camera", "family": "apriltag", "number": 7, "frames": 30},
    )

    assert daemon.requested_history_tag == TagId(TagFamily.APRILTAG, 7)
    assert daemon.requested_history_frames == 30


async def test_get_tag_history_rejects_family_without_number(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server, "get_tag_history", {"camera": "arducam-ov9782-usb-camera", "family": "apriltag"}
    )

    assert result.root.isError is True


# ---------------------------------------------------------------------------
# where -- AC: wraps find_feature; ambiguous returns every match; no match
# returns an empty list, not an error
# ---------------------------------------------------------------------------


async def test_where_returns_the_single_match(tmp_path):
    feature = Feature(slug="dot-west-blue", kind="dot", position=WorldPoint(-50.0, 0.0), colour="blue")
    daemon = _FakeDaemon(features=(feature,))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "where", {"playfield": "main-playfield", "query": "the blue dot"})

    assert result.root.isError is False
    (match,) = result.root.structuredContent["result"]
    assert match["slug"] == "dot-west-blue"
    assert daemon.requested_playfield == "main-playfield"
    assert daemon.requested_query == "the blue dot"


async def test_where_ambiguous_query_returns_every_match(tmp_path):
    features = tuple(
        Feature(slug=f"dot-{corner}-orange", kind="dot", position=WorldPoint(0.0, 0.0), colour="orange")
        for corner in ("northwest", "northeast", "southeast", "southwest")
    )
    daemon = _FakeDaemon(features=features)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "where", {"playfield": "main-playfield", "query": "the orange dot"})

    assert result.root.isError is False
    slugs = {m["slug"] for m in result.root.structuredContent["result"]}
    assert slugs == {f"dot-{c}-orange" for c in ("northwest", "northeast", "southeast", "southwest")}


async def test_where_no_match_returns_an_empty_list_not_an_error(tmp_path):
    daemon = _FakeDaemon(features=())
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "where", {"playfield": "main-playfield", "query": "the orange triangle"})

    assert result.root.isError is False
    assert result.root.structuredContent["result"] == []


# ---------------------------------------------------------------------------
# daemon parameter -- AC: late-binding, per-call override of the session default
# ---------------------------------------------------------------------------


async def test_daemon_parameter_targets_a_specific_host_for_one_call(tmp_path):
    daemon = _FakeDaemon(cameras=(_camera(),))
    server, discovery = _build(daemon, tmp_path)

    result = await _call(server, "list_cameras", {"daemon": "pi-field.local"})

    assert result.root.isError is False
    assert discovery.connect_calls == ["pi-field.local"]
