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
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Tuple

import pytest

from aprilcam.cli import camera as camera_cli
from aprilcam.errors import CameraNotPresent, ConnectionFailed, DiscoveryFailed, UnknownCamera
from aprilcam.types import (
    CameraState,
    ImageFrame,
    PixelPoint,
    ProbeReport,
    ProbeSkip,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    WorldPoint,
)
from aprilcam.view.viewer import FirstFrameTimeout, NoDisplayAvailable


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
                base64=False, encoding="jpeg", force=False, reset=False, dump=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _uncalibrated_record(
    family: TagFamily = TagFamily.APRILTAG, number: int = 7, x: float = 123.4, y: float = 56.7
) -> TagRecord:
    """A record shaped exactly as ``daemon/detection.py`` produces this
    sprint (sprint.md: 'every tag record from this sprint reports
    calibrated=False, world=None' -- see the module docstring)."""
    return TagRecord(
        tag=TagId(family, number),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        pixel=PixelPoint(x, y),
        calibrated=False,
    )


def _calibrated_record(
    family: TagFamily = TagFamily.APRILTAG,
    number: int = 1,
    wx: float = 10.0,
    wy: float = -5.0,
    heading_rad: Optional[float] = 0.5,
    speed: Optional[float] = 12.5,
    yaw_rad: Optional[float] = 0.2,
) -> TagRecord:
    """A record shaped as a real calibrated camera produces this sprint
    (ticket 022-003 onward) -- exercises ``_format_tag_row``'s
    calibrated branch. ``heading_rad``/``speed``/``yaw_rad`` are
    overridable so tests can shape the "stationary tag" case (ticket
    022-007): ``heading_rad=None`` with a real ``speed``/``yaw_rad``,
    exactly what ``vision.motion.compute_motion`` now reports for a tag
    whose two most recent samples are within
    ``MIN_RELIABLE_DISPLACEMENT_CM`` of each other."""
    return TagRecord(
        tag=TagId(family, number),
        world=WorldPoint(wx, wy),
        heading_rad=heading_rad,
        world_velocity=None,
        speed=speed,
        timestamp=0.0,
        pixel=PixelPoint(1.0, 2.0),
        calibrated=True,
        yaw_rad=yaw_rad,
    )


def _tag_frame(tags: Tuple[TagRecord, ...] = ()) -> TagFrame:
    return TagFrame(tags=tags, timestamp=0.0, frame_index=0)


@dataclass
class _FakeDaemon:
    cameras: Tuple[CameraState, ...] = ()
    probe_report: Optional[ProbeReport] = None
    frame: Optional[ImageFrame] = None
    get_frame_raises: Optional[Exception] = None
    requested_camera: Optional[str] = None
    requested_encoding: Optional[str] = None
    tag_frame: Optional[TagFrame] = None
    get_tags_raises: Optional[Exception] = None
    requested_tags_camera: Optional[str] = None
    set_camera_config_raises: Optional[Exception] = None
    pushed_camera_config: Optional[object] = None
    reset_camera_config_raises: Optional[Exception] = None
    reset_requested_camera: Optional[str] = None
    get_camera_config_returns: Optional[object] = None
    get_camera_config_raises: Optional[Exception] = None
    get_camera_config_requested: Optional[str] = None

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

    def get_tags(self, camera: str) -> TagFrame:
        self.requested_tags_camera = camera
        if self.get_tags_raises is not None:
            raise self.get_tags_raises
        return self.tag_frame if self.tag_frame is not None else _tag_frame()

    def set_camera_config(self, config) -> None:
        if self.set_camera_config_raises is not None:
            raise self.set_camera_config_raises
        self.pushed_camera_config = config

    def reset_camera_config(self, camera: str) -> None:
        self.reset_requested_camera = camera
        if self.reset_camera_config_raises is not None:
            raise self.reset_camera_config_raises

    def get_camera_config(self, camera: str):
        self.get_camera_config_requested = camera
        if self.get_camera_config_raises is not None:
            raise self.get_camera_config_raises
        assert self.get_camera_config_returns is not None, "test forgot to set get_camera_config_returns"
        return self.get_camera_config_returns


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
# camera tags -- AC: UC-20.S-1/S-2/S-3, ticket 021-006
# ---------------------------------------------------------------------------


def test_tags_prints_uncalibrated_flag_and_pixel_never_a_world_looking_number(capsys):
    """UC-20.S-3, taken literally: never ``0.0, 0.0`` or any
    world-looking number for an uncalibrated record -- the flag and
    the pixel position instead."""
    record = _uncalibrated_record(x=123.4, y=56.7)
    daemon = _FakeDaemon(tag_frame=_tag_frame((record,)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_tags(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "apriltag" in out and "7" in out
    assert "UNCALIBRATED" in out
    assert "123.4" in out and "56.7" in out
    assert "world=" not in out
    assert "0.0, 0.0" not in out
    assert daemon.requested_tags_camera == "arducam-ov9782-usb-camera"


def test_tags_distinguishes_apriltag_and_aruco_of_the_same_number(capsys):
    """UC-09.S-3's disambiguation, exercised through the CLI: family
    plus id, never id alone."""
    apriltag_7 = _uncalibrated_record(family=TagFamily.APRILTAG, number=7)
    aruco_7 = _uncalibrated_record(family=TagFamily.ARUCO, number=7)
    daemon = _FakeDaemon(tag_frame=_tag_frame((apriltag_7, aruco_7)))
    discovery = _FakeDiscovery(daemon=daemon)

    camera_cli.cmd_tags(_args(), discovery_factory=lambda: discovery)

    out = capsys.readouterr().out
    assert "apriltag 7" in out
    assert "aruco 7" in out


def test_tags_calibrated_record_prints_world_position_not_the_uncalibrated_flag(capsys):
    """The calibrated branch, exercised against a real daemon since
    ticket 022-003 activated calibration."""
    record = _calibrated_record(wx=10.0, wy=-5.0)
    daemon = _FakeDaemon(tag_frame=_tag_frame((record,)))
    discovery = _FakeDiscovery(daemon=daemon)

    camera_cli.cmd_tags(_args(), discovery_factory=lambda: discovery)

    out = capsys.readouterr().out
    assert "UNCALIBRATED" not in out
    assert "world=(10.0, -5.0)" in out


def test_tags_calibrated_record_prints_yaw_ahead_of_heading_and_speed(capsys):
    """Ticket 022-007 acceptance criterion: the CLI shows ``yaw_rad``
    (the tag's own facing direction) alongside ``heading_rad`` (motion
    direction) -- previously ``yaw`` was silently absent from
    ``camera tags`` output entirely, even though ``TagRecord`` already
    carried it (ticket 022-004)."""
    record = _calibrated_record(wx=10.0, wy=-5.0, yaw_rad=0.2, heading_rad=0.5, speed=12.5)
    daemon = _FakeDaemon(tag_frame=_tag_frame((record,)))
    discovery = _FakeDiscovery(daemon=daemon)

    camera_cli.cmd_tags(_args(), discovery_factory=lambda: discovery)

    out = capsys.readouterr().out
    assert "yaw=0.200" in out
    assert "heading=0.500" in out
    assert "speed=12.50" in out
    assert out.index("yaw=") < out.index("heading="), "yaw must print ahead of heading"


def test_tags_stationary_tag_shows_undefined_heading_but_a_real_yaw_and_speed(capsys):
    """Ticket 022-007 defect B, end to end through the CLI: a tag whose
    two most recent samples are within
    ``vision.motion.MIN_RELIABLE_DISPLACEMENT_CM`` of each other now
    carries ``heading_rad=None`` (undefined -- not a confident-looking
    noise-driven angle) from ``vision.motion.compute_motion``, while
    ``yaw_rad`` (orientation, not motion -- unaffected by this fix) and
    ``speed`` (honestly near-zero, not suppressed) stay populated. The
    CLI must render that distinction, not collapse it: a dash for the
    undefined heading, real numbers for yaw and speed."""
    record = _calibrated_record(
        wx=-67.1, wy=0.0, heading_rad=None, speed=0.0004, yaw_rad=-1.633
    )
    daemon = _FakeDaemon(tag_frame=_tag_frame((record,)))
    discovery = _FakeDiscovery(daemon=daemon)

    camera_cli.cmd_tags(_args(), discovery_factory=lambda: discovery)

    out = capsys.readouterr().out
    assert "yaw=-1.633" in out
    assert "heading=-" in out
    assert "speed=0.00" in out


def test_tags_with_nothing_detected_prints_an_empty_result_not_an_error(capsys):
    """UC-20's failure mode: nothing detected is an empty result, never
    an error or a stack trace."""
    daemon = _FakeDaemon(tag_frame=_tag_frame(()))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_tags(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out, err = capsys.readouterr()
    assert "(no tags detected)" in out
    assert err == ""


def test_tags_passes_the_daemon_flag_through_to_connect():
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    camera_cli.cmd_tags(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert discovery.connected_with == "pi-field.local"


def test_tags_unknown_camera_surfaces_the_typed_error_with_available_list(capsys):
    daemon = _FakeDaemon(
        get_tags_raises=UnknownCamera("no-such-cam", ("1 arducam-ov9782-usb-camera", "2 hd-usb-camera"))
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_tags(
        _args(camera="no-such-cam"), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "no-such-cam" in err
    assert "1 arducam-ov9782-usb-camera" in err and "2 hd-usb-camera" in err


def test_tags_camera_not_present_surfaces_a_distinct_typed_error(capsys):
    """``CameraNotPresent`` is distinct from ``UnknownCamera`` -- a
    registered-but-unplugged camera reads as 'not present', never
    'unknown camera' (ticket 021-002/005 handoff)."""
    daemon = _FakeDaemon(get_tags_raises=CameraNotPresent("arducam-ov9782-usb-camera"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_tags(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "not currently present" in err


# ---------------------------------------------------------------------------
# camera tags --all -- AC: fans out like camera list/probe, per-daemon labels
# ---------------------------------------------------------------------------


def test_tags_all_fans_out_and_reports_one_unreachable_daemon_without_failing(capsys):
    good = _FakeDaemon(tag_frame=_tag_frame((_uncalibrated_record(),)))
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True), _Info(host="unreachable-host", is_local=False)),
        daemons_by_host={
            None: good,
            "unreachable-host": ConnectionFailed("unreachable-host", reason="no answer"),
        },
    )

    code = camera_cli.cmd_tags(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1  # partial failure is visible in the exit code
    out, err = capsys.readouterr()
    assert "localhost (local):" in out
    assert "apriltag 7" in out
    assert "unreachable-host: unreachable" in err
    assert "no answer" in err


def test_tags_all_with_zero_daemons_found_is_success_not_error(capsys):
    discovery = _FakeDiscovery(infos=())

    code = camera_cli.cmd_tags(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no daemons found)" in capsys.readouterr().out


def test_tags_all_reports_discovery_failed_distinctly(capsys):
    discovery = _FakeDiscovery(find_all_raises=DiscoveryFailed("multicast blocked"))

    code = camera_cli.cmd_tags(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "multicast blocked" in capsys.readouterr().err


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


def test_tags_requires_a_camera_positional_argument():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "tags"])


def test_tags_is_wired_to_cmd_tags():
    parser = _build_parser()
    args = parser.parse_args(["camera", "tags", "arducam-ov9782-usb-camera"])
    assert args.func is camera_cli.cmd_tags
    assert args.camera == "arducam-ov9782-usb-camera"
    assert args.all is False
    assert args.daemon is None


def test_tags_accepts_a_persistent_number_as_the_camera_token():
    """cli.md's number-or-slug convention: an all-digits token is a
    valid camera argument at the argparse level -- resolution happens
    daemon-side (ticket 021-002)."""
    parser = _build_parser()
    args = parser.parse_args(["camera", "tags", "3"])
    assert args.camera == "3"


def test_tags_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "tags", "some-camera", "-h"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# camera config -- AC: ticket 022-001, push-only, playfield-link slice
# ---------------------------------------------------------------------------


class _FakeCameraConfigLoader:
    """A ``ConfigLoader``-shaped double: returns a pre-built
    ``CameraConfig`` (or raises) instead of touching a real file --
    ``cmd_config`` is tested against this seam the same way every other
    command here is tested against a fake ``Discovery``."""

    def __init__(self, config=None, raises: Optional[Exception] = None):
        self._config = config
        self._raises = raises
        self.requested_path = None

    def load_camera_config(self, path):
        self.requested_path = path
        if self._raises is not None:
            raise self._raises
        return self._config

    def load_playfield(self, path):  # pragma: no cover - not exercised here
        raise NotImplementedError


def test_config_pushes_the_loaded_document_and_reports_the_playfield(capsys, tmp_path):
    from aprilcam.config.interfaces import CameraConfig

    config = CameraConfig(
        name="arducam-ov9782-usb-camera", playfield="main-playfield", settings={}
    )
    loader = _FakeCameraConfigLoader(config=config)
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)
    file_path = tmp_path / "camera.json"

    code = camera_cli.cmd_config(
        _args(file=str(file_path)),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 0
    assert loader.requested_path == Path(file_path)
    assert daemon.pushed_camera_config == config
    out = capsys.readouterr().out
    assert "arducam-ov9782-usb-camera" in out
    assert "main-playfield" in out


def test_config_reports_no_playfield_when_the_document_carries_none(capsys, tmp_path):
    from aprilcam.config.interfaces import CameraConfig

    config = CameraConfig(name="hd-usb-camera", playfield=None, settings={})
    loader = _FakeCameraConfigLoader(config=config)
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    code = camera_cli.cmd_config(
        _args(file="camera.json"), discovery_factory=lambda: discovery, loader_factory=lambda: loader
    )

    assert code == 0
    assert "(none)" in capsys.readouterr().out


def test_config_malformed_document_is_rejected_before_any_rpc(capsys, tmp_path):
    from aprilcam.errors import MalformedDocument

    loader = _FakeCameraConfigLoader(raises=MalformedDocument("no 'name' field"))
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_config(
        _args(file="bad.json"), discovery_factory=lambda: discovery, loader_factory=lambda: loader
    )

    assert code == 1
    assert "no 'name' field" in capsys.readouterr().err
    assert daemon.pushed_camera_config is None  # never reached the daemon


def test_config_reports_a_daemon_side_rejection(capsys, tmp_path):
    from aprilcam.config.interfaces import CameraConfig
    from aprilcam.errors import UnknownCamera

    config = CameraConfig(name="unknown-cam", playfield=None, settings={})
    loader = _FakeCameraConfigLoader(config=config)
    daemon = _FakeDaemon(set_camera_config_raises=UnknownCamera("unknown-cam", ("hd-usb-camera",)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_config(
        _args(file="camera.json"), discovery_factory=lambda: discovery, loader_factory=lambda: loader
    )

    assert code == 1
    assert "unknown-cam" in capsys.readouterr().err


def test_config_is_wired_to_cmd_config():
    parser = _build_parser()
    args = parser.parse_args(["camera", "config", "camera.json"])
    assert args.func is camera_cli.cmd_config
    assert args.file == "camera.json"
    assert args.daemon is None
    assert args.reset is False
    assert args.dump is False


def test_config_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "config", "camera.json", "-h"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# camera config --reset -- AC: SUC-003, ticket 023-004
# ---------------------------------------------------------------------------


def test_config_reset_flag_is_wired():
    parser = _build_parser()
    args = parser.parse_args(["camera", "config", "arducam-ov9782-usb-camera", "--reset"])
    assert args.func is camera_cli.cmd_config
    assert args.file == "arducam-ov9782-usb-camera"
    assert args.reset is True


def test_config_reset_calls_reset_camera_config_with_the_positional_as_camera_name(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)
    loader = _FakeCameraConfigLoader()  # never touched -- --reset skips the loader entirely

    code = camera_cli.cmd_config(
        _args(file="arducam-ov9782-usb-camera", reset=True),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 0
    assert daemon.reset_requested_camera == "arducam-ov9782-usb-camera"
    assert loader.requested_path is None  # no file was ever loaded client-side
    assert "arducam-ov9782-usb-camera" in capsys.readouterr().out


def test_config_reset_reports_a_daemon_side_rejection(capsys):
    daemon = _FakeDaemon(
        reset_camera_config_raises=UnknownCamera("never-registered", ("hd-usb-camera",))
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_config(
        _args(file="never-registered", reset=True), discovery_factory=lambda: discovery
    )

    assert code == 1
    assert "never-registered" in capsys.readouterr().err


def test_config_reset_passes_the_daemon_flag_through_to_connect():
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    camera_cli.cmd_config(
        _args(file="arducam-ov9782-usb-camera", reset=True, daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
    )

    assert discovery.connected_with == "pi-field.local"


# ---------------------------------------------------------------------------
# camera config --dump -- AC: UC-24, ticket 023-005
# ---------------------------------------------------------------------------


def test_config_dump_flag_is_wired():
    parser = _build_parser()
    args = parser.parse_args(["camera", "config", "arducam-ov9782-usb-camera", "--dump"])
    assert args.func is camera_cli.cmd_config
    assert args.file == "arducam-ov9782-usb-camera"
    assert args.dump is True
    assert args.reset is False


def test_config_dump_and_reset_are_mutually_exclusive_at_parse_time():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["camera", "config", "arducam-ov9782-usb-camera", "--dump", "--reset"]
        )


def test_config_dump_prints_the_live_document_in_exact_on_disk_json(capsys):
    from aprilcam.config.interfaces import CameraConfig

    config = CameraConfig(
        name="arducam-ov9782-usb-camera",
        playfield="main-playfield",
        settings={"exposure": 6, "gain": 2},
    )
    daemon = _FakeDaemon(get_camera_config_returns=config)
    discovery = _FakeDiscovery(daemon=daemon)
    loader = _FakeCameraConfigLoader()  # never touched -- --dump skips the loader entirely

    code = camera_cli.cmd_config(
        _args(file="arducam-ov9782-usb-camera", dump=True),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 0
    assert daemon.get_camera_config_requested == "arducam-ov9782-usb-camera"
    assert loader.requested_path is None  # no file was ever loaded client-side
    printed = json.loads(capsys.readouterr().out)
    assert printed == config.to_dict()
    assert printed["name"] == "arducam-ov9782-usb-camera"  # ConfigDocument.to_dict's contract


def test_config_dump_round_trips_through_config_loader(capsys, tmp_path):
    """The binding contract ``config/interfaces.py`` states for
    ``ConfigLoader``: ``load(x.to_dict()) == x``. Dumped output, written
    to a file and re-loaded through the real ``ConfigLoader`` (not the
    fake used elsewhere in this file), reproduces an identical
    document -- UC-24.S-1's round trip, exercised headlessly against a
    fake ``Daemon``."""
    from aprilcam.config.interfaces import CameraConfig
    from aprilcam.config.loader import ConfigLoader as JsonConfigLoader

    config = CameraConfig(
        name="arducam-ov9782-usb-camera",
        playfield="main-playfield",
        settings={"exposure": 6, "gain": 2, "auto_exposure": 1},
    )
    daemon = _FakeDaemon(get_camera_config_returns=config)
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_config(
        _args(file="arducam-ov9782-usb-camera", dump=True), discovery_factory=lambda: discovery
    )
    assert code == 0

    dumped_path = tmp_path / "dumped.json"
    dumped_path.write_text(capsys.readouterr().out)
    reloaded = JsonConfigLoader().load_camera_config(dumped_path)

    assert reloaded == config


def test_config_dump_reports_a_daemon_side_rejection(capsys):
    daemon = _FakeDaemon(
        get_camera_config_raises=UnknownCamera("never-configured", ("hd-usb-camera",))
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = camera_cli.cmd_config(
        _args(file="never-configured", dump=True), discovery_factory=lambda: discovery
    )

    assert code == 1
    assert "never-configured" in capsys.readouterr().err


def test_config_dump_passes_the_daemon_flag_through_to_connect():
    from aprilcam.config.interfaces import CameraConfig

    config = CameraConfig(name="arducam-ov9782-usb-camera", playfield=None, settings={})
    daemon = _FakeDaemon(get_camera_config_returns=config)
    discovery = _FakeDiscovery(daemon=daemon)

    camera_cli.cmd_config(
        _args(file="arducam-ov9782-usb-camera", dump=True, daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
    )

    assert discovery.connected_with == "pi-field.local"


def test_config_dump_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "config", "camera.json", "--dump", "-h"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# camera view -- AC: UC-16.S-1/S-3, ticket 022-002. Headless throughout:
# ``run_view`` is injected exactly like ``discovery_factory`` elsewhere in
# this file, so none of these tests ever touch ``view/viewer.py``'s real
# poll loop or GUI toolkit -- that machinery has its own headless tests in
# ``tests/aprilcam/view/test_viewer.py``.
# ---------------------------------------------------------------------------


def test_view_refuses_all_before_ever_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect when --all is refused up front")

    code = camera_cli.cmd_view(_args(all=True), discovery_factory=lambda: _ExplodingDiscovery())

    assert code == 1
    err = capsys.readouterr().err
    assert "--all" in err
    assert "single" in err.lower()


def test_view_prints_a_raw_signposting_line_before_connecting(capsys):
    """Sprint 031 ticket 004 (SUC-004): the startup line names the view
    raw and points at ``playfield view`` as the deskewed alternative,
    printed before any daemon connection is attempted."""
    calls = []

    class _OrderRecordingDiscovery:
        def connect(self, host=None):
            calls.append("connect")
            return _FakeDaemon()

    code = camera_cli.cmd_view(
        _args(camera="arducam-ov9782-usb-camera"),
        discovery_factory=lambda: _OrderRecordingDiscovery(),
        run_view=lambda camera, connected: calls.append("run_view"),
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "raw" in out.lower()
    assert "playfield view" in out
    assert "arducam-ov9782-usb-camera" in out
    assert calls == ["connect", "run_view"]


def test_view_prints_the_raw_signposting_line_even_when_connection_fails(capsys):
    """The startup line must appear even if the subsequent connection
    fails -- an operator who saw the raw-ness warning and then hit a
    connection error still saw the warning."""
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))

    code = camera_cli.cmd_view(
        _args(camera="arducam-ov9782-usb-camera", daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "raw" in out.lower()
    assert "playfield view" in out


def test_view_does_not_print_the_raw_signposting_line_when_all_is_refused(capsys):
    """``--all`` is refused before the view is ever attempted, so the
    raw-ness signpost (which only makes sense for a view that is about
    to open) should not print either."""
    code = camera_cli.cmd_view(_args(all=True), discovery_factory=lambda: _FakeDiscovery())

    assert code == 1
    out = capsys.readouterr().out
    assert "playfield view" not in out


def test_view_connects_and_hands_the_connected_daemon_to_run_view():
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)
    calls = []

    code = camera_cli.cmd_view(
        _args(camera="arducam-ov9782-usb-camera"),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: calls.append((camera, connected)),
    )

    assert code == 0
    assert calls == [("arducam-ov9782-usb-camera", daemon)]


def test_view_passes_the_daemon_flag_through_to_connect():
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    camera_cli.cmd_view(
        _args(daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: None,
    )

    assert discovery.connected_with == "pi-field.local"


def test_view_reports_a_connection_failure_without_ever_calling_run_view(capsys):
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))
    calls = []

    code = camera_cli.cmd_view(
        _args(daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
        run_view=lambda camera, connected: calls.append((camera, connected)),
    )

    assert code == 1
    assert calls == []
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


def test_view_no_display_points_at_camera_image_instead_of_a_backtrace(capsys):
    """UC-16.S-3: a clear message naming ``camera image``, not a
    toolkit backtrace."""
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    def raising_run_view(camera, connected):
        raise NoDisplayAvailable("no $DISPLAY or $WAYLAND_DISPLAY set")

    code = camera_cli.cmd_view(
        _args(camera="arducam-ov9782-usb-camera"),
        discovery_factory=lambda: discovery,
        run_view=raising_run_view,
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "camera image" in err
    assert "arducam-ov9782-usb-camera" in err


def test_view_first_frame_timeout_names_the_camera_and_suggests_a_remedy(capsys):
    """Sprint 038, SUC-004: ``cmd_view`` catches
    ``view.viewer.FirstFrameTimeout`` (raised when no frame ever
    arrives, instead of the process hanging forever) and prints a
    diagnostic naming the camera and the likely cause/remedy (a wedged
    capture session -- ``camera probe``/daemon restart), returning
    ``EXIT_ERROR`` rather than letting the exception escape unhandled."""
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    def raising_run_view(camera, connected):
        raise FirstFrameTimeout(camera)

    code = camera_cli.cmd_view(
        _args(camera="wedged-camera"),
        discovery_factory=lambda: discovery,
        run_view=raising_run_view,
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "wedged-camera" in err
    assert "no frame" in err.lower()
    assert "probe" in err.lower() or "restart" in err.lower()


def test_view_surfaces_a_typed_error_raised_mid_view(capsys):
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    def raising_run_view(camera, connected):
        raise CameraNotPresent(camera)

    code = camera_cli.cmd_view(
        _args(camera="hd-usb-camera"),
        discovery_factory=lambda: discovery,
        run_view=raising_run_view,
    )

    assert code == 1
    assert "not currently present" in capsys.readouterr().err


def test_view_is_wired_to_cmd_view():
    parser = _build_parser()
    args = parser.parse_args(["camera", "view", "arducam-ov9782-usb-camera"])
    assert args.func is camera_cli.cmd_view
    assert args.camera == "arducam-ov9782-usb-camera"
    assert args.all is False
    assert args.daemon is None


def test_view_requires_a_camera_positional_argument():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "view"])


def test_view_accepts_a_persistent_number_as_the_camera_token():
    parser = _build_parser()
    args = parser.parse_args(["camera", "view", "3"])
    assert args.camera == "3"


def test_view_accepts_all_at_parse_time_but_cmd_view_refuses_it_at_runtime():
    """``--all`` is wired onto this subparser (so ``-h`` documents it
    and the refusal message can explain itself, matching ``camera
    image --all``'s pattern) -- argparse accepts it; ``cmd_view`` is
    what refuses it, exercised above."""
    parser = _build_parser()
    args = parser.parse_args(["camera", "view", "some-camera", "--all"])
    assert args.all is True


def test_view_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "view", "some-camera", "-h"])
    assert excinfo.value.code == 0
