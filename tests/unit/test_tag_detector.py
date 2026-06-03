"""Unit tests for TagDetector."""

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.core import TagDetector, DetectorConfig, Detection


class TestTagDetector:

    def test_default_construction(self):
        td = TagDetector()
        assert td is not None

    def test_custom_config(self):
        cfg = DetectorConfig(family="25h9", proc_width=640)
        td = TagDetector(cfg)
        assert td is not None

    def test_detect_on_blank_frame(self):
        td = TagDetector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detections = td.detect(frame)
        assert isinstance(detections, list)
        assert len(detections) == 0

    def test_detect_returns_detection_objects(self):
        td = TagDetector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detections = td.detect(frame)
        assert isinstance(detections, list)
        # No tags in a blank frame
        for d in detections:
            assert isinstance(d, Detection)

    def test_detect_with_precomputed_gray(self):
        import cv2 as cv
        td = TagDetector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
        detections = td.detect(frame, gray=gray)
        assert isinstance(detections, list)

    def test_detection_dataclass_fields(self):
        d = Detection(id=42, center=(100.0, 200.0),
                      corners=np.zeros((4, 2), dtype=np.float32),
                      family="36h11")
        assert d.id == 42
        assert d.center == (100.0, 200.0)
        assert d.family == "36h11"
        assert d.corners.shape == (4, 2)
