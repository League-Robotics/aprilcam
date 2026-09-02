"""Tests for ``aprilcam.cli.flatfield`` (sprint 037 ticket 004).

Headless, matching ``test_calibrate_cli.py``'s established style: a
fake ``Daemon``/``Discovery`` pair injected through ``cmd_flatfield``'s
keyword seams -- no real daemon, no vision. ``cmd_flatfield`` is a pure
RPC orchestrator (module docstring), so every test here is about argv
shaping and result/error presentation, never about flat-field math
itself (``tests/aprilcam/calibration/`` and ``tests/aprilcam/daemon/
test_flatfield_rpc.py`` cover that).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Optional, Tuple

import pytest

from aprilcam.calibration.interfaces import FlatFieldResult
from aprilcam.cli import camera as camera_cli
from aprilcam.cli import daemon as daemon_cli
from aprilcam.cli import flatfield as flatfield_cli
from aprilcam.cli import playfield as playfield_cli
from aprilcam.errors import ConnectionFailed, NoFieldMask


def _result(
    succeeded: bool = True,
    spread_before: Tuple[float, ...] = (1.55, 1.40, 1.33),
    spread_after: Tuple[float, ...] = (1.03, 1.02, 1.01),
    reason: Optional[str] = None,
    forced_checks: Tuple[str, ...] = (),
) -> FlatFieldResult:
    return FlatFieldResult(
        succeeded=succeeded,
        spread_before=spread_before,
        spread_after=spread_after,
        reason=reason,
        forced_checks=forced_checks,
        gain=None,
    )


@dataclass
class _FakeDaemon:
    result: Optional[FlatFieldResult] = None
    capture_raises: Optional[Exception] = None
    clear_raises: Optional[Exception] = None
    requested_camera: Optional[str] = None
    requested_kwargs: dict = field(default_factory=dict)
    cleared_camera: Optional[str] = None

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
        self.requested_kwargs = dict(exposure=exposure, frames=frames, force=force, dry_run=dry_run)
        if self.capture_raises is not None:
            raise self.capture_raises
        return self.result if self.result is not None else _result()

    def clear_flat_field(self, camera: str) -> None:
        self.cleared_camera = camera
        if self.clear_raises is not None:
            raise self.clear_raises


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
        all=False,
        camera="arducam-ov9782-usb-camera",
        exposure=None,
        frames=None,
        force=False,
        dry_run=False,
        clear=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# --all refusal (mirrors calibrate's UC-15.S-4 fan-out rule)
# ---------------------------------------------------------------------------


def test_flatfield_refuses_all_before_ever_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect when --all is refused up front")

    code = flatfield_cli.cmd_flatfield(
        _args(all=True), discovery_factory=lambda: _ExplodingDiscovery()
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "--all" in err
    assert "single" in err.lower()


# ---------------------------------------------------------------------------
# --clear combined with a capture-only flag: refused client-side, no RPC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        dict(clear=True, exposure=2.0),
        dict(clear=True, frames=8),
        dict(clear=True, dry_run=True),
        dict(clear=True, force=True),
    ],
)
def test_flatfield_refuses_clear_combined_with_a_capture_only_flag_before_any_rpc(capsys, overrides):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect when --clear conflict is refused up front")

    code = flatfield_cli.cmd_flatfield(
        _args(**overrides), discovery_factory=lambda: _ExplodingDiscovery()
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "--clear" in err


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_flatfield_success_reports_spread_before_and_after_per_channel(capsys):
    daemon = _FakeDaemon(result=_result())
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(
        _args(camera="arducam-ov9782-usb-camera"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    out = capsys.readouterr().out
    assert "succeeded" in out
    assert "arducam-ov9782-usb-camera" in out
    assert "1.550" in out
    assert "1.030" in out


def test_flatfield_passes_the_daemon_flag_through_to_connect():
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    flatfield_cli.cmd_flatfield(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert discovery.connected_with == "pi-field.local"


def test_flatfield_passes_exposure_and_frames_through_untouched():
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    flatfield_cli.cmd_flatfield(
        _args(exposure=-4.0, frames=16), discovery_factory=lambda: discovery
    )

    assert daemon.requested_kwargs == dict(exposure=-4.0, frames=16, force=False, dry_run=False)


def test_flatfield_force_true_reports_forced_checks(capsys):
    daemon = _FakeDaemon(
        result=_result(
            succeeded=True,
            reason="max_spread_after exceeded: 1.90 > 1.20 (forced)",
            forced_checks=("max_spread_after",),
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(_args(force=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.requested_kwargs["force"] is True
    out = capsys.readouterr().out
    assert "max_spread_after" in out
    assert "forced" in out.lower()


# ---------------------------------------------------------------------------
# --dry-run: clearly labelled as evaluated-but-not-persisted
# ---------------------------------------------------------------------------


def test_flatfield_dry_run_success_labels_output_as_not_persisted(capsys):
    daemon = _FakeDaemon(result=_result(succeeded=True))
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(_args(dry_run=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.requested_kwargs["dry_run"] is True
    out = capsys.readouterr().out
    assert "not persisted" in out.lower()
    assert "dry-run" in out.lower() or "dry_run" in out.lower()


def test_flatfield_dry_run_refusal_still_reports_spread_and_reason(capsys):
    daemon = _FakeDaemon(
        result=_result(
            succeeded=False,
            spread_after=(1.90,),
            spread_before=(2.10,),
            reason="max_spread_after exceeded: 1.90 > 1.20",
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(_args(dry_run=True), discovery_factory=lambda: discovery)

    assert code == 1
    out = capsys.readouterr().out
    assert "not persisted" in out.lower()
    assert "2.100" in out
    assert "1.900" in out
    assert "max_spread_after exceeded" in out


# ---------------------------------------------------------------------------
# Explicit refusal -- data, not an exception
# ---------------------------------------------------------------------------


def test_flatfield_refusal_reports_reason_and_measured_spread_never_omitted(capsys):
    daemon = _FakeDaemon(
        result=_result(
            succeeded=False,
            spread_before=(2.10,),
            spread_after=(1.90,),
            reason="max_spread_after exceeded: 1.90 > 1.20",
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    out = capsys.readouterr().out
    assert "refused" in out
    assert "2.100" in out
    assert "1.900" in out
    assert "max_spread_after exceeded" in out


# ---------------------------------------------------------------------------
# --clear (success path)
# ---------------------------------------------------------------------------


def test_flatfield_clear_calls_clear_flat_field_and_never_capture(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(_args(clear=True), discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.cleared_camera == "arducam-ov9782-usb-camera"
    assert daemon.requested_camera is None  # capture_flat_field never called
    out = capsys.readouterr().out
    assert "cleared" in out.lower()


def test_flatfield_clear_reports_a_typed_error(capsys):
    daemon = _FakeDaemon(clear_raises=ConnectionFailed("pi-field.local", reason="refused"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(_args(clear=True), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "refused" in err


# ---------------------------------------------------------------------------
# Precondition / transport errors
# ---------------------------------------------------------------------------


def test_flatfield_no_field_mask_reports_the_typed_error(capsys):
    daemon = _FakeDaemon(capture_raises=NoFieldMask("hd-usb-camera"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = flatfield_cli.cmd_flatfield(
        _args(camera="hd-usb-camera"), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "hd-usb-camera" in err
    assert "field mask" in err.lower() or "field-interior" in err.lower() or "calibration" in err.lower()


def test_flatfield_reports_a_connection_failure(capsys):
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))

    code = flatfield_cli.cmd_flatfield(
        _args(daemon="pi-field.local"), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


# ---------------------------------------------------------------------------
# argv wiring -- 'camera flatfield' (grouped under camera.add_subparser)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aprilcam")
    parser.set_defaults(func=None)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    daemon_cli.add_subparser(subparsers)
    camera_cli.add_subparser(subparsers)
    playfield_cli.add_subparser(subparsers)
    return parser


def test_flatfield_is_wired_under_camera_as_cmd_flatfield():
    parser = _build_parser()
    args = parser.parse_args(["camera", "flatfield", "arducam-ov9782-usb-camera"])
    assert args.func is flatfield_cli.cmd_flatfield
    assert args.camera == "arducam-ov9782-usb-camera"
    assert args.all is False
    assert args.daemon is None
    assert args.exposure is None
    assert args.frames is None
    assert args.force is False
    assert args.dry_run is False
    assert args.clear is False


def test_flatfield_appears_in_camera_help(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "-h"])
    out = capsys.readouterr().out
    assert "flatfield" in out


def test_flatfield_parses_exposure_frames_force_dry_run():
    parser = _build_parser()
    args = parser.parse_args(
        [
            "camera",
            "flatfield",
            "arducam-ov9782-usb-camera",
            "--exposure",
            "-4.0",
            "--frames",
            "16",
            "--force",
            "--dry-run",
        ]
    )
    assert args.exposure == pytest.approx(-4.0)
    assert args.frames == 16
    assert args.force is True
    assert args.dry_run is True


def test_flatfield_parses_clear():
    parser = _build_parser()
    args = parser.parse_args(["camera", "flatfield", "arducam-ov9782-usb-camera", "--clear"])
    assert args.clear is True


def test_flatfield_requires_a_camera_positional_argument():
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "flatfield"])


def test_flatfield_accepts_a_persistent_number_as_the_camera_token():
    parser = _build_parser()
    args = parser.parse_args(["camera", "flatfield", "3"])
    assert args.camera == "3"


def test_flatfield_accepts_all_at_parse_time_but_cmd_flatfield_refuses_it_at_runtime():
    parser = _build_parser()
    args = parser.parse_args(["camera", "flatfield", "some-camera", "--all"])
    assert args.all is True


def test_flatfield_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "flatfield", "some-camera", "-h"])
    assert excinfo.value.code == 0
