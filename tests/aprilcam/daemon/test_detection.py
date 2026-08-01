"""Tests for ``aprilcam.daemon.detection`` (ticket 021-004).

Headless throughout. Two fixture styles, matching the ticket's testing
plan:

* A :class:`~tests.aprilcam.camera.test_camera.FakeBackend`-backed
  ``Camera`` with a real synthetic AprilTag/ArUco image (from
  ``tests.aprilcam.vision._tag_fixtures``) run through the *real*
  ``vision.pipeline``/``vision.detector`` -- end-to-end burst
  construction, no mocks below the camera boundary.
* A substitutable, call-counting fake standing in for ``TagDetector`` --
  pure scheduling/counting assertions ("one burst = exactly one
  ``detect()`` call") that do not need real pixels at all.
"""

from __future__ import annotations

from typing import List, Optional

import cv2 as cv
import numpy as np
import pytest

from aprilcam.camera.camera import Camera
from aprilcam.daemon.detection import RING_BUFFER_SIZE, DetectionEngine
from aprilcam.types import TagFamily, TagId
from aprilcam.vision.detector import Detection, TagDetector

from tests.aprilcam.camera.test_camera import FakeBackend
from tests.aprilcam.vision._tag_fixtures import synthetic_tag_image


def _blank_frame() -> np.ndarray:
    return np.full((32, 32, 3), 255, dtype=np.uint8)


def _tag_frame_bgr(family: TagFamily, tag_id: int) -> np.ndarray:
    """A synthetic tag image, converted to BGR -- what a real ``Camera.read()`` returns."""
    gray = synthetic_tag_image(family, tag_id)
    return cv.cvtColor(gray, cv.COLOR_GRAY2BGR)


class _CountingDetector:
    """A ``TagDetector``-shaped double that only counts ``detect()`` calls.

    Not a subclass of ``TagDetector`` -- ``daemon/detection.py``'s
    ``DetectorFactory`` type only requires the same ``detect(gray, *,
    scale)`` method shape, and this module (a test file) is outside
    ``pyproject.toml``'s ``[tool.mypy] files`` scope, so nothing here
    needs nominal compatibility with the real class.
    """

    def __init__(self, hits: Optional[List[Detection]] = None) -> None:
        self.calls = 0
        self._hits = hits if hits is not None else []

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> "List[Detection]":
        self.calls += 1
        return list(self._hits)


def _camera(slug: str, *, frames=None) -> Camera:
    cam = Camera(slug, FakeBackend(read_queue=list(frames) if frames is not None else []))
    cam.open()
    return cam


# ---------------------------------------------------------------------------
# run_burst -- empty field / no-frame-available
# ---------------------------------------------------------------------------


def test_run_burst_on_empty_field_returns_empty_tag_frame_and_appends_it():
    engine = DetectionEngine(detector_factory=lambda: _CountingDetector())
    cam = _camera("cam-a", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cam-a", cam)

    assert tag_frame.tags == ()
    assert tag_frame.frame_index == 0
    assert engine.history("cam-a") == (tag_frame,)


def test_run_burst_raises_and_appends_nothing_when_no_frame_available():
    engine = DetectionEngine(detector_factory=lambda: _CountingDetector())
    cam = _camera("cam-b", frames=[])  # empty queue -> Camera.read() returns None

    with pytest.raises(RuntimeError):
        engine.run_burst("cam-b", cam)

    assert engine.history("cam-b") == ()
    assert engine.latest("cam-b") is None


# ---------------------------------------------------------------------------
# run_burst -- end-to-end against a real synthetic tag image
# ---------------------------------------------------------------------------


def test_run_burst_builds_an_honestly_uncalibrated_tag_record():
    engine = DetectionEngine()
    cam = _camera("cam-c", frames=[_tag_frame_bgr(TagFamily.APRILTAG, 7)])

    tag_frame = engine.run_burst("cam-c", cam)

    assert len(tag_frame.tags) == 1
    record = tag_frame.tags[0]
    assert record.tag == TagId(family=TagFamily.APRILTAG, number=7)
    assert record.calibrated is False
    assert record.world is None
    assert record.heading_rad is None
    assert record.world_velocity is None
    assert record.speed is None
    assert record.yaw_rad is None
    assert record.world_corners is None
    assert record.pixel is not None
    assert record.pixel_corners is not None
    assert len(record.pixel_corners) == 4
    assert record.timestamp == tag_frame.timestamp


def test_run_burst_on_a_family_disambiguated_field():
    """AprilTag 7 and ArUco 7 in the same burst are reported as two
    distinct records (family disambiguates the shared numeric id) --
    UC-09.S-3's guarantee, exercised at the burst-construction layer."""
    canvas = np.full((600, 1200), 255, dtype=np.uint8)
    canvas[:, :600] = synthetic_tag_image(TagFamily.APRILTAG, 7)
    canvas[:, 600:] = synthetic_tag_image(TagFamily.ARUCO, 7)
    frame_bgr = cv.cvtColor(canvas, cv.COLOR_GRAY2BGR)

    engine = DetectionEngine()
    cam = _camera("cam-d", frames=[frame_bgr])

    tag_frame = engine.run_burst("cam-d", cam)

    tags = {r.tag for r in tag_frame.tags}
    assert TagId(family=TagFamily.APRILTAG, number=7) in tags
    assert TagId(family=TagFamily.ARUCO, number=7) in tags


# ---------------------------------------------------------------------------
# Detector construction -- one per camera, reused across bursts
# ---------------------------------------------------------------------------


def test_detector_factory_called_once_per_camera_and_reused():
    built = []

    def factory():
        detector = _CountingDetector()
        built.append(detector)
        return detector

    engine = DetectionEngine(detector_factory=factory)
    cam = _camera("cam-e", frames=[_blank_frame(), _blank_frame(), _blank_frame()])

    engine.run_burst("cam-e", cam)
    engine.run_burst("cam-e", cam)
    engine.run_burst("cam-e", cam)

    assert len(built) == 1
    assert built[0].calls == 3


def test_detector_factory_called_separately_per_distinct_camera():
    built = []

    def factory():
        detector = _CountingDetector()
        built.append(detector)
        return detector

    engine = DetectionEngine(detector_factory=factory)
    cam_x = _camera("cam-x", frames=[_blank_frame()])
    cam_y = _camera("cam-y", frames=[_blank_frame()])

    engine.run_burst("cam-x", cam_x)
    engine.run_burst("cam-y", cam_y)

    assert len(built) == 2


# ---------------------------------------------------------------------------
# Scheduling/counting -- a substitutable detector double, no real pixels
# ---------------------------------------------------------------------------


def test_idle_engine_runs_zero_detections():
    """No ``run_burst`` call at all -> zero ``detect()`` calls, trivially
    -- there is no background loop to idle (sprint.md Decision 1)."""
    counting = _CountingDetector()
    DetectionEngine(detector_factory=lambda: counting)
    assert counting.calls == 0


def test_each_run_burst_call_is_exactly_one_detect_call():
    counting = _CountingDetector()
    engine = DetectionEngine(detector_factory=lambda: counting)
    cam = _camera("cam-f", frames=[_blank_frame() for _ in range(4)])

    for expected in range(1, 5):
        engine.run_burst("cam-f", cam)
        assert counting.calls == expected


# ---------------------------------------------------------------------------
# Ring buffer -- bounded, eviction at capacity, per-camera frame_index
# ---------------------------------------------------------------------------


def test_ring_buffer_size_defaults_to_v1s_300():
    assert RING_BUFFER_SIZE == 300


def test_ring_buffer_evicts_oldest_entry_at_capacity():
    engine = DetectionEngine(buffer_size=3, detector_factory=lambda: _CountingDetector())
    cam = _camera("cam-g", frames=[_blank_frame() for _ in range(5)])

    frames = [engine.run_burst("cam-g", cam) for _ in range(5)]

    history = engine.history("cam-g")
    assert len(history) == 3
    # Oldest two (frame_index 0, 1) evicted; the three most recent remain,
    # oldest to newest.
    assert history == tuple(frames[-3:])
    assert [f.frame_index for f in history] == [2, 3, 4]


def test_frame_index_is_monotonic_and_independent_per_camera():
    engine = DetectionEngine(detector_factory=lambda: _CountingDetector())
    cam_a = _camera("cam-h", frames=[_blank_frame(), _blank_frame()])
    cam_b = _camera("cam-i", frames=[_blank_frame()])

    first_a = engine.run_burst("cam-h", cam_a)
    first_b = engine.run_burst("cam-i", cam_b)
    second_a = engine.run_burst("cam-h", cam_a)

    assert first_a.frame_index == 0
    assert second_a.frame_index == 1
    assert first_b.frame_index == 0  # independent counter for a different camera


# ---------------------------------------------------------------------------
# history() / latest() -- pure reads, never trigger a burst
# ---------------------------------------------------------------------------


def test_history_on_a_never_seen_camera_is_empty_not_an_error():
    engine = DetectionEngine()
    assert engine.history("never-queried") == ()


def test_latest_on_a_never_seen_camera_is_none():
    engine = DetectionEngine()
    assert engine.latest("never-queried") is None


def test_latest_reflects_the_most_recent_burst():
    engine = DetectionEngine(detector_factory=lambda: _CountingDetector())
    cam = _camera("cam-j", frames=[_blank_frame(), _blank_frame()])

    first = engine.run_burst("cam-j", cam)
    second = engine.run_burst("cam-j", cam)

    assert engine.latest("cam-j") is second
    assert engine.latest("cam-j") is not first


def test_history_and_latest_do_not_call_detect():
    counting = _CountingDetector()
    engine = DetectionEngine(detector_factory=lambda: counting)
    cam = _camera("cam-k", frames=[_blank_frame()])

    engine.run_burst("cam-k", cam)
    assert counting.calls == 1

    engine.history("cam-k")
    engine.latest("cam-k")

    assert counting.calls == 1  # unchanged -- pure reads


def test_default_detector_factory_builds_a_real_tag_detector():
    engine = DetectionEngine()
    detector = engine._detector_for("cam-l")  # noqa: SLF001 - internal, testing the default wiring
    assert isinstance(detector, TagDetector)
