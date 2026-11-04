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
from aprilcam.cli._common import add_daemon_option
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
    lens_model_source: Optional[str] = None,
    lens_warning: Optional[str] = None,
) -> CalibrationResult:
    return CalibrationResult(
        succeeded=succeeded,
        matched_ids=matched_ids,
        missing_ids=missing_ids,
        reprojection_error_cm=reprojection_error_cm,
        reason=reason,
        lens_model_source=lens_model_source,
        lens_warning=lens_warning,
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
    base = dict(
        daemon=None, all=False, camera="arducam-ov9782-usb-camera"
    )
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


def test_calibrate_success_with_four_or_fewer_matched_ids_no_longer_prints_the_blanket_caveat(capsys):
    daemon = _FakeDaemon(result=_result(matched_ids=(1, 2, 3, 4)))
    discovery = _FakeDiscovery(daemon=daemon)

    code = calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    err = capsys.readouterr().err
    # Sprint 045: replaced by the honest confidence labels.
    assert "degrees-of-freedom-limited" not in err


def test_calibrate_success_with_more_than_four_matched_ids_omits_the_dof_caveat(capsys):
    daemon = _FakeDaemon(result=_result(matched_ids=tuple(range(1, 11))))
    discovery = _FakeDiscovery(daemon=daemon)

    calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    err = capsys.readouterr().err
    assert "degrees-of-freedom-limited" not in err


def test_removed_estimator_flag_is_gone_from_help_and_parser(capsys):
    flag = "--with-" + "intrinsics"
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "calibrate", "-h"])
    assert flag not in capsys.readouterr().out
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "calibrate", "cam", flag])
    assert excinfo.value.code == 2
    assert f"unrecognized arguments: {flag}" in capsys.readouterr().err


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
    add_daemon_option(parser)
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


@pytest.mark.parametrize(
    "source, expected",
    [
        ("measured", "lens model: measured"),
        ("estimated", "lens model: estimated"),
        ("legacy", "lens model: legacy"),
        ("none", "lens model: none"),
    ],
)
def test_calibrate_success_states_which_lens_model_was_carried_forward(capsys, source, expected):
    discovery = _FakeDiscovery(
        daemon=_FakeDaemon(result=_result(lens_model_source=source))
    )

    code = calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert expected in capsys.readouterr().out


def test_calibrate_prints_the_resolution_mismatch_warning(capsys):
    warning = "stored lens model for 'cam' was measured at 1280x720 but frames are 640x480"
    discovery = _FakeDiscovery(
        daemon=_FakeDaemon(
            result=_result(lens_model_source="none", lens_warning=warning)
        )
    )

    calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert warning in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Sprint 045 ticket 007: pose provenance reporting
# ---------------------------------------------------------------------------

from aprilcam.types import WorldPoint  # noqa: E402


def _pose_result(**overrides) -> CalibrationResult:
    base = dict(
        succeeded=True,
        matched_ids=(1, 2, 3, 4, 5),
        missing_ids=(),
        reprojection_error_cm=0.12,
        lens_model_source="measured",
        method="pose",
        confidence="full",
        corner_count=20,
        reprojection_error_px=0.31,
        scale_agreement=1.004,
        angular_spread_deg=48.2,
        camera_height_cm=150.0,
        camera_nadir=WorldPoint(3.5, -4.0),
        camera_tilt_deg=7.5,
        leave_one_out_worst_id=3,
        leave_one_out_worst_cm=0.42,
        leave_one_out_cm=((1, 0.1), (2, 0.2), (3, 0.42)),
    )
    base.update(overrides)
    return CalibrationResult(**base)


def _run(result, **argv):
    daemon = _FakeDaemon(result=result)
    return calibrate_cli.cmd_calibrate(
        _args(**argv), discovery_factory=lambda: _FakeDiscovery(daemon=daemon)
    )


def test_pose_success_prints_every_number(capsys):
    assert _run(_pose_result()) == 0
    cap = capsys.readouterr()
    for text in (
        "method: pose", "confidence: full", "corners used: 20", "0.31 px", "0.12 cm",
        "scale agreement: 1.004", "angular spread: 48.2 deg", "lens model: measured",
        "height 150.0 cm", "(3.5, -4.0) cm", "tilt 7.5 deg",
        "leave-one-out worst: marker 3, 0.42 cm",
    ):
        assert text in cap.out, text
    assert "marker 1:" not in cap.out  # table only with --verbose
    assert "degrees-of-freedom" not in cap.err
    assert "warning" not in cap.err


def test_pose_success_verbose_prints_the_per_marker_table(capsys):
    assert _run(_pose_result(), verbose=True) == 0
    out = capsys.readouterr().out
    assert "marker 1: 0.10 cm" in out and "marker 3: 0.42 cm" in out


def test_reduced_and_helpful_missing_warn_on_stderr(capsys):
    result = _pose_result(
        confidence="reduced", notes=("two markers: camera position is poorly constrained",),
        helpful_missing_ids=(7, 9),
    )
    assert _run(result) == 0
    cap = capsys.readouterr()
    assert "confidence: reduced" in cap.out
    assert "warning: calibration confidence is reduced" in cap.err
    assert "two markers: camera position" in cap.err
    assert "helpful marker id(s) not detected: 7, 9" in cap.err


def test_low_confidence_warns_with_cluster_note(capsys):
    result = _pose_result(confidence="low", notes=("markers span only 4.0 deg",))
    assert _run(result) == 0
    err = capsys.readouterr().err
    assert "confidence is low" in err and "markers span only 4.0 deg" in err


def test_centre_point_fallback_prints_method_and_reason(capsys):
    result = CalibrationResult(
        succeeded=True, matched_ids=(1, 2, 3, 4), missing_ids=(),
        reprojection_error_cm=0.2, method="centre-point",
        method_reason="no measured lens model", confidence="reduced",
    )
    assert _run(result) == 0
    cap = capsys.readouterr()
    assert "method: centre-point (no measured lens model)" in cap.out
    assert "no measured lens model" in cap.err
    assert "degrees-of-freedom" not in cap.err


def test_gate_failure_names_gate_threshold_and_measured_values(capsys):
    reason = (
        "pose calibration rejected: corner reprojection rms 2.100 px exceeds the 1.500 px "
        "acceptance threshold (worst leave-one-out: marker 3, 4.00 cm from where the others place it)"
    )
    result = _pose_result(
        succeeded=False, reason=reason, reprojection_error_px=2.1, confidence=None
    )
    assert _run(result) == 1
    err = capsys.readouterr().err
    assert "exceeds the 1.500 px acceptance threshold" in err
    assert "reprojection error: 2.10 px" in err
    assert "marker 3" in err


def test_missing_ids_failure_wording_unchanged(capsys):
    result = CalibrationResult(succeeded=False, matched_ids=(1,), missing_ids=(2, 5))
    assert _run(result) == 1
    assert "missing ArUco id(s): 2, 5" in capsys.readouterr().err


def test_camera_lens_alias_parses_like_intrinsics():
    import aprilcam.cli as cli_module

    parser = cli_module._build_parser()
    a = parser.parse_args(["camera", "lens", "cam1", "--no-gui"])
    b = parser.parse_args(["camera", "intrinsics", "cam1", "--no-gui"])
    assert a.func is b.func
    assert a.camera == b.camera == ["cam1"] and a.no_gui and b.no_gui


# ---------------------------------------------------------------------------
# ``playfield calibrate [CAMERA]`` and the deprecated ``camera calibrate``
# (sprint 049, ticket 001)
# ---------------------------------------------------------------------------


@dataclass
class _LinkedState:
    name: str
    playfield: Optional[str] = "field"
    present: bool = True


class _PfDaemon(_FakeDaemon):
    cameras: tuple = ()

    def list_cameras(self):
        return self.cameras


def _pf_args(**overrides) -> argparse.Namespace:
    return _args(**{"camera": None, **overrides})


def test_playfield_calibrate_help_has_optional_positional_and_no_camera_flag(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["playfield", "calibrate", "-h"])
    out = capsys.readouterr().out
    assert "[CAMERA]" in out
    assert "--camera" not in out


def test_playfield_calibrate_parses_positional_and_default():
    parser = _build_parser()
    assert parser.parse_args(["playfield", "calibrate", "angst-4"]).camera == "angst-4"
    assert parser.parse_args(["playfield", "calibrate"]).camera is None


def test_playfield_calibrate_explicit_camera_is_sent_verbatim(capsys):
    daemon = _PfDaemon(result=_result())
    discovery = _FakeDiscovery(daemon=daemon)
    code = calibrate_cli.cmd_playfield_calibrate(
        _pf_args(camera="brio", daemon="pi.local"), discovery_factory=lambda: discovery
    )
    assert code == 0
    assert discovery.connected_with == "pi.local"
    assert daemon.requested_camera == "brio"
    assert "calibration succeeded for 'brio'" in capsys.readouterr().out


def test_playfield_calibrate_omitted_camera_uses_the_one_linked_camera():
    daemon = _PfDaemon(result=_result())
    daemon.cameras = (_LinkedState("cam-a"), _LinkedState("cam-b", playfield=None))
    code = calibrate_cli.cmd_playfield_calibrate(
        _pf_args(), discovery_factory=lambda: _FakeDiscovery(daemon=daemon)
    )
    assert code == 0
    assert daemon.requested_camera == "cam-a"


def test_playfield_calibrate_omitted_camera_zero_linked_says_pass_camera(capsys):
    daemon = _PfDaemon()
    daemon.cameras = (_LinkedState("cam-b", playfield=None),)
    code = calibrate_cli.cmd_playfield_calibrate(
        _pf_args(), discovery_factory=lambda: _FakeDiscovery(daemon=daemon)
    )
    assert code == 1
    assert daemon.requested_camera is None
    err = capsys.readouterr().err
    assert "pass CAMERA" in err and "--camera" not in err


def test_playfield_calibrate_omitted_camera_several_linked_lists_candidates(capsys):
    daemon = _PfDaemon()
    daemon.cameras = (_LinkedState("cam-a"), _LinkedState("cam-b"))
    code = calibrate_cli.cmd_playfield_calibrate(
        _pf_args(), discovery_factory=lambda: _FakeDiscovery(daemon=daemon)
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "cam-a" in err and "cam-b" in err
    assert "pass CAMERA to disambiguate" in err


def test_playfield_calibrate_refuses_all_before_connecting(capsys):
    class _Exploding:
        def connect(self, host=None):
            raise AssertionError("must not connect")

    code = calibrate_cli.cmd_playfield_calibrate(
        _pf_args(all=True, camera="x"), discovery_factory=lambda: _Exploding()
    )
    assert code == 1
    assert "'playfield calibrate' is single-target" in capsys.readouterr().err


def test_resolver_default_hint_still_says_camera_flag():
    from aprilcam.cli._common import resolve_playfield_camera

    daemon = _PfDaemon()
    daemon.cameras = ()
    _, problem = resolve_playfield_camera(daemon)
    assert problem is not None and "pass --camera" in problem
    daemon.cameras = (_LinkedState("a"), _LinkedState("b"))
    _, problem = resolve_playfield_camera(daemon)
    assert "pass --camera to disambiguate" in problem


def test_camera_calibrate_is_deprecated_but_behaves_the_same(capsys):
    daemon = _FakeDaemon(result=_result())
    code = calibrate_cli.cmd_calibrate(
        _args(), discovery_factory=lambda: _FakeDiscovery(daemon=daemon)
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "calibration succeeded for 'arducam-ov9782-usb-camera'" in captured.out
    assert "deprecated" in captured.err
    assert "aprilcam playfield calibrate" in captured.err


def test_camera_calibrate_help_says_deprecated(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "calibrate", "-h"])
    assert "DEPRECATED" in capsys.readouterr().out
