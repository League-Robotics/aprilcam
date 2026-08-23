"""Tests for ``aprilcam.cli.playfield`` (ticket 022-001; ``cmd_image``
added by ticket 022-005; ``cmd_view`` added by ticket 022-006;
``cmd_annotations`` added by ticket 024-004; ``cmd_where`` added by
ticket 025-002).

Headless, matching ``test_camera_cli.py``'s established style: a fake
``ConfigLoader`` and a fake ``Daemon``/``Discovery`` pair injected
through ``cmd_config``/``cmd_image``/``cmd_view``'s keyword seams -- no
real file I/O, no daemon. ``cmd_view``'s own tests inject ``run_view``
exactly like ``camera.py``'s ``cmd_view`` tests inject it, so none of
these ever touch ``view/viewer.py``'s real poll loop or GUI toolkit --
that machinery has its own headless tests in
``tests/aprilcam/view/test_viewer.py``.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

import pytest

from aprilcam.cli import playfield as playfield_cli
from aprilcam.config.interfaces import Feature
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.errors import (
    CameraNotPresent,
    ConnectionFailed,
    DiscoveryFailed,
    MalformedDocument,
    NotCalibrated,
    UnknownPlayfield,
)
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    CameraState,
    Circle,
    ImageFrame,
    Marker,
    PathShape,
    Polygon,
    Style,
    Text,
    WorldPoint,
)
from aprilcam.view.viewer import NoDisplayAvailable


def _definition(name: str = "main-playfield") -> PlayfieldDefinition:
    return PlayfieldDefinition(
        name=name,
        width_cm=134.3,
        height_cm=89.3,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"u{i}", x=float(i), y=float(-i), tag_id=i)
            for i in range(1, 11)
        ),
    )


class _FakePlayfieldLoader:
    def __init__(self, definition=None, raises: Optional[Exception] = None):
        self._definition = definition
        self._raises = raises
        self.requested_path = None

    def load_playfield(self, path):
        self.requested_path = path
        if self._raises is not None:
            raise self._raises
        return self._definition

    def load_camera_config(self, path):  # pragma: no cover - not exercised here
        raise NotImplementedError


def _camera(name: str, *, playfield: Optional[str] = None, present: bool = True) -> CameraState:
    return CameraState(
        name=name, present=present, usable=present, playfield=playfield, calibrated=playfield is not None
    )


def _frame(data: bytes = b"\x89PNG\r\n\x1a\nrest", *, px_per_cm: float = 5.958, width: int = 800, height: int = 532) -> ImageFrame:
    return ImageFrame(
        data=data,
        width=width,
        height=height,
        encoding="png",
        timestamp=0.0,
        deskewed=True,
        px_per_cm=px_per_cm,
        world_extent=(134.3, 89.3),
    )


@dataclass
class _FakeDaemon:
    set_playfield_raises: Optional[Exception] = None
    pushed: object = None
    cameras: Tuple[CameraState, ...] = ()
    frame: Optional[ImageFrame] = None
    get_frame_raises: Optional[Exception] = None
    requested_camera: Optional[str] = None
    requested_encoding: Optional[str] = None
    get_playfield_returns: object = None
    get_playfield_raises: Optional[Exception] = None
    get_playfield_requested: Optional[str] = None
    list_playfields_returns: Tuple[object, ...] = ()
    list_playfields_raises: Optional[Exception] = None
    annotation_set_returns: object = None
    get_annotations_raises: Optional[Exception] = None
    get_annotations_requested: Optional[Tuple[str, Optional[str]]] = None
    clear_annotations_raises: Optional[Exception] = None
    clear_annotations_requested: Optional[Tuple[str, Optional[str]]] = None
    find_feature_returns: Tuple[Feature, ...] = ()
    find_feature_raises: Optional[Exception] = None
    find_feature_requested: Optional[Tuple[str, str]] = None

    def set_playfield(self, definition) -> None:
        if self.set_playfield_raises is not None:
            raise self.set_playfield_raises
        self.pushed = definition

    def list_cameras(self) -> Tuple[CameraState, ...]:
        return self.cameras

    def list_playfields(self) -> Tuple[object, ...]:
        if self.list_playfields_raises is not None:
            raise self.list_playfields_raises
        return self.list_playfields_returns

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        self.requested_camera = camera
        self.requested_encoding = encoding
        if self.get_frame_raises is not None:
            raise self.get_frame_raises
        return self.frame if self.frame is not None else _frame()

    def get_playfield(self, name: str):
        self.get_playfield_requested = name
        if self.get_playfield_raises is not None:
            raise self.get_playfield_raises
        assert self.get_playfield_returns is not None, "test forgot to set get_playfield_returns"
        return self.get_playfield_returns

    def get_annotations(self, playfield: str, layer: Optional[str] = None):
        self.get_annotations_requested = (playfield, layer)
        if self.get_annotations_raises is not None:
            raise self.get_annotations_raises
        assert self.annotation_set_returns is not None, "test forgot to set annotation_set_returns"
        return self.annotation_set_returns

    def clear_annotations(self, playfield: str, layer: Optional[str] = None) -> None:
        self.clear_annotations_requested = (playfield, layer)
        if self.clear_annotations_raises is not None:
            raise self.clear_annotations_raises

    def find_feature(self, playfield: str, query: str) -> Tuple[Feature, ...]:
        self.find_feature_requested = (playfield, query)
        if self.find_feature_raises is not None:
            raise self.find_feature_raises
        return self.find_feature_returns


@dataclass
class _FakeDiscovery:
    daemon: Optional[_FakeDaemon] = None
    connect_raises: Optional[Exception] = None
    connected_with: Optional[str] = None
    daemons_by_host: dict = field(default_factory=dict)
    infos: Tuple[object, ...] = ()
    find_all_raises: Optional[Exception] = None

    def connect(self, host: Optional[str] = None):
        self.connected_with = host
        if host in self.daemons_by_host:
            target = self.daemons_by_host[host]
            if isinstance(target, Exception):
                raise target
            return target
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.daemon

    def find_all(self):
        if self.find_all_raises is not None:
            raise self.find_all_raises
        return self.infos


@dataclass(frozen=True)
class _Info:
    host: str
    is_local: bool


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        daemon=None,
        file="main-playfield.json",
        camera=None,
        output=None,
        base64=False,
        encoding="jpeg",
        force=False,
        all=False,
        dump=False,
        playfield="main-playfield",
        clear=False,
        layer=None,
        query="the blue dot",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_config_pushes_the_loaded_definition(capsys, tmp_path):
    definition = _definition()
    loader = _FakePlayfieldLoader(definition=definition)
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)
    file_path = tmp_path / "main-playfield.json"

    code = playfield_cli.cmd_config(
        _args(file=str(file_path)),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 0
    assert loader.requested_path == Path(file_path)
    assert daemon.pushed is definition
    out = capsys.readouterr().out
    assert "main-playfield" in out
    assert "10 features" in out
    assert "10 required ArUco ids" in out


def test_config_malformed_document_is_rejected_before_any_rpc(capsys):
    loader = _FakePlayfieldLoader(raises=MalformedDocument("duplicate ArUco id 3"))
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_config(
        _args(), discovery_factory=lambda: discovery, loader_factory=lambda: loader
    )

    assert code == 1
    assert "duplicate ArUco id 3" in capsys.readouterr().err
    assert daemon.pushed is None


def test_config_reports_a_daemon_side_rejection(capsys):
    loader = _FakePlayfieldLoader(definition=_definition())
    daemon = _FakeDaemon(set_playfield_raises=UnknownPlayfield("main-playfield", ()))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_config(
        _args(), discovery_factory=lambda: discovery, loader_factory=lambda: loader
    )

    assert code == 1
    assert "main-playfield" in capsys.readouterr().err


def test_config_reports_connection_failure(capsys):
    from aprilcam.errors import ConnectionFailed

    loader = _FakePlayfieldLoader(definition=_definition())
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))

    code = playfield_cli.cmd_config(
        _args(daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


def test_config_passes_the_daemon_flag_through_to_connect():
    loader = _FakePlayfieldLoader(definition=_definition())
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    playfield_cli.cmd_config(
        _args(daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert discovery.connected_with == "pi-field.local"


# ---------------------------------------------------------------------------
# playfield config --dump -- AC: UC-24, ticket 023-005 (playfield's first
# CLI *read* path)
# ---------------------------------------------------------------------------


def test_config_dump_flag_is_wired():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "config", "main-playfield", "--dump"])
    assert args.func is playfield_cli.cmd_config
    assert args.file == "main-playfield"
    assert args.dump is True


def test_config_dump_prints_the_live_document_in_exact_on_disk_json(capsys):
    from aprilcam.playfield.playfield import Playfield

    definition = _definition()
    playfield = Playfield(name=definition.name, definition=definition)
    daemon = _FakeDaemon(get_playfield_returns=playfield)
    discovery = _FakeDiscovery(daemon=daemon)
    loader = _FakePlayfieldLoader()  # never touched -- --dump skips the loader entirely

    code = playfield_cli.cmd_config(
        _args(file="main-playfield", dump=True),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 0
    assert daemon.get_playfield_requested == "main-playfield"
    assert loader.requested_path is None  # no file was ever loaded client-side
    printed = json.loads(capsys.readouterr().out)
    assert printed == definition.to_dict()
    assert printed["name"] == "main-playfield"  # ConfigDocument.to_dict's contract


def test_config_dump_round_trips_through_config_loader(capsys, tmp_path):
    """The binding contract ``config/interfaces.py`` states for
    ``ConfigLoader``: ``load(x.to_dict()) == x``. Dumped output, written
    to a file and re-loaded through the real ``ConfigLoader`` (not the
    fake used elsewhere in this file), reproduces an identical
    document -- UC-24.S-1's round trip, exercised headlessly against a
    fake ``Daemon``."""
    from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
    from aprilcam.playfield.playfield import Playfield

    definition = _definition()
    playfield = Playfield(name=definition.name, definition=definition)
    daemon = _FakeDaemon(get_playfield_returns=playfield)
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_config(
        _args(file="main-playfield", dump=True), discovery_factory=lambda: discovery
    )
    assert code == 0

    dumped_path = tmp_path / "dumped.json"
    dumped_path.write_text(capsys.readouterr().out)
    reloaded = JsonConfigLoader().load_playfield(dumped_path)

    assert reloaded == definition


def test_config_dump_reports_a_daemon_side_rejection(capsys):
    daemon = _FakeDaemon(get_playfield_raises=UnknownPlayfield("ghost-field", ("main-playfield",)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_config(
        _args(file="ghost-field", dump=True), discovery_factory=lambda: discovery
    )

    assert code == 1
    assert "ghost-field" in capsys.readouterr().err


def test_config_dump_passes_the_daemon_flag_through_to_connect():
    from aprilcam.playfield.playfield import Playfield

    definition = _definition()
    playfield = Playfield(name=definition.name, definition=definition)
    daemon = _FakeDaemon(get_playfield_returns=playfield)
    discovery = _FakeDiscovery(daemon=daemon)

    playfield_cli.cmd_config(
        _args(file="main-playfield", dump=True, daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
    )

    assert discovery.connected_with == "pi-field.local"


def test_config_dump_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "config", "main-playfield", "--dump", "-h"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# playfield image -- AC: UC-06.S-1/S-3, UC-19.S-1/S-2, ticket 022-005
# ---------------------------------------------------------------------------


def test_image_writes_the_deskewed_frame_to_a_file(tmp_path):
    frame = _frame(data=b"\x89PNG\r\n\x1a\nfield-bytes", width=800, height=532, px_per_cm=800 / 134.3)
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield="main-playfield"),), frame=frame)
    discovery = _FakeDiscovery(daemon=daemon)
    out_path = tmp_path / "field.png"

    code = playfield_cli.cmd_image(
        _args(output=str(out_path), encoding="png"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert out_path.read_bytes() == frame.data
    assert daemon.requested_camera == "arducam"  # resolved automatically -- exactly one linked
    assert daemon.requested_encoding == "png"


def test_image_reports_the_resolved_dimensions_and_px_per_cm(capsys, tmp_path):
    frame = _frame(width=800, height=532, px_per_cm=800 / 134.3)
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield="main-playfield"),), frame=frame)
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_image(
        _args(output=str(tmp_path / "field.png")), discovery_factory=lambda: discovery
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "800x532" in out
    assert "5.9" in out  # px_per_cm, printed to 3 decimals


def test_image_default_streams_raw_bytes_to_a_non_tty_stdout(capsysbinary, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    daemon = _FakeDaemon(
        cameras=(_camera("arducam", playfield="main-playfield"),), frame=_frame(data=b"deskewed-bytes")
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_image(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert capsysbinary.readouterr().out == b"deskewed-bytes"


def test_image_base64_mode_writes_encoded_text_to_stdout(capsys):
    daemon = _FakeDaemon(
        cameras=(_camera("arducam", playfield="main-playfield"),), frame=_frame(data=b"raw-bytes")
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_image(_args(base64=True), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out.strip()
    assert base64.b64decode(out) == b"raw-bytes"


def test_image_raw_mode_refuses_an_attached_terminal_without_force(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield="main-playfield"),))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_image(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "terminal" in err
    assert daemon.requested_camera is None  # refused before any capture RPC


def test_image_unwritable_output_directory_fails_before_any_capture(capsys, tmp_path):
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield="main-playfield"),))
    discovery = _FakeDiscovery(daemon=daemon)
    bad_path = tmp_path / "does-not-exist" / "field.png"

    code = playfield_cli.cmd_image(_args(output=str(bad_path)), discovery_factory=lambda: discovery)

    assert code == 1
    assert "directory does not exist" in capsys.readouterr().err
    assert daemon.requested_camera is None


def test_image_uncalibrated_camera_surfaces_not_calibrated_and_writes_no_file(capsys, tmp_path):
    daemon = _FakeDaemon(
        cameras=(_camera("arducam", playfield="main-playfield"),),
        get_frame_raises=NotCalibrated("'arducam' is not calibrated"),
    )
    discovery = _FakeDiscovery(daemon=daemon)
    out_path = tmp_path / "field.png"

    code = playfield_cli.cmd_image(_args(output=str(out_path)), discovery_factory=lambda: discovery)

    assert code == 1
    assert "not calibrated" in capsys.readouterr().err
    assert not out_path.exists()


def test_image_with_no_linked_camera_errors_naming_camera_config(capsys):
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield=None),))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_image(_args(base64=True), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "no camera is linked" in err
    assert "camera config" in err
    assert daemon.requested_camera is None


def test_image_with_several_linked_cameras_errors_naming_them_and_pointing_at_camera_flag(capsys):
    daemon = _FakeDaemon(
        cameras=(
            _camera("arducam", playfield="main-playfield"),
            _camera("hd-usb-camera", playfield="main-playfield"),
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_image(_args(base64=True), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "arducam" in err and "hd-usb-camera" in err
    assert "--camera" in err
    assert daemon.requested_camera is None


def test_image_explicit_camera_flag_skips_resolution_entirely(tmp_path):
    """An explicit ``--camera`` is never second-guessed against what is
    currently linked -- not even queried against, since ``list_cameras``
    is only ever called when ``--camera`` is absent."""

    class _ExplodingCameraList:
        def list_cameras(self):
            raise AssertionError("must not call list_cameras when --camera is given")

        def get_frame(self, camera, *, deskewed=False, encoding="jpeg"):
            assert camera == "chosen-camera"
            return _frame()

    discovery = _FakeDiscovery(daemon=_ExplodingCameraList())
    out_path = tmp_path / "field.png"

    code = playfield_cli.cmd_image(
        _args(camera="chosen-camera", output=str(out_path)), discovery_factory=lambda: discovery
    )

    assert code == 0


def test_image_present_but_absent_linked_camera_is_not_offered_by_resolution(capsys):
    """A camera whose config still names a playfield but is not
    currently present (UC-25) is not a candidate for automatic
    resolution -- mirrors ``CameraState.present`` gating everywhere
    else a camera is picked for a live operation."""
    daemon = _FakeDaemon(cameras=(_camera("unplugged-cam", playfield="main-playfield", present=False),))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_image(_args(base64=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "no camera is linked" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# playfield view -- AC: UC-18.S-1/S-2, ticket 022-006
# ---------------------------------------------------------------------------


def test_view_refuses_all_before_ever_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect when --all is refused up front")

    code = playfield_cli.cmd_view(_args(all=True), discovery_factory=lambda: _ExplodingDiscovery())

    assert code == 1
    err = capsys.readouterr().err
    assert "--all" in err
    assert "single" in err.lower()


def test_view_resolves_the_one_linked_camera_and_hands_it_to_run_view():
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield="main-playfield"),))
    discovery = _FakeDiscovery(daemon=daemon)
    calls = []

    code = playfield_cli.cmd_view(
        _args(),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: calls.append((camera, connected)),
    )

    assert code == 0
    assert calls == [("arducam", daemon)]


def test_view_explicit_camera_flag_skips_resolution_entirely():
    class _ExplodingCameraList:
        def list_cameras(self):
            raise AssertionError("must not call list_cameras when --camera is given")

    discovery = _FakeDiscovery(daemon=_ExplodingCameraList())
    calls = []

    code = playfield_cli.cmd_view(
        _args(camera="chosen-camera"),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: calls.append((camera, connected)),
    )

    assert code == 0
    assert calls == [("chosen-camera", discovery.daemon)]


def test_view_with_no_linked_camera_errors_naming_camera_config(capsys):
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield=None),))
    discovery = _FakeDiscovery(daemon=daemon)
    calls = []

    code = playfield_cli.cmd_view(
        _args(),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: calls.append((camera, connected)),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "no camera is linked" in err
    assert "camera config" in err
    assert calls == []


def test_view_with_several_linked_cameras_errors_naming_them_and_pointing_at_camera_flag(capsys):
    daemon = _FakeDaemon(
        cameras=(
            _camera("arducam", playfield="main-playfield"),
            _camera("hd-usb-camera", playfield="main-playfield"),
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)
    calls = []

    code = playfield_cli.cmd_view(
        _args(),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: calls.append((camera, connected)),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "arducam" in err and "hd-usb-camera" in err
    assert "--camera" in err
    assert calls == []


def test_view_reports_a_connection_failure_without_ever_calling_run_view(capsys):
    from aprilcam.errors import ConnectionFailed

    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))
    calls = []

    code = playfield_cli.cmd_view(
        _args(daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: calls.append((camera, connected)),
    )

    assert code == 1
    assert calls == []
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


def test_view_no_display_points_at_playfield_image_instead_of_a_backtrace(capsys):
    """Mirrors UC-16.S-3, for UC-18's own headless case."""
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield="main-playfield"),))
    discovery = _FakeDiscovery(daemon=daemon)

    def raising_run_view(camera, connected):
        raise NoDisplayAvailable("no $DISPLAY or $WAYLAND_DISPLAY set")

    code = playfield_cli.cmd_view(
        _args(), discovery_factory=lambda: discovery, run_view=raising_run_view
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "playfield image" in err


def test_view_uncalibrated_camera_points_at_camera_calibrate(capsys):
    """UC-18.S-2: names calibration as the missing step and points at
    ``camera calibrate`` -- ``run_playfield_view`` itself never falls
    back to a raw frame (its own tests in ``test_viewer.py`` prove
    that); this only asserts the CLI boundary adds the pointer."""
    daemon = _FakeDaemon(cameras=(_camera("arducam", playfield="main-playfield"),))
    discovery = _FakeDiscovery(daemon=daemon)

    def raising_run_view(camera, connected):
        raise NotCalibrated("'arducam' is not calibrated")

    code = playfield_cli.cmd_view(
        _args(), discovery_factory=lambda: discovery, run_view=raising_run_view
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "not calibrated" in err
    assert "camera calibrate" in err
    assert "arducam" in err


def test_view_surfaces_a_typed_error_raised_mid_view(capsys):
    daemon = _FakeDaemon(cameras=(_camera("hd-usb-camera", playfield="main-playfield"),))
    discovery = _FakeDiscovery(daemon=daemon)

    def raising_run_view(camera, connected):
        raise CameraNotPresent(camera)

    code = playfield_cli.cmd_view(
        _args(), discovery_factory=lambda: discovery, run_view=raising_run_view
    )

    assert code == 1
    assert "not currently present" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# playfield annotations -- AC: UC-28.S-1/S-3/S-4/S-5, ticket 024-004
# ---------------------------------------------------------------------------


def _annotation_set(*annotations: Annotation, revision: int = 1) -> AnnotationSet:
    return AnnotationSet(annotations=tuple(annotations), revision=revision, timestamp=0.0)


def test_annotations_lists_the_current_set_grouped_by_layer(capsys):
    annotations = (
        Annotation(id="goal", layer="planner", shape=Marker(at=WorldPoint(50.0, 30.0), label="goal")),
        Annotation(
            id="route",
            layer="planner",
            shape=PathShape(points=(WorldPoint(0.0, 0.0), WorldPoint(25.0, 10.0))),
        ),
        Annotation(id="zone", layer="robot-3", shape=Polygon(points=(WorldPoint(0.0, 0.0),))),
    )
    daemon = _FakeDaemon(annotation_set_returns=_annotation_set(*annotations, revision=7))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.get_annotations_requested == ("main-playfield", None)
    out = capsys.readouterr().out
    assert "revision 7" in out
    assert "planner" in out and "robot-3" in out
    assert "goal" in out and "50.00" in out and "30.00" in out
    assert "route" in out and "Marker" in out and "PathShape" in out
    assert "zone" in out and "Polygon" in out


def test_annotations_empty_set_reads_as_explicitly_empty_not_an_error(capsys):
    daemon = _FakeDaemon(annotation_set_returns=_annotation_set(revision=0))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "revision 0" in out
    assert "no annotations" in out


def test_annotations_prints_every_shape_kind_with_its_geometry(capsys):
    annotations = (
        Annotation(id="c", layer="x", shape=Circle(center=WorldPoint(1.0, 2.0), radius_cm=5.0)),
        Annotation(id="t", layer="x", shape=Text(at=WorldPoint(3.0, 4.0), text="hello")),
    )
    daemon = _FakeDaemon(annotation_set_returns=_annotation_set(*annotations))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "circle" in out and "radius=5.00cm" in out
    assert "text" in out and "'hello'" in out


def test_annotations_unknown_playfield_surfaces_the_daemon_error(capsys):
    daemon = _FakeDaemon(get_annotations_raises=UnknownPlayfield("ghost-field", ("main-playfield",)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(
        _args(playfield="ghost-field"), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "ghost-field" in err
    assert "main-playfield" in err


def test_annotations_passes_the_daemon_flag_through_to_connect():
    daemon = _FakeDaemon(annotation_set_returns=_annotation_set())
    discovery = _FakeDiscovery(daemon=daemon)

    playfield_cli.cmd_annotations(
        _args(daemon="pi-field.local"), discovery_factory=lambda: discovery
    )

    assert discovery.connected_with == "pi-field.local"


def test_annotations_all_fans_out_and_reports_one_unreachable_daemon(capsys):
    good = _FakeDaemon(annotation_set_returns=_annotation_set(revision=3))
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True), _Info(host="unreachable-host", is_local=False)),
        daemons_by_host={
            None: good,
            "unreachable-host": ConnectionFailed("unreachable-host", reason="no answer"),
        },
    )

    code = playfield_cli.cmd_annotations(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1  # partial failure is visible in the exit code
    out, err = capsys.readouterr()
    assert "localhost (local):" in out
    assert "revision 3" in out
    assert "unreachable-host: unreachable" in err
    assert "no answer" in err


def test_annotations_all_with_zero_daemons_found_is_success_not_error(capsys):
    discovery = _FakeDiscovery(infos=())

    code = playfield_cli.cmd_annotations(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no daemons found)" in capsys.readouterr().out


def test_annotations_all_reports_discovery_failed_distinctly(capsys):
    discovery = _FakeDiscovery(find_all_raises=DiscoveryFailed("multicast blocked"))

    code = playfield_cli.cmd_annotations(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "multicast blocked" in capsys.readouterr().err


def test_annotations_clear_clears_every_layer_by_default(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(_args(clear=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.clear_annotations_requested == ("main-playfield", None)
    assert "main-playfield" in capsys.readouterr().out


def test_annotations_clear_with_layer_clears_only_that_layer(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(
        _args(clear=True, layer="planner"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert daemon.clear_annotations_requested == ("main-playfield", "planner")
    out = capsys.readouterr().out
    assert "planner" in out and "main-playfield" in out


def test_annotations_clearing_an_absent_layer_is_a_quiet_no_op(capsys):
    """``clear_annotations`` clearing an absent layer is a no-op
    enforced daemon-side (``daemon/interfaces.py``'s own docstring) --
    the fake simply never raises, and the CLI reports success exactly
    as it would for a layer that did exist, since it cannot and should
    not distinguish the two client-side."""
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(
        _args(clear=True, layer="never-existed"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert daemon.clear_annotations_requested == ("main-playfield", "never-existed")


def test_annotations_clear_reports_a_daemon_side_unknown_playfield(capsys):
    daemon = _FakeDaemon(clear_annotations_raises=UnknownPlayfield("ghost-field", ("main-playfield",)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(
        _args(clear=True, playfield="ghost-field"), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "ghost-field" in err
    assert "main-playfield" in err


def test_annotations_clear_all_is_refused_before_ever_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect when --clear --all is refused up front")

    code = playfield_cli.cmd_annotations(
        _args(clear=True, all=True), discovery_factory=lambda: _ExplodingDiscovery()
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "--all" in err
    assert "single" in err.lower()


def test_annotations_style_default_does_not_affect_shape_formatting():
    """A cheap smoke check that ``Style`` (unused by ``_format_shape``
    -- geometry only, per its docstring) still constructs and an
    ``Annotation`` carrying the default is listed without error."""
    daemon = _FakeDaemon(
        annotation_set_returns=_annotation_set(
            Annotation(id="a", layer="x", shape=Marker(at=WorldPoint(0.0, 0.0)), style=Style())
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_annotations(_args(), discovery_factory=lambda: discovery)

    assert code == 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="aprilcam")
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    playfield_cli.add_subparser(subparsers)
    return top


def test_config_is_wired_to_cmd_config():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "config", "main-playfield.json"])
    assert args.func is playfield_cli.cmd_config
    assert args.file == "main-playfield.json"
    assert args.daemon is None
    assert args.dump is False


def test_config_requires_a_file_positional_argument():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["playfield", "config"])


def test_playfield_with_no_subcommand_uses_the_incomplete_command_default(capsys):
    parser = _build_parser()
    args = parser.parse_args(["playfield"])

    code = args.func(args)

    assert code == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_config_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "config", "main-playfield.json", "-h"])
    assert excinfo.value.code == 0


def test_image_is_wired_to_cmd_image_with_no_camera_positional():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "image"])
    assert args.func is playfield_cli.cmd_image
    assert args.camera is None
    assert args.daemon is None
    assert args.output is None
    assert args.base64 is False
    assert args.encoding == "jpeg"
    assert args.force is False


def test_image_accepts_an_explicit_camera_flag():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "image", "--camera", "3"])
    assert args.camera == "3"


def test_image_output_and_base64_are_mutually_exclusive():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["playfield", "image", "-o", "f.png", "--base64"])


def test_image_rejects_an_unsupported_encoding_at_parse_time():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["playfield", "image", "--encoding", "bmp"])


def test_image_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "image", "-h"])
    assert excinfo.value.code == 0


def test_view_is_wired_to_cmd_view_with_no_camera_positional():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "view"])
    assert args.func is playfield_cli.cmd_view
    assert args.camera is None
    assert args.daemon is None
    assert args.all is False


def test_view_accepts_an_explicit_camera_flag():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "view", "--camera", "3"])
    assert args.camera == "3"


def test_view_accepts_all_at_parse_time_but_cmd_view_refuses_it_at_runtime():
    """``--all`` is wired onto this subparser (so ``-h`` documents it
    and the refusal message can explain itself, matching ``camera
    view``'s pattern) -- argparse accepts it; ``cmd_view`` is what
    refuses it, exercised above."""
    parser = _build_parser()
    args = parser.parse_args(["playfield", "view", "--all"])
    assert args.all is True


def test_view_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "view", "-h"])
    assert excinfo.value.code == 0


def test_annotations_is_wired_to_cmd_annotations_with_a_required_playfield_positional():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "annotations", "main-playfield"])
    assert args.func is playfield_cli.cmd_annotations
    assert args.playfield == "main-playfield"
    assert args.daemon is None
    assert args.all is False
    assert args.clear is False
    assert args.layer is None


def test_annotations_requires_a_playfield_positional_argument():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["playfield", "annotations"])


def test_annotations_accepts_clear_and_layer_flags():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "annotations", "main-playfield", "--clear", "--layer", "planner"])
    assert args.clear is True
    assert args.layer == "planner"


def test_annotations_accepts_all_at_parse_time_but_cmd_annotations_refuses_it_with_clear():
    """``--all`` is wired onto this subparser (list fans out, per
    ``cli.md``'s fan-out table) -- argparse accepts it unconditionally;
    ``cmd_annotations`` is what refuses the ``--clear --all``
    combination at runtime, exercised above."""
    parser = _build_parser()
    args = parser.parse_args(["playfield", "annotations", "main-playfield", "--clear", "--all"])
    assert args.clear is True
    assert args.all is True


def test_annotations_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "annotations", "main-playfield", "-h"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# list -- the playfield counterpart to `camera list`
# ---------------------------------------------------------------------------


@dataclass
class _FakeDefinition:
    width_cm: float = 134.3
    height_cm: float = 89.3
    _aruco: Tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
    _features: Tuple[object, ...] = tuple(range(25))

    def required_aruco_ids(self) -> Tuple[int, ...]:
        return self._aruco

    def features(self) -> Tuple[object, ...]:
        return self._features


@dataclass
class _FakePlayfield:
    name: str = "main-playfield"
    definition: _FakeDefinition = field(default_factory=_FakeDefinition)
    _cameras: Tuple[str, ...] = ("arducam-ov9782-usb-camera",)

    def linked_cameras(self) -> Tuple[str, ...]:
        return self._cameras


def test_list_prints_each_playfield_with_size_and_derived_camera_links(capsys):
    daemon = _FakeDaemon(list_playfields_returns=(_FakePlayfield(),))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "main-playfield" in out
    assert "134.3 x 89.3 cm" in out
    assert "aruco=10" in out
    assert "features=25" in out
    # the derived link is the one fact not readable from the JSON on disk
    assert "arducam-ov9782-usb-camera" in out


def test_list_with_no_playfields_is_an_empty_result_not_an_error(capsys):
    """A daemon started before any playfield was pushed or installed is
    a normal state (UC-01), exactly as an empty camera list is."""
    discovery = _FakeDiscovery(daemon=_FakeDaemon(list_playfields_returns=()))

    code = playfield_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no playfields)" in capsys.readouterr().out


def test_list_shows_a_dash_when_no_camera_is_linked(capsys):
    daemon = _FakeDaemon(
        list_playfields_returns=(_FakePlayfield(_cameras=()),)
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert "cameras=-" in capsys.readouterr().out


def test_list_passes_the_daemon_flag_through(capsys):
    discovery = _FakeDiscovery(daemon=_FakeDaemon(list_playfields_returns=()))

    playfield_cli.cmd_list(
        _args(daemon="pi-field.local"), discovery_factory=lambda: discovery
    )

    assert discovery.connected_with == "pi-field.local"


def test_list_surfaces_a_connection_failure_as_a_typed_error(capsys):
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("localhost"))

    code = playfield_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code != 0
    assert "localhost" in capsys.readouterr().err


def test_list_fans_out_labelling_each_daemon(capsys):
    daemon = _FakeDaemon(list_playfields_returns=(_FakePlayfield(),))
    discovery = _FakeDiscovery(
        daemon=daemon, infos=(_Info("localhost", is_local=True),)
    )

    code = playfield_cli.cmd_list(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "localhost" in out
    assert "main-playfield" in out


def test_playfield_list_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "list", "-h"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# where -- find_feature (ticket 025-002, UC-08)
# ---------------------------------------------------------------------------


def _feature(
    slug: str, kind: str = "dot", *, colour: Optional[str] = None, x: float = 0.0, y: float = 0.0
) -> Feature:
    return Feature(slug=slug, kind=kind, position=WorldPoint(x=x, y=y), colour=colour)


def test_where_prints_slug_kind_colour_and_position_for_each_match(capsys):
    daemon = _FakeDaemon(
        find_feature_returns=(_feature("dot-west-blue", "dot", colour="blue", x=-50.0, y=0.0),)
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_where(_args(query="the blue dot"), discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.find_feature_requested == ("main-playfield", "the blue dot")
    out = capsys.readouterr().out
    assert "dot-west-blue" in out
    assert "dot" in out
    assert "blue" in out
    assert "-50.00" in out and "0.00" in out


def test_where_ambiguous_query_prints_every_match(capsys):
    daemon = _FakeDaemon(
        find_feature_returns=(
            _feature("dot-northwest-orange", colour="orange", x=-50.0, y=30.0),
            _feature("dot-northeast-orange", colour="orange", x=50.0, y=30.0),
            _feature("dot-southeast-orange", colour="orange", x=50.0, y=-30.0),
            _feature("dot-southwest-orange", colour="orange", x=-50.0, y=-30.0),
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_where(_args(query="the orange dot"), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    for slug in (
        "dot-northwest-orange",
        "dot-northeast-orange",
        "dot-southeast-orange",
        "dot-southwest-orange",
    ):
        assert slug in out


def test_where_no_match_prints_an_explicit_no_matches_not_silent_empty_output(capsys):
    daemon = _FakeDaemon(find_feature_returns=())
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_where(
        _args(query="the orange triangle"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert "(no matches)" in capsys.readouterr().out


def test_where_feature_with_no_colour_prints_a_dash_not_none(capsys):
    daemon = _FakeDaemon(
        find_feature_returns=(_feature("apriltag-center-a1", "april_tag", colour=None),)
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_where(_args(query="tag"), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "apriltag-center-a1" in out
    assert "None" not in out


def test_where_unknown_playfield_surfaces_the_daemon_error(capsys):
    daemon = _FakeDaemon(find_feature_raises=UnknownPlayfield("ghost-field", ("main-playfield",)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_where(
        _args(playfield="ghost-field"), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "ghost-field" in err
    assert "main-playfield" in err


def test_where_passes_the_daemon_flag_through_to_connect():
    daemon = _FakeDaemon(find_feature_returns=())
    discovery = _FakeDiscovery(daemon=daemon)

    playfield_cli.cmd_where(
        _args(daemon="pi-field.local"), discovery_factory=lambda: discovery
    )

    assert discovery.connected_with == "pi-field.local"


def test_where_all_fans_out_and_reports_one_unreachable_daemon(capsys):
    good = _FakeDaemon(find_feature_returns=(_feature("dot-west-blue", colour="blue"),))
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True), _Info(host="unreachable-host", is_local=False)),
        daemons_by_host={
            None: good,
            "unreachable-host": ConnectionFailed("unreachable-host", reason="no answer"),
        },
    )

    code = playfield_cli.cmd_where(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1  # partial failure is visible in the exit code
    out, err = capsys.readouterr()
    assert "localhost (local):" in out
    assert "dot-west-blue" in out
    assert "unreachable-host: unreachable" in err
    assert "no answer" in err


def test_where_all_with_zero_daemons_found_is_success_not_error(capsys):
    discovery = _FakeDiscovery(infos=())

    code = playfield_cli.cmd_where(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no daemons found)" in capsys.readouterr().out


def test_where_all_reports_discovery_failed_distinctly(capsys):
    discovery = _FakeDiscovery(find_all_raises=DiscoveryFailed("multicast blocked"))

    code = playfield_cli.cmd_where(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "multicast blocked" in capsys.readouterr().err


def test_where_is_wired_to_cmd_where_with_playfield_and_query_positionals():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "where", "main-playfield", "the blue dot"])
    assert args.func is playfield_cli.cmd_where
    assert args.playfield == "main-playfield"
    assert args.query == "the blue dot"
    assert args.daemon is None
    assert args.all is False


def test_where_requires_both_positional_arguments():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["playfield", "where", "main-playfield"])


def test_where_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "where", "main-playfield", "the blue dot", "-h"])
    assert excinfo.value.code == 0
