"""Pose provenance on Calibration / CalibrationResult (sprint 045 ticket 005)."""
from __future__ import annotations

import json

import pytest

from aprilcam.calibration.calibration import Calibration
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
from aprilcam.types import CameraState, WorldPoint

_H = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

_OLD_FILE = {
    "stale": False,
    "calibrated_at": 5.0,
    "playfield": "p",
    "homography": [list(r) for r in _H],
    "matched_ids": [1, 2],
    "reprojection_error_cm": 0.2,
}

_POSE = dict(
    method="pose", confidence="full", camera_height_cm=120.5,
    camera_nadir_x_cm=3.0, camera_nadir_y_cm=-4.0, camera_tilt_deg=7.5,
    reprojection_error_px=0.4, corner_count=40, scale_agreement=1.02,
)


def _cal(**kw) -> Calibration:
    return Calibration(stale=False, calibrated_at=5.0, playfield="p", homography=_H,
                       matched_ids=(1, 2), **kw)


def test_pre_045_file_loads_with_none_and_reserialises_identically():
    cal = Calibration.from_dict(_OLD_FILE)
    for name in _POSE:
        assert getattr(cal, name) is None
    assert json.dumps(cal.to_dict(), sort_keys=True) == json.dumps(_OLD_FILE, sort_keys=True)


def test_pose_fields_round_trip_through_dict():
    cal = _cal(**_POSE)
    data = cal.to_dict()
    for name, value in _POSE.items():
        assert data[name] == value
    assert Calibration.from_dict(json.loads(json.dumps(data))) == cal


def test_camera_geometry_cases():
    pose = _cal(**_POSE)
    assert pose.camera_geometry() == (WorldPoint(3.0, -4.0), 120.5)
    stale = _cal(**{**_POSE}, ) 
    import dataclasses
    assert dataclasses.replace(stale, stale=True).camera_geometry() is None
    assert _cal(**{**_POSE, "method": "centre-point"}).camera_geometry() is None
    assert _cal().camera_geometry() is None


def test_calibration_proto_round_trip_keeps_presence():
    back = calibration_from_proto(_calibration_to_proto(_cal(**_POSE)))
    for name, value in _POSE.items():
        assert getattr(back, name) == value
    bare = calibration_from_proto(_calibration_to_proto(_cal()))
    for name in _POSE:
        assert getattr(bare, name) is None


def test_calibration_result_proto_round_trip():
    full = CalibrationResult(
        succeeded=True, matched_ids=(1,), missing_ids=(), method="pose",
        method_reason=None, confidence="reduced", corner_count=36,
        reprojection_error_px=0.3, scale_agreement=1.01, angular_spread_deg=22.0,
        camera_height_cm=100.0, camera_nadir=WorldPoint(1.5, 2.5), camera_tilt_deg=3.0,
        notes=("a", "b"), helpful_missing_ids=(7, 9), leave_one_out_worst_id=4,
        leave_one_out_worst_cm=0.8, layout_extent_ratio=0.5,
    )
    assert calibration_result_from_proto(_calibration_result_to_proto(full)) == full
    bare = CalibrationResult(succeeded=False, matched_ids=(), missing_ids=(1,))
    assert calibration_result_from_proto(_calibration_result_to_proto(bare)) == bare


def test_camera_state_method_and_confidence_round_trip():
    state = CameraState(name="c", present=True, usable=True, playfield=None, calibrated=True,
                        calibration_method="pose", calibration_confidence="low")
    back = camera_state_from_proto(_camera_state_to_proto(state))
    assert (back.calibration_method, back.calibration_confidence) == ("pose", "low")
    empty = camera_state_from_proto(_camera_state_to_proto(
        CameraState(name="c", present=True, usable=True, playfield=None, calibrated=False)))
    assert (empty.calibration_method, empty.calibration_confidence) == ("", "")
