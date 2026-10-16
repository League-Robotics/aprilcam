"""Proto round trips for lens provenance (sprint 044 ticket 010)."""
from __future__ import annotations

from aprilcam.calibration.calibration import Calibration as ConcreteCalibration
from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.client.models import (
    calibration_from_proto,
    calibration_result_from_proto,
    camera_state_from_proto,
)
from aprilcam.daemon.rpc import (
    _calibration_result_to_proto,
    _calibration_to_proto,
    _camera_state_to_proto,
)
from aprilcam.proto import aprilcam_v2_pb2
from aprilcam.types import CameraState

_K = ((800.0, 0.0, 320.0), (0.0, 800.0, 240.0), (0.0, 0.0, 1.0))
_H = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _state(**kw) -> CameraState:
    return CameraState(name="cam", present=True, usable=True, playfield=None, calibrated=True, **kw)


def test_camera_state_lens_fields_round_trip():
    state = _state(lens_model="measured", lens_pending=True)
    back = camera_state_from_proto(_camera_state_to_proto(state))
    assert (back.lens_model, back.lens_pending) == ("measured", True)


def test_camera_state_defaults_round_trip_and_old_daemon_reads_absent():
    back = camera_state_from_proto(_camera_state_to_proto(_state()))
    assert (back.lens_model, back.lens_pending) == ("absent", False)
    # An older daemon never sets the field: empty string -> "absent".
    legacy = aprilcam_v2_pb2.CameraState(name="cam", present=True, usable=True)
    assert camera_state_from_proto(legacy).lens_model == "absent"


def test_calibration_result_lens_fields_round_trip_with_none_preserved():
    full = CalibrationResult(
        succeeded=True, matched_ids=(1,), missing_ids=(), lens_model_source="legacy", lens_warning="w"
    )
    back = calibration_result_from_proto(_calibration_result_to_proto(full))
    assert (back.lens_model_source, back.lens_warning) == ("legacy", "w")
    bare = CalibrationResult(succeeded=False, matched_ids=(), missing_ids=(1,))
    back = calibration_result_from_proto(_calibration_result_to_proto(bare))
    assert back.lens_model_source is None and back.lens_warning is None


def test_calibration_lens_provenance_round_trips_and_matrix_stays_off_the_client():
    cal = ConcreteCalibration(
        stale=False, calibrated_at=1.0, playfield="p", homography=_H, matched_ids=(1,),
        camera_matrix=_K, dist_coeffs=(0.1, 0.0, 0.0, 0.0, 0.0), lens_source="measured",
        lens_views=14, lens_rms_px=0.4, lens_residual_px=0.6, lens_installed_at=99.0,
    )
    back = calibration_from_proto(_calibration_to_proto(cal))
    assert back.lens_source == "measured"
    assert (back.lens_views, back.lens_rms_px, back.lens_residual_px, back.lens_installed_at) == (
        14, 0.4, 0.6, 99.0,
    )
    assert not hasattr(back, "camera_matrix")


def test_calibration_without_a_lens_keeps_none():
    cal = ConcreteCalibration(
        stale=False, calibrated_at=1.0, playfield="p", homography=_H, matched_ids=(1,)
    )
    back = calibration_from_proto(_calibration_to_proto(cal))
    assert back.lens_source is None and back.lens_views is None and back.lens_rms_px is None
