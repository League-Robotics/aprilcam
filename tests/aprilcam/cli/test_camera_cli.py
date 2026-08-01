"""Tests for ``aprilcam.cli.camera`` (ticket 020-006).

Headless: every test drives ``cmd_list``/``cmd_probe``/``cmd_image``
directly against a fake ``Discovery``/``Daemon`` pair injected through
the ``discovery_factory`` keyword seam, matching
``tests/aprilcam/client/test_discovery.py``'s fake-driven style. The
``--all`` fan-out tests use a fake ``Discovery.find_all()`` returning
two entries -- one reachable, one that raises on ``connect`` -- as the
headless stand-in for "a second local daemon instance bound to an
alternate socket path, plus one named -d target that is deliberately
unreachable" (UC-14.S-3/S-4); a genuine two-real-process version lives
in ``test_cli_integration.py``'s ``@pytest.mark.integration`` test.
"""

from __future__ import annotations

import argparse
import base64
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Tuple

import pytest

from aprilcam.cli import camera as camera_cli
from aprilcam.errors import ConnectionFailed, DiscoveryFailed, UnknownCamera
from aprilcam.types import CameraState, ImageFrame, ProbeReport, ProbeSkip


def _camera(name: str = "arducam-ov9782-usb-camera", number: int = 1, **overrides) -> CameraState:
    fields = dict(
        name=name,
        present=True,
        usable=True,
        playfield=None,
        calibrated=False,
        number=number,
    )
    fields.update(overrides)
    return CameraState(**fields)


def _frame(data: bytes = b"\xff\xd8\xff\xd9", encoding: str = "jpeg") -> ImageFrame:
    return ImageFrame(data=data, width=4, height=4, encoding=encoding, timestamp=0.0)


def _args(**overrides) -> argparse.Namespace:
    base = dict(daemon=None, all=False, camera="arducam-ov9782-usb-camera", output=None,
                base64=False, encoding="jpeg", force=False)
    base.update(overrides)
    return argparse.Namespace(**base)


@dataclass
class _FakeDaemon:
    cameras: Tuple[CameraState, ...] = ()
    probe_report: Optional[ProbeReport] = None
    frame: Optional[ImageFrame] = None
    get_frame_raises: Optional[Exception] = None
    requested_camera: Optional[str] = None
    requested_encoding: Optional[str] = None

    def list_cameras(self) -> Tuple[CameraState, ...]:
        return self.cameras

    def probe_cameras(self) -> ProbeReport:
        return self.probe_report if self.probe_report is not None else ProbeReport(cameras=self.cameras)

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        self.requested_camera = camera
        self.requested_encoding = encoding
        if self.get_frame_raises is not None:
            raise self.get_frame_raises
        return self.frame if self.frame is not None else _frame(encoding=encoding)


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


# ---------------------------------------------------------------------------
# camera list -- AC: UC-14.S-1/S-2, UC-25.S-4 (unusable listed, never omitted)
# ---------------------------------------------------------------------------


def test_list_prints_every_camera_with_number_and_flags(capsys):
    cameras = (
        _camera("arducam-ov9782-usb-camera", 1, playfield="main-playfield", calibrated=True),
        _camera("hd-usb-camera", 2),
    )
    discovery = _FakeDiscovery(daemon=_FakeDaemon(cameras=cameras))

    code = camera_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "[1]" in out and "arducam-ov9782-usb-camera" in out
    assert "main-playfield" in out and "calibrated" in out
    assert "[2]" in out and "hd-usb-camera" in out
    assert discovery.connected_with is None  # no -d given -> local


def test_list_shows_present_but_unusable_camera_distinctly_from_absent(capsys):
    """UC-25.S-4: 'plugged in and broken' must read differently from
    'not plugged in' -- both stay in the list, never omitted."""
    broken = _camera("broken-cam", 3, present=True, usable=False)
    gone = _camera("unplugged-cam", 4, present=False, usable=False)
    discovery = _FakeDiscovery(daemon=_FakeDaemon(cameras=(broken, gone)))

    camera_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    out = capsys.readouterr().out
    assert "broken-cam" in out and "present" in out and "unusable" in out
    assert "unplugged-cam" in out and "absent" in out


def test_list_with_no_cameras_prints_explicit_empty_marker(capsys):
    discovery = _FakeDiscovery(daemon=_FakeDaemon(cameras=()))

    code = camera_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no cameras)" in capsys.readouterr().out


def test_list_passes_the_daemon_flag_through_to_connect():
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    camera_cli.cmd_list(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert discovery.connected_with == "pi-field.local"


def test_list_reports_connection_failure_with_typed_message(capsys):
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))

    code = camera_cli.cmd_list(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


# ---------------------------------------------------------------------------
# camera list --all -- AC: UC-14.S-3/S-4 fan-out, one unreachable daemon
# ---------------------------------------------------------------------------


def test_list_all_fans_out_and_reports_one_unreachable_daemon_without_failing(capsys):
    good = _FakeDaemon(cameras=(_camera(),))
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True), _Info(host="unreachable-host", is_local=False)),
        daemons_by_host={
            None: good,  # is_local -> connect(None)
            "unreachable-host": ConnectionFailed("unreachable-host", reason="no answer"),
        },
    )

    code = camera_cli.cmd_list(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1  # partial failure is visible in the exit code
    out, err = capsys.readouterr()
    assert "localhost (local):" in out
    assert "arducam-ov9782-usb-camera" in out
    assert "unreachable-host: unreachable" in err
    assert "no answer" in err


def test_list_all_with_zero_daemons_found_is_success_not_error(capsys):
    discovery = _FakeDiscovery(infos=())

    code = camera_cli.cmd_list(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no daemons found)" in capsys.readouterr().out


def test_list_all_reports_discovery_failed_distinctly(capsys):
    discovery = _FakeDiscovery(find_all_raises=DiscoveryFailed("multicast blocked"))

    code = camera_cli.cmd_list(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "multicast blocked" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# camera probe -- AC: UC-25.S-1/S-2 independent reports, ProbeReport.skipped
# ---------------------------------------------------------------------------


def test_probe_reports_cameras_and_skipped_backends(capsys):
    report = ProbeReport(
        cameras=(_camera(),),
        skipped=(ProbeSkip(backend="hd-usb-camera", reason="in use by a live subscriber"),),
    )
    discovery = _FakeDiscovery(daemon=_FakeDaemon(probe_report=report))

    code = camera_cli.cmd_probe(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "arducam-ov9782-usb-camera" in out
    assert "skipped:" in out
    assert "hd-usb-camera: in use by a live subscriber" in out


def test_probe_with_a_complete_probe_shows_no_skipped_section(capsys):
    discovery = _FakeDiscovery(daemon=_FakeDaemon(probe_report=ProbeReport(cameras=(_camera(),))))

    camera_cli.cmd_probe(_args(), discovery_factory=lambda: discovery)

    assert "skipped" not in capsys.readouterr().out


def test_probe_all_fans_out(capsys):
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True),),
        daemons_by_host={None: _FakeDaemon(probe_report=ProbeReport(cameras=(_camera(),)))},
    )

    code = camera_cli.cmd_probe(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert "localhost (local):" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# camera image -- AC: UC-17.S-1..S-4
# ---------------------------------------------------------------------------


def test_image_writes_png_to_a_file_with_the_requested_encoding(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\nrest-of-file"
    daemon = _FakeDaemon(frame=_frame(data=png_bytes, encoding="png"))
    discovery = _FakeDiscovery(daemon=daemon)
    out_path = tmp_path / "frame.png"

    code = camera_cli.cmd_image(
        _args(output=str(out_path), encoding="png"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert out_path.read_bytes() == png_bytes
    assert daemon.requested_encoding == "png"


def test_image_default_streams_raw_bytes_to_a_non_tty_stdout(capsysbinary, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    daemon = _FakeDaemon(frame=_frame(data=b"raw-jpeg-bytes"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_image(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert capsysbinary.readouterr().out == b"raw-jpeg-bytes"


def test_image_base64_mode_writes_encoded_text_to_stdout(capsys):
    daemon = _FakeDaemon(frame=_frame(data=b"raw-bytes"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_image(_args(base64=True), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out.strip()
    assert base64.b64decode(out) == b"raw-bytes"


def test_image_raw_mode_refuses_an_attached_terminal_without_force(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_image(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "terminal" in err
    assert daemon.requested_camera is None  # refused before any capture RPC


def test_image_raw_mode_with_force_writes_to_an_attached_terminal(capsysbinary, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    daemon = _FakeDaemon(frame=_frame(data=b"forced-bytes"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_image(_args(force=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert capsysbinary.readouterr().out == b"forced-bytes"


def test_image_unwritable_output_directory_fails_before_any_capture(capsys, tmp_path):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)
    bad_path = tmp_path / "does-not-exist" / "frame.jpg"

    code = camera_cli.cmd_image(
        _args(output=str(bad_path)), discovery_factory=lambda: discovery
    )

    assert code == 1
    assert "directory does not exist" in capsys.readouterr().err
    assert daemon.requested_camera is None


def test_image_unknown_camera_surfaces_the_typed_error_with_available_list(capsys):
    daemon = _FakeDaemon(get_frame_raises=UnknownCamera("no-such-cam", ("arducam-ov9782-usb-camera", "hd-usb-camera")))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_image(
        _args(camera="no-such-cam", base64=True), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "no-such-cam" in err
    assert "arducam-ov9782-usb-camera" in err and "hd-usb-camera" in err


# ---------------------------------------------------------------------------
# camera image --all -- AC: base64-only fan-out, file/raw exceptions documented
# ---------------------------------------------------------------------------


def test_image_all_refuses_a_single_file_destination(capsys, tmp_path):
    discovery = _FakeDiscovery()

    code = camera_cli.cmd_image(
        _args(all=True, output=str(tmp_path / "frame.jpg")), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "--all" in err and "-o" in err and "--base64" in err


def test_image_all_refuses_raw_bytes_to_stdout(capsys):
    discovery = _FakeDiscovery()

    code = camera_cli.cmd_image(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "--base64" in capsys.readouterr().err


def test_image_all_base64_fans_out_labelled_lines(capsys):
    good = _FakeDaemon(frame=_frame(data=b"one-daemons-bytes"))
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True), _Info(host="unreachable-host", is_local=False)),
        daemons_by_host={
            None: good,
            "unreachable-host": ConnectionFailed("unreachable-host", reason="down"),
        },
    )

    code = camera_cli.cmd_image(
        _args(all=True, base64=True), discovery_factory=lambda: discovery
    )

    assert code == 1  # one daemon unreachable -> visible in the exit code
    out, err = capsys.readouterr()
    assert "localhost (local): " in out
    assert base64.b64decode(out.split(": ", 1)[1].strip()) == b"one-daemons-bytes"
    assert "unreachable-host: unreachable" in err


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="aprilcam")
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    camera_cli.add_subparser(subparsers)
    return top


def test_camera_alone_prints_help_and_fails(capsys):
    parser = _build_parser()
    args = parser.parse_args(["camera"])

    code = args.func(args)

    assert code == 1
    assert "usage: aprilcam camera" in capsys.readouterr().out


def test_image_rejects_an_unsupported_encoding_at_parse_time():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "image", "some-camera", "--encoding", "bmp"])


def test_image_output_and_base64_are_mutually_exclusive():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "image", "some-camera", "-o", "f.jpg", "--base64"])


def test_camera_list_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "list", "-h"])
    assert excinfo.value.code == 0
