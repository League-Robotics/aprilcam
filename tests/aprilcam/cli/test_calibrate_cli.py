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
    intrinsics_estimated: Optional[bool] = None,
    intrinsics_mode: Optional[str] = None,
    elevated_features_used: Optional[int] = None,
    lens_model_source: Optional[str] = None,
    lens_warning: Optional[str] = None,
) -> CalibrationResult:
    return CalibrationResult(
        succeeded=succeeded,
        matched_ids=matched_ids,
        missing_ids=missing_ids,
        reprojection_error_cm=reprojection_error_cm,
        reason=reason,
        intrinsics_estimated=intrinsics_estimated,
        intrinsics_mode=intrinsics_mode,
        elevated_features_used=elevated_features_used,
        lens_model_source=lens_model_source,
        lens_warning=lens_warning,
    )


@dataclass
class _FakeDaemon:
    result: Optional[CalibrationResult] = None
    calibrate_raises: Optional[Exception] = None
    requested_camera: Optional[str] = None
    requested_estimate_intrinsics: Optional[bool] = None

    def calibrate(self, camera: str, *, estimate_intrinsics: bool = False) -> CalibrationResult:
        self.requested_camera = camera
        self.requested_estimate_intrinsics = estimate_intrinsics
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
        daemon=None, all=False, camera="arducam-ov9782-usb-camera", with_intrinsics=False
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


# ---------------------------------------------------------------------------
# Intrinsics reporting (sprint 030 ticket 002)
# ---------------------------------------------------------------------------


def test_calibrate_success_reports_intrinsics_estimated_this_call_and_its_mode(capsys):
    daemon = _FakeDaemon(
        result=_result(
            intrinsics_estimated=True, intrinsics_mode="coplanar-constrained", elevated_features_used=0
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = calibrate_cli.cmd_calibrate(
        _args(with_intrinsics=True), discovery_factory=lambda: discovery
    )

    assert code == 0
    assert daemon.requested_estimate_intrinsics is True
    out = capsys.readouterr().out
    assert "intrinsics estimated this call" in out
    assert "coplanar-constrained" in out


def test_calibrate_success_reports_elevated_feature_count_when_present(capsys):
    daemon = _FakeDaemon(
        result=_result(intrinsics_estimated=True, intrinsics_mode="elevated", elevated_features_used=3)
    )
    discovery = _FakeDiscovery(daemon=daemon)

    calibrate_cli.cmd_calibrate(_args(with_intrinsics=True), discovery_factory=lambda: discovery)

    out = capsys.readouterr().out
    assert "elevated" in out
    assert "3" in out


def test_calibrate_success_without_the_flag_reports_carried_forward_unchanged(capsys):
    """``intrinsics_estimated=False`` (a plain, no-flag recalibration)
    prints that any previously-stored lens model was carried forward
    unchanged -- true whether or not one actually existed."""
    daemon = _FakeDaemon(result=_result(intrinsics_estimated=False))
    discovery = _FakeDiscovery(daemon=daemon)

    code = calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "carried forward unchanged" in out


def test_calibrate_success_reports_no_intrinsics_note_when_not_applicable(capsys):
    """``intrinsics_estimated=None`` (the concept did not apply to this
    call) prints neither the "estimated" nor the "carried forward"
    note."""
    daemon = _FakeDaemon(result=_result(intrinsics_estimated=None))
    discovery = _FakeDiscovery(daemon=daemon)

    calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    out = capsys.readouterr().out
    assert "intrinsics" not in out


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


def test_calibrate_with_intrinsics_wired_into_help_text(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "calibrate", "-h"])
    out = capsys.readouterr().out
    assert "--with-intrinsics" in out


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
    assert args.with_intrinsics is False


def test_calibrate_with_intrinsics_flag_parses_true_and_reaches_the_daemon_call():
    """Sprint 030 ticket 002: ``--with-intrinsics`` threads through to
    ``Daemon.calibrate``'s ``estimate_intrinsics`` keyword."""
    parser = _build_parser()
    args = parser.parse_args(
        ["camera", "calibrate", "arducam-ov9782-usb-camera", "--with-intrinsics"]
    )
    assert args.with_intrinsics is True

    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)
    code = calibrate_cli.cmd_calibrate(args, discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.requested_estimate_intrinsics is True


def test_calibrate_without_the_flag_passes_estimate_intrinsics_false():
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert daemon.requested_estimate_intrinsics is False


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


# ---------------------------------------------------------------------------
# Lens provenance (sprint 044 ticket 010)
# ---------------------------------------------------------------------------


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
        daemon=_FakeDaemon(result=_result(intrinsics_estimated=False, lens_model_source=source))
    )

    code = calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert expected in capsys.readouterr().out


def test_calibrate_prints_the_resolution_mismatch_warning(capsys):
    warning = "stored lens model for 'cam' was measured at 1280x720 but frames are 640x480"
    discovery = _FakeDiscovery(
        daemon=_FakeDaemon(
            result=_result(intrinsics_estimated=False, lens_model_source="none", lens_warning=warning)
        )
    )

    calibrate_cli.cmd_calibrate(_args(), discovery_factory=lambda: discovery)

    assert warning in capsys.readouterr().err


def test_calibrate_reports_the_refusal_of_with_intrinsics_over_a_measured_model(capsys):
    discovery = _FakeDiscovery(
        daemon=_FakeDaemon(
            result=_result(
                succeeded=False,
                matched_ids=(),
                reprojection_error_cm=None,
                reason="estimate_intrinsics refused: camera has a measured lens model installed",
                intrinsics_estimated=False,
                lens_model_source="measured",
            )
        )
    )

    code = calibrate_cli.cmd_calibrate(
        _args(with_intrinsics=True), discovery_factory=lambda: discovery
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "estimate_intrinsics refused" in err
    assert "--with-intrinsics refused" in err


def test_with_intrinsics_help_documents_the_no_board_fallback():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    calibrate_cli.add_subparser(sub)
    help_text = sub.choices["calibrate"].format_help()
    assert "no-board fallback" in help_text


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
