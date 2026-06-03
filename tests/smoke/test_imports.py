"""Smoke tests: verify all new classes import cleanly."""

import pytest

pytestmark = pytest.mark.needs_cv2


def test_import_camera():
    from aprilcam import Camera
    assert Camera is not None


def test_import_video_camera():
    from aprilcam import VideoCamera
    assert VideoCamera is not None


def test_import_playfield():
    from aprilcam import Playfield
    assert Playfield is not None


def test_import_tag():
    from aprilcam import Tag
    assert Tag is not None


def test_import_calibrate():
    from aprilcam import calibrate
    assert callable(calibrate)


def test_import_tag_record():
    from aprilcam import TagRecord
    assert TagRecord is not None


def test_import_object_record():
    from aprilcam import ObjectRecord
    assert ObjectRecord is not None


def test_import_tag_detector():
    from aprilcam.core import TagDetector, DetectorConfig, Detection
    assert TagDetector is not None
    assert DetectorConfig is not None
    assert Detection is not None


def test_import_velocity_estimator():
    from aprilcam.core import VelocityEstimator
    assert VelocityEstimator is not None


def test_import_optical_flow_tracker():
    from aprilcam.core import OpticalFlowTracker
    assert OpticalFlowTracker is not None


def test_import_detection_pipeline():
    from aprilcam.core import DetectionPipeline
    assert DetectionPipeline is not None


def test_import_ring_buffer():
    from aprilcam.core import RingBuffer
    assert RingBuffer is not None


def test_import_tui():
    from aprilcam.ui import TagTableTUI
    assert TagTableTUI is not None


def test_import_errors():
    from aprilcam import CameraError, CameraNotFoundError, CameraInUseError, CameraPermissionError
    assert CameraError is not None


def test_import_calibration_types():
    from aprilcam import CameraCalibration, FieldSpec
    assert CameraCalibration is not None
    assert FieldSpec is not None
