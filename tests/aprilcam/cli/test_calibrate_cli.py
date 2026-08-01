"""Tests for ``aprilcam.cli.calibrate`` (ticket 022-003).

Headless, matching ``test_camera_cli.py``/``test_playfield_cli.py``'s
established style: a fake ``Daemon``/``Discovery`` pair injected
through ``cmd_calibrate``'s keyword seams -- no real daemon, no vision.
``cmd_calibrate`` is a pure RPC orchestrator (module docstring), so
every test here is about argv shaping and result/error presentation,
never about calibration math itself (``tests/aprilcam/calibration/``
covers that).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional, Tuple

import pytest

from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.cli import calibrate as calibrate_cli
from aprilcam.cli import camera as camera_cli
from aprilcam.cli import daemon as daemon_cli
from aprilcam.cli import playfield as playfield_cli
from aprilcam.errors import ConnectionFailed, NoPlayfieldLinked


def _result(
    succeeded: bool = True,
    matched_ids: Tuple[int, ...] = tuple(range(1, 11)),
    missing_ids: Tuple[int, ...] = (),
    reprojection_error_cm: Optional[float] = 0.125,
    reason: Optional[str] = None,
) -> CalibrationResult:
    return CalibrationResult(
        succeeded=succeeded,
        matched_ids=matched_ids,
        missing_ids=missing_ids,
        reprojection_error_cm=reprojection_error_cm,
        reason=reason,
    )


@dataclass
class _FakeDaemon:
    result: Optional[CalibrationResult] = None
    calibrate_raises: Optional[Exception] = None
    requested_camera: Optional[str] = None

    def calibrate(self, camera: str) -> CalibrationResult:
        self.requested_camera = camera
        if self.calibrate_raises is not None:
            raise self.calibrate_raises
        return self.result if self.result is not None else _result()


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
    base = dict(daemon=None, all=False, camera="arducam-ov9782-usb-camera")
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# --all refusal (UC-15.S-4)
# ---------------------------------------------------------------------------


def test_calibrate_refuses_all_before_ever_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect when --all is refused up front")

    code = calibrate_cli.cmd_calibrate(
        _args(all=True), discovery_factory=lambda: _ExplodingDiscovery()
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "--all" in err
    assert "single" in err.lower()


# ---------------------------------------------------------------------------
# Success (UC-15.S-1)
# ---------------------------------------------------------------------------


def test_calibrate_success_reports_matched_ids_and_reprojection_error(capsys):
    daemon = _FakeDaemon(result=_result())
    discovery = _FakeDiscovery(daemon=daemon)

    code = calibrate_cli.cmd_calibrate(
        _args(camera="arducam-ov9782-usb-camera"), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    out = capsys.readouterr().out
    assert "succeeded" in out
    assert "arducam-ov9782-usb-camera" in out
    assert "10" in out  # 10 matched ids
    for tag_id in range(1, 11):
        assert str(tag_id) in out
    assert "0.125" in out


def test_calibrate_passes_the_daemon_flag_through_to_connect():
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    calibrate_cli.cmd_calibrate(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert discovery.connected_with == "pi-field.local"


# ---------------------------------------------------------------------------
# Explicit failure -- data, not an exception (UC-04.S-2, UC-04.S-3)
# ---------------------------------------------------------------------------


def test_calibrate_failure_names_exactly_the_missing_ids_never_a_count(capsys):
    daemon = _FakeDaemon(result=_result(succeeded=False, matched_ids=(1, 2, 3, 4, 5, 7, 8, 9, 10), missing_ids=(6,)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "failed" in err
    assert "6" in err
    assert "9 of 10" not in err  # UC-04.S-2: never a count


def test_calibrate_degenerate_geometry_failure_prints_the_named_reason(capsys):
    daemon = _FakeDaemon(
        result=_result(
            succeeded=False,
            matched_ids=(1, 2, 3, 4),
            missing_ids=(),
            reprojection_error_cm=None,
            reason="surveyed marker positions are collinear; cannot solve a homography from them",
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "collinear" in err


# ---------------------------------------------------------------------------
# Precondition / transport errors (UC-15.S-2, ConnectionFailed)
# ---------------------------------------------------------------------------


def test_calibrate_no_playfield_linked_reports_the_typed_error(capsys):
    daemon = _FakeDaemon(calibrate_raises=NoPlayfieldLinked("hd-usb-camera"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = calibrate_cli.cmd_calibrate(_args(camera="hd-usb-camera"), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "hd-usb-camera" in err
    assert "playfield" in err.lower()


def test_calibrate_reports_a_connection_failure(capsys):
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))

    code = calibrate_cli.cmd_calibrate(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


# ---------------------------------------------------------------------------
# argv wiring -- 'camera calibrate' (grouped under camera.add_subparser)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aprilcam")
    parser.set_defaults(func=None)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    daemon_cli.add_subparser(subparsers)
    camera_cli.add_subparser(subparsers)
    playfield_cli.add_subparser(subparsers)
    return parser


def test_calibrate_is_wired_under_camera_as_cmd_calibrate():
    parser = _build_parser()
    args = parser.parse_args(["camera", "calibrate", "arducam-ov9782-usb-camera"])
    assert args.func is calibrate_cli.cmd_calibrate
    assert args.camera == "arducam-ov9782-usb-camera"
    assert args.all is False
    assert args.daemon is None


def test_calibrate_requires_a_camera_positional_argument():
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "calibrate"])


def test_calibrate_accepts_a_persistent_number_as_the_camera_token():
    parser = _build_parser()
    args = parser.parse_args(["camera", "calibrate", "3"])
    assert args.camera == "3"


def test_calibrate_accepts_all_at_parse_time_but_cmd_calibrate_refuses_it_at_runtime():
    """``--all`` is wired onto this subparser (so ``-h`` documents it and
    the refusal message can explain itself, matching ``camera view
    --all``'s pattern) -- argparse accepts it; ``cmd_calibrate`` is what
    refuses it, exercised above."""
    parser = _build_parser()
    args = parser.parse_args(["camera", "calibrate", "some-camera", "--all"])
    assert args.all is True


def test_calibrate_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "calibrate", "some-camera", "-h"])
    assert excinfo.value.code == 0
