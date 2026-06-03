"""Unit tests for OpticalFlowTracker."""

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.core import OpticalFlowTracker, Detection


def _make_detection(tag_id, cx, cy):
    corners = np.array([
        [cx - 5, cy - 5], [cx + 5, cy - 5],
        [cx + 5, cy + 5], [cx - 5, cy + 5],
    ], dtype=np.float32)
    return Detection(id=tag_id, center=(cx, cy), corners=corners, family="36h11")


class TestOpticalFlowTracker:

    def test_construction(self):
        t = OpticalFlowTracker(detect_interval=3)
        assert t.should_detect() is True

    def test_update_with_detections_returns_them(self):
        t = OpticalFlowTracker()
        gray = np.zeros((480, 640), dtype=np.uint8)
        dets = [_make_detection(1, 100, 200)]
        result = t.update(gray, dets)
        assert len(result) == 1
        assert result[0].id == 1

    def test_update_without_detections_on_blank(self):
        t = OpticalFlowTracker()
        gray = np.zeros((480, 640), dtype=np.uint8)
        # First: provide detections
        dets = [_make_detection(1, 100, 200)]
        t.update(gray, dets)
        # Second: track (but blank frame = no texture = tracking fails)
        result = t.update(gray)
        assert isinstance(result, list)

    def test_should_detect_frame_zero(self):
        t = OpticalFlowTracker(detect_interval=5)
        assert t.should_detect() is True

    def test_should_detect_respects_interval(self):
        t = OpticalFlowTracker(detect_interval=3)
        gray = np.random.randint(0, 255, (100, 100), dtype=np.uint8)
        dets = [_make_detection(1, 50, 50)]
        t.update(gray, dets)  # frame 0
        assert not t.should_detect()  # frame 1
        gray2 = np.random.randint(0, 255, (100, 100), dtype=np.uint8)
        t.update(gray2)  # frame 1
        assert not t.should_detect()  # frame 2

    def test_reset(self):
        t = OpticalFlowTracker()
        gray = np.zeros((480, 640), dtype=np.uint8)
        t.update(gray, [_make_detection(1, 100, 200)])
        t.reset()
        assert t.frame_index == 0
        assert t.should_detect() is True
