"""Tests for ``aprilcam.mcp.tools.configuration`` (ticket 026-003).

Headless throughout, following ``test_perception.py``'s pattern (ticket
002): every tool is driven as a *real* MCP tool call through
``FastMCP``'s own low-level ``CallToolRequest`` handler --
``build_server``'s ``connection_factory`` seam is handed a
``ConnectionManager`` wired to a fake ``client.interfaces.Discovery``,
which in turn hands back a fake ``Daemon`` -- no real daemon, socket, or
gRPC channel anywhere in this file.

Fakes construct :class:`~aprilcam.calibration.interfaces.Calibration`
directly (the abstract base, not ``calibration.calibration``'s concrete
cv2-backed subclass) -- exactly what ``mcp/tools/configuration.py``
itself imports and what ``daemon.interfaces.Daemon.get_calibration``
declares; see that module's docstring for why the concrete subclass is
never imported anywhere under ``mcp/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from mcp import types

from aprilcam.calibration.interfaces import Calibration, CalibrationResult, FlatFieldInfo, FlatFieldResult
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import ConfigLoader
from aprilcam.errors import MalformedDocument, NoFieldMask, NoPlayfieldLinked, UnknownCamera
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server
from aprilcam.playfield.playfield import Playfield

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDaemon:
    camera_config: Optional[CameraConfig] = None
    get_camera_config_raises: Optional[Exception] = None
    set_camera_config_raises: Optional[Exception] = None
    reset_camera_config_raises: Optional[Exception] = None
    playfield: Optional[Playfield] = None
    get_playfield_raises: Optional[Exception] = None
    set_playfield_raises: Optional[Exception] = None
    playfields: Tuple[Playfield, ...] = ()
    calibration_result: Optional[CalibrationResult] = None
    calibrate_raises: Optional[Exception] = None
    calibration: Optional[Calibration] = None
    get_calibration_raises: Optional[Exception] = None
    flatfield_result: Optional[FlatFieldResult] = None
    capture_flat_field_raises: Optional[Exception] = None
    flatfield_info: Optional[FlatFieldInfo] = None
    get_flatfield_raises: Optional[Exception] = None
    clear_flat_field_raises: Optional[Exception] = None

    requested_camera: Optional[str] = None
    pushed_camera_config: Optional[CameraConfig] = None
    requested_playfield_name: Optional[str] = None
    pushed_playfield: Optional[Any] = None
    requested_estimate_intrinsics: Optional[bool] = None
    requested_flatfield_kwargs: Optional[Dict[str, Any]] = None
    cleared_flatfield_camera: Optional[str] = None

    def get_camera_config(self, camera: str) -> CameraConfig:
        self.requested_camera = camera
        if self.get_camera_config_raises is not None:
            raise self.get_camera_config_raises
        assert self.camera_config is not None, "test forgot to set _FakeDaemon.camera_config"
        return self.camera_config

    def set_camera_config(self, config: CameraConfig) -> None:
        self.pushed_camera_config = config
        if self.set_camera_config_raises is not None:
            raise self.set_camera_config_raises

    def reset_camera_config(self, camera: str) -> None:
        self.requested_camera = camera
        if self.reset_camera_config_raises is not None:
            raise self.reset_camera_config_raises

    def get_playfield(self, name: str) -> Playfield:
        self.requested_playfield_name = name
        if self.get_playfield_raises is not None:
            raise self.get_playfield_raises
        assert self.playfield is not None, "test forgot to set _FakeDaemon.playfield"
        return self.playfield

    def set_playfield(self, definition: Any) -> None:
        self.pushed_playfield = definition
        if self.set_playfield_raises is not None:
            raise self.set_playfield_raises

    def list_playfields(self) -> Tuple[Playfield, ...]:
        return self.playfields

    def calibrate(self, camera: str, *, estimate_intrinsics: bool = False) -> CalibrationResult:
        self.requested_camera = camera
        self.requested_estimate_intrinsics = estimate_intrinsics
        if self.calibrate_raises is not None:
            raise self.calibrate_raises
        assert self.calibration_result is not None, "test forgot to set _FakeDaemon.calibration_result"
        return self.calibration_result

    def get_calibration(self, camera: str) -> Optional[Calibration]:
        self.requested_camera = camera
        if self.get_calibration_raises is not None:
            raise self.get_calibration_raises
        return self.calibration

    def get_frame(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - guard only
        raise AssertionError(
            "calibrate's NoPlayfieldLinked precondition must fail before any frame "
            "is requested (UC-15.S-2) -- get_frame must never be called for that case"
        )

    def capture_flat_field(
        self,
        camera: str,
        *,
        exposure: Optional[float] = None,
        frames: Optional[int] = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> FlatFieldResult:
        self.requested_camera = camera
        self.requested_flatfield_kwargs = {
            "exposure": exposure,
            "frames": frames,
            "force": force,
            "dry_run": dry_run,
        }
        if self.capture_flat_field_raises is not None:
            raise self.capture_flat_field_raises
        assert self.flatfield_result is not None, "test forgot to set _FakeDaemon.flatfield_result"
        return self.flatfield_result

    def get_flatfield(self, camera: str) -> Optional[FlatFieldInfo]:
        self.requested_camera = camera
        if self.get_flatfield_raises is not None:
            raise self.get_flatfield_raises
        return self.flatfield_info

    def clear_flat_field(self, camera: str) -> None:
        self.cleared_flatfield_camera = camera
        if self.clear_flat_field_raises is not None:
            raise self.clear_flat_field_raises


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

    def find_all(self):  # pragma: no cover - configuration tools never fan out
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


def _playfield_doc(**overrides: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "name": "test-field",
        "playfield": {"width_cm": 100.0, "height_cm": 80.0},
        "april_tags": [{"slug": "a1", "id": 1, "x": 0, "y": 0}],
        "aruco_tags": [
            {"slug": "u1", "id": 1, "x": -40, "y": 30},
            {"slug": "u2", "id": 2, "x": 40, "y": -30},
        ],
        "rectangles": [{"slug": "r1", "x": 10, "y": 10, "color": "blue"}],
        "dots": [{"slug": "d1", "x": -10, "y": -10, "color": "red"}],
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# get_camera_config -- AC: whole document back, verbatim
# ---------------------------------------------------------------------------


async def test_get_camera_config_returns_the_whole_document(tmp_path):
    config = CameraConfig(
        name="arducam-ov9782-usb-camera", playfield="main-playfield", settings={"exposure": 100}
    )
    daemon = _FakeDaemon(camera_config=config)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_camera_config", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured == {
        "name": "arducam-ov9782-usb-camera",
        "playfield": "main-playfield",
        "settings": {"exposure": 100},
    }
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"


async def test_get_camera_config_unknown_camera_surfaces_the_typed_error(tmp_path):
    daemon = _FakeDaemon(get_camera_config_raises=UnknownCamera("no-such-cam", ("arducam-ov9782-usb-camera",)))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_camera_config", {"camera": "no-such-cam"})

    assert result.root.isError is True
    text = result.root.content[0].text
    assert "no-such-cam" in text
    assert "arducam-ov9782-usb-camera" in text


# ---------------------------------------------------------------------------
# set_camera_config -- AC: dict in, no path, round-trips through the
# dict-based loader, malformed document rejected with zero RPCs
# ---------------------------------------------------------------------------


async def test_set_camera_config_round_trips_through_the_dict_based_loader(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)
    document = {
        "name": "arducam-ov9782-usb-camera",
        "playfield": "main-playfield",
        "settings": {"exposure": 100, "gain": 2.5},
    }

    result = await _call(server, "set_camera_config", {"document": document})

    assert result.root.isError is False
    expected = ConfigLoader().parse_camera_config(document)
    assert daemon.pushed_camera_config == expected
    assert discovery.connect_calls == [None]  # the RPC really was attempted, once


async def test_set_camera_config_malformed_document_is_rejected_with_no_rpc_attempted(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)
    document = {"playfield": "main-playfield", "settings": {}}  # no 'name'

    result = await _call(server, "set_camera_config", {"document": document})

    assert result.root.isError is True
    assert "name" in result.root.content[0].text
    assert discovery.connect_calls == []  # no daemon connection even attempted
    assert daemon.pushed_camera_config is None


async def test_set_camera_config_non_object_settings_is_rejected(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)
    document = {"name": "hd-usb-camera", "settings": "not-an-object"}

    result = await _call(server, "set_camera_config", {"document": document})

    assert result.root.isError is True
    assert discovery.connect_calls == []


# ---------------------------------------------------------------------------
# reset_camera_config
# ---------------------------------------------------------------------------


async def test_reset_camera_config_calls_the_daemon(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "reset_camera_config", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"


# ---------------------------------------------------------------------------
# get_playfield / list_playfields -- AC: to_dict()-equivalent shape,
# loadable verbatim
# ---------------------------------------------------------------------------


async def test_get_playfield_returns_the_to_dict_equivalent_shape(tmp_path):
    definition = ConfigLoader().parse_playfield(_playfield_doc(name="main-playfield"))
    daemon = _FakeDaemon(playfield=Playfield(name=definition.name, definition=definition))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_playfield", {"name": "main-playfield"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured == definition.to_dict()
    # loadable verbatim -- the round-trip contract config/loader.py documents
    reloaded = ConfigLoader().parse_playfield(structured)
    assert reloaded == definition
    assert daemon.requested_playfield_name == "main-playfield"


async def test_list_playfields_returns_every_document(tmp_path):
    d1 = ConfigLoader().parse_playfield(_playfield_doc(name="field-one"))
    d2 = ConfigLoader().parse_playfield(_playfield_doc(name="field-two"))
    daemon = _FakeDaemon(
        playfields=(
            Playfield(name=d1.name, definition=d1),
            Playfield(name=d2.name, definition=d2),
        )
    )
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "list_playfields")

    assert result.root.isError is False
    structured = result.root.structuredContent["result"]
    assert [doc["name"] for doc in structured] == ["field-one", "field-two"]
    assert structured[0] == d1.to_dict()
    assert structured[1] == d2.to_dict()


# ---------------------------------------------------------------------------
# set_playfield -- AC: dict in, round-trips, malformed rejected with
# zero RPCs
# ---------------------------------------------------------------------------


async def test_set_playfield_round_trips_through_the_dict_based_loader(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)
    document = _playfield_doc(name="main-playfield")

    result = await _call(server, "set_playfield", {"document": document})

    assert result.root.isError is False
    expected = ConfigLoader().parse_playfield(document)
    assert daemon.pushed_playfield == expected


async def test_set_playfield_duplicate_aruco_id_is_rejected_with_no_rpc_attempted(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)
    document = _playfield_doc(
        aruco_tags=[
            {"slug": "u1", "id": 3, "x": -40, "y": 30},
            {"slug": "u1-dup", "id": 3, "x": 40, "y": -30},
        ]
    )

    result = await _call(server, "set_playfield", {"document": document})

    assert result.root.isError is True
    text = result.root.content[0].text
    assert "3" in text
    assert "ArUco" in text
    assert discovery.connect_calls == []
    assert daemon.pushed_playfield is None


async def test_set_playfield_no_name_is_rejected_with_no_rpc_attempted(tmp_path):
    daemon = _FakeDaemon()
    server, discovery = _build(daemon, tmp_path)
    document = _playfield_doc()
    del document["name"]

    result = await _call(server, "set_playfield", {"document": document})

    assert result.root.isError is True
    assert "name" in result.root.content[0].text
    assert discovery.connect_calls == []


# ---------------------------------------------------------------------------
# calibrate -- AC: CalibrationResult; NoPlayfieldLinked before any frame
# ---------------------------------------------------------------------------


async def test_calibrate_returns_the_result(tmp_path):
    outcome = CalibrationResult(
        succeeded=True,
        matched_ids=(1, 2, 3),
        missing_ids=(),
        reprojection_error_cm=0.18,
        reason=None,
    )
    daemon = _FakeDaemon(calibration_result=outcome)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "calibrate", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured["succeeded"] is True
    assert structured["matched_ids"] == [1, 2, 3]
    assert structured["reprojection_error_cm"] == 0.18
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"


async def test_calibrate_estimate_intrinsics_true_reaches_the_daemon_and_reports_mode(tmp_path):
    """Sprint 030 ticket 002: ``estimate_intrinsics`` threads through to
    ``Daemon.calibrate``, and the new mode/count fields round-trip in
    the tool's structured output."""
    outcome = CalibrationResult(
        succeeded=True,
        matched_ids=tuple(range(1, 11)),
        missing_ids=(),
        reprojection_error_cm=0.18,
        intrinsics_estimated=True,
        intrinsics_mode="coplanar-constrained",
        elevated_features_used=0,
    )
    daemon = _FakeDaemon(calibration_result=outcome)
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "calibrate",
        {"camera": "arducam-ov9782-usb-camera", "estimate_intrinsics": True},
    )

    assert result.root.isError is False
    assert daemon.requested_estimate_intrinsics is True
    structured = result.root.structuredContent
    assert structured["intrinsics_estimated"] is True
    assert structured["intrinsics_mode"] == "coplanar-constrained"
    assert structured["elevated_features_used"] == 0


async def test_calibrate_without_estimate_intrinsics_defaults_to_false(tmp_path):
    daemon = _FakeDaemon(calibration_result=CalibrationResult(succeeded=True, matched_ids=(), missing_ids=()))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "calibrate", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert daemon.requested_estimate_intrinsics is False


async def test_calibrate_failure_reports_missing_ids(tmp_path):
    outcome = CalibrationResult(succeeded=False, matched_ids=(1, 2), missing_ids=(3,), reason=None)
    daemon = _FakeDaemon(calibration_result=outcome)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "calibrate", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert result.root.structuredContent["succeeded"] is False
    assert result.root.structuredContent["missing_ids"] == [3]


async def test_calibrate_no_playfield_linked_fails_before_any_frame_is_requested(tmp_path):
    """AC: a camera with no playfield link fails with NoPlayfieldLinked
    before any frame is requested -- verified against a fake Daemon
    whose ``get_frame`` raises ``AssertionError`` if ever called."""
    daemon = _FakeDaemon(calibrate_raises=NoPlayfieldLinked("arducam-ov9782-usb-camera"))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "calibrate", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is True
    assert "arducam-ov9782-usb-camera" in result.root.content[0].text
    # daemon.get_frame would raise AssertionError if this tool ever
    # called it -- it did not, since no such exception escaped above.


# ---------------------------------------------------------------------------
# get_calibration -- AC: Optional[Calibration], never a fabricated default
# ---------------------------------------------------------------------------


async def test_get_calibration_returns_none_when_never_calibrated(tmp_path):
    daemon = _FakeDaemon(calibration=None)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_calibration", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert result.root.structuredContent["result"] is None
    assert result.root.content == []


async def test_get_calibration_returns_the_calibration(tmp_path):
    calibration = Calibration(stale=False, calibrated_at=1_700_000_000.0, playfield="main-playfield")
    daemon = _FakeDaemon(calibration=calibration)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_calibration", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    structured = result.root.structuredContent["result"]
    assert structured["stale"] is False
    assert structured["playfield"] == "main-playfield"


async def test_get_calibration_exposes_the_lens_provenance(tmp_path):
    calibration = Calibration(
        stale=False,
        calibrated_at=1.0,
        playfield="main-playfield",
        lens_source="measured",
        lens_views=14,
        lens_rms_px=0.4,
        lens_residual_px=0.6,
        lens_installed_at=99.0,
    )
    server, _ = _build(_FakeDaemon(calibration=calibration), tmp_path)

    result = await _call(server, "get_calibration", {"camera": "c"})

    structured = result.root.structuredContent["result"]
    assert structured["lens_source"] == "measured"
    assert structured["lens_views"] == 14
    assert structured["lens_residual_px"] == 0.6


async def test_calibrate_exposes_lens_model_source_and_warning(tmp_path):
    outcome = CalibrationResult(
        succeeded=True,
        matched_ids=(1, 2, 3, 4),
        missing_ids=(),
        lens_model_source="measured",
        lens_warning=None,
    )
    server, _ = _build(_FakeDaemon(calibration_result=outcome), tmp_path)

    result = await _call(server, "calibrate", {"camera": "c"})

    structured = result.root.structuredContent
    assert structured["lens_model_source"] == "measured"
    assert structured["lens_warning"] is None


# ---------------------------------------------------------------------------
# daemon parameter -- AC: late-binding, per-call override of the session default
# ---------------------------------------------------------------------------


async def test_daemon_parameter_targets_a_specific_host_for_one_call(tmp_path):
    daemon = _FakeDaemon(camera_config=CameraConfig(name="c", playfield=None, settings={}))
    server, discovery = _build(daemon, tmp_path)

    result = await _call(server, "get_camera_config", {"camera": "c", "daemon": "pi-field.local"})

    assert result.root.isError is False
    assert discovery.connect_calls == ["pi-field.local"]


# ---------------------------------------------------------------------------
# capture_flatfield / get_flatfield / clear_flatfield -- sprint 037 ticket 005
# ---------------------------------------------------------------------------


async def test_capture_flatfield_returns_the_result_on_success(tmp_path):
    outcome = FlatFieldResult(
        succeeded=True,
        spread_before=(2.65,),
        spread_after=(1.11,),
        reason=None,
    )
    daemon = _FakeDaemon(flatfield_result=outcome)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "capture_flatfield", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured["succeeded"] is True
    assert structured["spread_before"] == [2.65]
    assert structured["spread_after"] == [1.11]
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    # gain is never exposed client-side (client/daemon_client.py always sets it
    # None; the MCP tool must not fabricate or leak anything either) -- absent
    # from the structured content entirely.
    assert "gain" not in structured or structured["gain"] is None


async def test_capture_flatfield_forwards_optional_parameters(tmp_path):
    outcome = FlatFieldResult(succeeded=True, spread_before=(1.3,), spread_after=(1.05,))
    daemon = _FakeDaemon(flatfield_result=outcome)
    server, _ = _build(daemon, tmp_path)

    result = await _call(
        server,
        "capture_flatfield",
        {
            "camera": "arducam-ov9782-usb-camera",
            "exposure": 1.0,
            "frames": 6,
            "force": True,
            "dry_run": True,
        },
    )

    assert result.root.isError is False
    assert daemon.requested_flatfield_kwargs == {
        "exposure": 1.0,
        "frames": 6,
        "force": True,
        "dry_run": True,
    }


async def test_capture_flatfield_defaults_are_all_off(tmp_path):
    outcome = FlatFieldResult(succeeded=True, spread_before=(1.0,), spread_after=(1.0,))
    daemon = _FakeDaemon(flatfield_result=outcome)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "capture_flatfield", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert daemon.requested_flatfield_kwargs == {
        "exposure": None,
        "frames": None,
        "force": False,
        "dry_run": False,
    }


async def test_capture_flatfield_refusal_is_reported_as_data_not_an_error(tmp_path):
    """A failing quality-gate check is ``succeeded=False`` with a
    ``reason`` -- explicit failure as data, mirroring ``calibrate``'s
    ``CalibrationResult`` contract, never a tool error."""
    outcome = FlatFieldResult(
        succeeded=False,
        spread_before=(2.65,),
        spread_after=(1.9,),
        reason="spread_after exceeds threshold",
    )
    daemon = _FakeDaemon(flatfield_result=outcome)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "capture_flatfield", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    structured = result.root.structuredContent
    assert structured["succeeded"] is False
    assert structured["reason"] == "spread_after exceeds threshold"


async def test_capture_flatfield_no_field_mask_surfaces_the_typed_error(tmp_path):
    daemon = _FakeDaemon(capture_flat_field_raises=NoFieldMask("arducam-ov9782-usb-camera"))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "capture_flatfield", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is True
    assert "arducam-ov9782-usb-camera" in result.root.content[0].text


async def test_get_flatfield_returns_none_when_never_captured(tmp_path):
    daemon = _FakeDaemon(flatfield_info=None)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_flatfield", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert result.root.structuredContent["result"] is None
    assert result.root.content == []


async def test_get_flatfield_returns_the_info(tmp_path):
    info = FlatFieldInfo(present=True, captured_at=1_700_000_000.0, stale=False)
    daemon = _FakeDaemon(flatfield_info=info)
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_flatfield", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    structured = result.root.structuredContent["result"]
    assert structured["present"] is True
    assert structured["stale"] is False
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"


async def test_get_flatfield_unknown_camera_surfaces_the_typed_error(tmp_path):
    daemon = _FakeDaemon(get_flatfield_raises=UnknownCamera("no-such-cam", ()))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "get_flatfield", {"camera": "no-such-cam"})

    assert result.root.isError is True
    assert "no-such-cam" in result.root.content[0].text


async def test_clear_flatfield_calls_the_daemon(tmp_path):
    daemon = _FakeDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "clear_flatfield", {"camera": "arducam-ov9782-usb-camera"})

    assert result.root.isError is False
    assert daemon.cleared_flatfield_camera == "arducam-ov9782-usb-camera"


async def test_clear_flatfield_error_is_surfaced(tmp_path):
    daemon = _FakeDaemon(clear_flat_field_raises=UnknownCamera("no-such-cam", ()))
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "clear_flatfield", {"camera": "no-such-cam"})

    assert result.root.isError is True
    assert "no-such-cam" in result.root.content[0].text


async def test_capture_flatfield_daemon_parameter_targets_a_specific_host(tmp_path):
    outcome = FlatFieldResult(succeeded=True, spread_before=(1.0,), spread_after=(1.0,))
    daemon = _FakeDaemon(flatfield_result=outcome)
    server, discovery = _build(daemon, tmp_path)

    result = await _call(
        server,
        "capture_flatfield",
        {"camera": "c", "daemon": "pi-field.local"},
    )

    assert result.root.isError is False
    assert discovery.connect_calls == ["pi-field.local"]


# ---------------------------------------------------------------------------
# list_cameras -- AC: MCP output includes the new CameraState fields
# (sprint 037 ticket 005) -- registered by mcp/tools/perception.py, exercised
# here since that's where every other CameraState-surfacing shape is covered
# ---------------------------------------------------------------------------


async def test_list_cameras_includes_flatfield_state(tmp_path):
    from aprilcam.types import CameraState

    state = CameraState(
        name="arducam-ov9782-usb-camera",
        present=True,
        usable=True,
        playfield="main-playfield",
        calibrated=True,
        flatfield_present=True,
        flatfield_stale=True,
        number=3,
    )

    @dataclass
    class _PerceptionDaemon(_FakeDaemon):
        def list_cameras(self):
            return (state,)

    daemon = _PerceptionDaemon()
    server, _ = _build(daemon, tmp_path)

    result = await _call(server, "list_cameras")

    assert result.root.isError is False
    structured = result.root.structuredContent["result"]
    assert structured[0]["flatfield_present"] is True
    assert structured[0]["flatfield_stale"] is True
