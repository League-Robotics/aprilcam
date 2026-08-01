"""Tests for ``aprilcam.cli.playfield`` (ticket 022-001; ``cmd_image``
added by ticket 022-005; ``cmd_view`` added by ticket 022-006).

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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

import pytest

from aprilcam.cli import playfield as playfield_cli
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.errors import CameraNotPresent, MalformedDocument, NotCalibrated, UnknownPlayfield
from aprilcam.types import CameraState, ImageFrame
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

    def set_playfield(self, definition) -> None:
        if self.set_playfield_raises is not None:
            raise self.set_playfield_raises
        self.pushed = definition

    def list_cameras(self) -> Tuple[CameraState, ...]:
        return self.cameras

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        self.requested_camera = camera
        self.requested_encoding = encoding
        if self.get_frame_raises is not None:
            raise self.get_frame_raises
        return self.frame if self.frame is not None else _frame()


@dataclass
class _FakeDiscovery:
    daemon: Optional[_FakeDaemon] = None
    connect_raises: Optional[Exception] = None
    connected_with: Optional[str] = None

    def connect(self, host: Optional[str] = None):
        self.connected_with = host
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.daemon


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
