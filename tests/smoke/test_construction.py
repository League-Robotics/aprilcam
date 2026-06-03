"""Smoke tests: verify basic construction without hardware."""

from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.needs_cv2

MOVIES_DIR = Path(__file__).parent.parent / "movies"


def test_detector_config_defaults():
    from aprilcam.core import DetectorConfig
    cfg = DetectorConfig()
    assert cfg.family == "36h11"
    assert cfg.proc_width == 0


def test_tag_detector_construction():
    from aprilcam.core import TagDetector
    td = TagDetector()
    assert td is not None


def test_velocity_estimator_construction():
    from aprilcam.core import VelocityEstimator
    ve = VelocityEstimator()
    assert ve.speed == 0.0
    assert ve.velocity == (0.0, 0.0)


def test_optical_flow_tracker_construction():
    from aprilcam.core import OpticalFlowTracker
    t = OpticalFlowTracker(detect_interval=3)
    assert t.should_detect() is True
    assert t.frame_index == 0


def test_ring_buffer_construction():
    from aprilcam.core import RingBuffer
    rb = RingBuffer(maxlen=10)
    assert len(rb) == 0
    assert rb.get_latest() is None


def test_video_camera_construction():
    from aprilcam import VideoCamera
    mov = MOVIES_DIR / "bright-gsc.mov"
    if not mov.exists():
        pytest.skip("Test video not available")
    cam = VideoCamera(mov)
    assert cam.name == "bright-gsc"
    assert cam.index == -1
    assert not cam.is_open


def test_video_camera_not_found():
    from aprilcam import VideoCamera
    with pytest.raises(FileNotFoundError):
        VideoCamera("/nonexistent/video.mov")


def test_tui_construction():
    from aprilcam.ui import TagTableTUI
    tui = TagTableTUI()
    tui.stop()  # should be safe even if not started


def test_field_spec():
    from aprilcam import FieldSpec
    fs = FieldSpec(width_in=40.0, height_in=35.0, units="inch")
    assert fs.width_cm == pytest.approx(101.6, abs=0.1)
    assert fs.height_cm == pytest.approx(88.9, abs=0.1)
