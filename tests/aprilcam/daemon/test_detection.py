"""Tests for ``aprilcam.daemon.detection`` (ticket 021-004; world
coordinates added ticket 022-004).

Headless throughout. Three fixture styles, matching the ticket's testing
plan:

* A :class:`~tests.aprilcam.camera.test_camera.FakeBackend`-backed
  ``Camera`` with a real synthetic AprilTag/ArUco image (from
  ``tests.aprilcam.vision._tag_fixtures``) run through the *real*
  ``vision.pipeline``/``vision.detector`` -- end-to-end burst
  construction, no mocks below the camera boundary.
* A substitutable, call-counting fake standing in for ``TagDetector`` --
  pure scheduling/counting assertions ("one burst = exactly one
  ``detect()`` call") that do not need real pixels at all.
* A substitutable, **scripted** fake standing in for ``TagDetector``
  (:class:`_ScriptedDetector`) that returns hand-built ``Detection``
  objects at known world positions/orientations (via
  ``standard_pixel_for_world`` -- the same synthetic top-down, north-up
  camera mount ``tests/aprilcam/calibration/test_calibration.py``
  establishes) paired with a real, non-trivial ``Calibration`` solved by
  ``calibration.calibration.Calibrator`` against those same fixtures
  (:func:`_fixture_calibration`). This is what the world-coordinate
  tests below use: real geometry (a real homography solve, real
  ``Calibration.to_world`` conversion), but a controlled, known pixel
  trajectory across bursts -- rendering a fresh marker image per frame
  of a multi-frame motion sequence would be needless overhead for what
  these tests check.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration.calibration import (
    DRIFT_CHECK_INTERVAL_S,
    DRIFT_TOLERANCE_CM,
    MIN_DRIFT_CHECK_MARKERS,
    Calibration,
    Calibrator,
)
from aprilcam.camera.camera import Camera
from aprilcam.config.interfaces import CameraConfig
from aprilcam.daemon.detection import RING_BUFFER_SIZE, DetectionEngine
from aprilcam.types import PixelPoint, TagFamily, TagId
from aprilcam.vision.detector import Detection, TagDetector

from tests.aprilcam.calibration.test_calibration import (
    MAIN_PLAYFIELD_ARUCO_POSITIONS,
    main_playfield_definition,
    render_playfield_frame,
    standard_pixel_for_world,
)
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


# ---------------------------------------------------------------------------
# World coordinates (ticket 022-004) -- calibrated camera path
# ---------------------------------------------------------------------------


def _fixture_calibration(camera_slug: str = "cal-camera") -> Calibration:
    """A real ``Calibration``, solved by ``calibration.calibration.
    Calibrator`` against ``main-playfield``'s real 10 surveyed ArUco
    positions and the standard synthetic top-down camera mount
    (``tests/aprilcam/calibration/test_calibration.py``'s own fixtures,
    ticket 022-003) -- not a hand-built homography, so the east/north
    sign and yaw checks below exercise a real solve, matching the
    ticket's testing plan ("a fixture ``Calibration`` from ticket 003's
    test fixtures")."""
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    camera_configs = {
        camera_slug: CameraConfig(name=camera_slug, playfield="main-playfield", settings={})
    }
    playfields = {"main-playfield": definition}
    calibrator = Calibrator(
        get_camera_config=camera_configs.get,
        get_playfield=playfields.get,
        capture_frames=lambda slug: [frame],
    )
    result = calibrator.calibrate(camera_slug)
    assert result.succeeded is True
    calibration = calibrator.get_calibration(camera_slug)
    assert calibration is not None
    return calibration


def _detection_at(
    family: TagFamily,
    number: int,
    *,
    center_world: Tuple[float, float],
    yaw_rad: float = 0.0,
    half_size_cm: float = 5.0,
) -> Detection:
    """A ``Detection`` for a synthetic tag centred at *center_world*
    (A1-centred world cm) whose front edge (``corners[0]`` ->
    ``corners[1]``) points *yaw_rad* -- 0=east, CCW positive, the same
    convention ``design.md`` Sec.6 / ``TagRecord.yaw_rad`` use. Corners
    are built directly in world space in the contract's
    (front-left, front-right, back-right, back-left) order, then mapped
    through ``standard_pixel_for_world`` -- the exact synthetic camera
    mount :func:`_fixture_calibration` solves against, so
    ``DetectionEngine``'s pixel -> world conversion recovers
    *center_world*/*yaw_rad* through the real homography, not a mock.
    """
    cx, cy = center_world
    ux, uy = math.cos(yaw_rad), math.sin(yaw_rad)  # front-edge direction
    vx, vy = -uy, ux  # perpendicular: front-edge -> back-edge direction
    corners_world = [
        (cx - half_size_cm * ux - half_size_cm * vx, cy - half_size_cm * uy - half_size_cm * vy),
        (cx + half_size_cm * ux - half_size_cm * vx, cy + half_size_cm * uy - half_size_cm * vy),
        (cx + half_size_cm * ux + half_size_cm * vx, cy + half_size_cm * uy + half_size_cm * vy),
        (cx - half_size_cm * ux + half_size_cm * vx, cy - half_size_cm * uy + half_size_cm * vy),
    ]
    corners_px = np.array(
        [standard_pixel_for_world(x, y) for x, y in corners_world], dtype=np.float32
    )
    center_px = PixelPoint(*standard_pixel_for_world(cx, cy))
    return Detection(family=family, number=number, corners=corners_px, center=center_px)


class _ScriptedDetector:
    """A ``TagDetector``-shaped double returning one hand-built hit list
    per call, in order -- lets a test walk a tag through a chosen
    world-space trajectory across successive ``run_burst`` calls without
    rendering a distinct real image per frame (module docstring)."""

    def __init__(self, hits_sequence: Sequence[Sequence[Detection]]) -> None:
        self._hits_sequence = [list(hits) for hits in hits_sequence]
        self.calls = 0

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> "List[Detection]":
        hits = self._hits_sequence[self.calls]
        self.calls += 1
        return list(hits)


def test_default_calibration_lookup_is_always_none():
    """The constructor default (module docstring's "world coordinates,
    when a calibration exists" section) -- pinned directly, the same way
    :func:`test_default_detector_factory_builds_a_real_tag_detector`
    pins ``_default_detector_factory``."""
    from aprilcam.daemon.detection import _default_calibration_lookup

    assert _default_calibration_lookup("any-slug") is None
    assert _default_calibration_lookup("main-camera") is None


def test_calibrated_camera_populates_world_fields_on_first_sighting():
    calibration = _fixture_calibration()
    hit = _detection_at(TagFamily.APRILTAG, 1, center_world=(10.0, 20.0))
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([[hit]]),
        calibration_lookup=lambda slug: calibration,
    )
    cam = _camera("cal-cam", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cal-cam", cam)

    assert len(tag_frame.tags) == 1
    record = tag_frame.tags[0]
    assert record.calibrated is True
    assert record.world is not None
    assert record.world.x == pytest.approx(10.0, abs=0.5)
    assert record.world.y == pytest.approx(20.0, abs=0.5)
    assert record.world_corners is not None
    assert len(record.world_corners) == 4
    assert record.yaw_rad is not None
    # First sighting -- insufficient history, so motion fields stay
    # None, never a fabricated zero (vision.motion.compute_motion's own
    # contract, exercised here through the real call path for the first
    # time).
    assert record.heading_rad is None
    assert record.world_velocity is None
    assert record.speed is None
    # Calibration adds world fields; it never removes the pixel ones.
    assert record.pixel is not None
    assert record.pixel_corners is not None


def test_yaw_rad_matches_atan2_of_the_front_edge_at_a_known_rotation():
    calibration = _fixture_calibration()
    known_yaw = math.radians(37.0)  # non-axis-aligned, like vision/detector.py's own convention check
    hit = _detection_at(TagFamily.APRILTAG, 1, center_world=(0.0, 0.0), yaw_rad=known_yaw)
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([[hit]]),
        calibration_lookup=lambda slug: calibration,
    )
    cam = _camera("cal-cam", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cal-cam", cam)
    record = tag_frame.tags[0]

    assert record.yaw_rad == pytest.approx(known_yaw, abs=0.05)
    # Cross-check against the returned world_corners' own front edge
    # (design.md Sec.6's contract), not just against the fixture's
    # input -- pins that yaw_rad really is derived from corners[0]->[1].
    assert record.world_corners is not None
    front_left, front_right = record.world_corners[0], record.world_corners[1]
    expected = math.atan2(front_right.y - front_left.y, front_right.x - front_left.x)
    assert record.yaw_rad == pytest.approx(expected)


def test_moving_east_across_bursts_reports_strictly_increasing_world_x():
    """SUC-004's mirror-catching check, at fixture level: physically-east
    pixel motion (through a real, solved homography) must never produce
    a decreasing or constant reported ``world.x``."""
    calibration = _fixture_calibration()
    hits = [
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(-20.0, 0.0))],
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(-10.0, 0.0))],
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(0.0, 0.0))],
    ]
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector(hits),
        calibration_lookup=lambda slug: calibration,
    )
    cam = _camera("cal-cam", frames=[_blank_frame() for _ in hits])

    xs = []
    ys = []
    for _ in hits:
        tag_frame = engine.run_burst("cal-cam", cam)
        world = tag_frame.tags[0].world
        assert world is not None
        xs.append(world.x)
        ys.append(world.y)

    assert xs[0] < xs[1] < xs[2]
    # East-only motion should not smuggle in a spurious y drift -- not a
    # strict requirement of a real (slightly noisy) homography solve,
    # but the y values should stay clustered near 0, not walk off with x.
    assert max(ys) - min(ys) < 1.0


def test_moving_north_across_bursts_reports_strictly_increasing_world_y():
    """SUC-004's mirror-catching check for the other axis: physically-
    north pixel motion must never produce a decreasing or constant
    reported ``world.y`` -- the specific check this project fears
    failing (a silent north/south mirror)."""
    calibration = _fixture_calibration()
    hits = [
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(0.0, -20.0))],
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(0.0, -10.0))],
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(0.0, 0.0))],
    ]
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector(hits),
        calibration_lookup=lambda slug: calibration,
    )
    cam = _camera("cal-cam", frames=[_blank_frame() for _ in hits])

    ys = []
    for _ in hits:
        tag_frame = engine.run_burst("cal-cam", cam)
        world = tag_frame.tags[0].world
        assert world is not None
        ys.append(world.y)

    assert ys[0] < ys[1] < ys[2]


def test_motion_fields_populate_once_a_second_calibrated_sample_exists():
    """Acceptance criterion 3: once >= 2 world samples exist for a tag,
    ``heading_rad``/``world_velocity``/``speed`` are populated and
    ``heading_rad == atan2(world_vy, world_vx)`` from the same two
    samples -- pinning ``vision.motion.compute_motion``'s existing
    contract against the real call path for the first time."""
    calibration = _fixture_calibration()
    hits = [
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(-10.0, 0.0))],
        [_detection_at(TagFamily.APRILTAG, 1, center_world=(10.0, 0.0))],
    ]
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector(hits),
        calibration_lookup=lambda slug: calibration,
    )
    cam = _camera("cal-cam", frames=[_blank_frame() for _ in hits])

    first = engine.run_burst("cal-cam", cam)
    first_record = first.tags[0]
    assert first_record.heading_rad is None
    assert first_record.world_velocity is None
    assert first_record.speed is None

    second = engine.run_burst("cal-cam", cam)
    second_record = second.tags[0]
    assert second_record.heading_rad is not None
    assert second_record.world_velocity is not None
    assert second_record.speed is not None
    assert second_record.speed > 0.0

    w0, w1 = first_record.world, second_record.world
    assert w0 is not None and w1 is not None
    expected_heading = math.atan2(w1.y - w0.y, w1.x - w0.x)
    assert second_record.heading_rad == pytest.approx(expected_heading)


def test_world_corners_preserve_order_and_use_the_same_homography_as_world():
    """Acceptance criterion: ``world_corners`` preserve the
    (front-left, front-right, back-right, back-left) order and are
    converted through the identical homography used for ``world`` --
    never independently recomputed. Checked by re-deriving each
    ``world_corners`` entry via ``calibration.to_world`` on the matching
    ``pixel_corners`` entry and requiring an exact match."""
    calibration = _fixture_calibration()
    hit = _detection_at(
        TagFamily.APRILTAG, 1, center_world=(15.0, -8.0), yaw_rad=math.radians(20.0)
    )
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([[hit]]),
        calibration_lookup=lambda slug: calibration,
    )
    cam = _camera("cal-cam", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cal-cam", cam)
    record = tag_frame.tags[0]

    assert record.world_corners is not None
    assert record.pixel_corners is not None
    assert len(record.world_corners) == 4
    for world_corner, pixel_corner in zip(record.world_corners, record.pixel_corners):
        expected = calibration.to_world(pixel_corner)
        assert world_corner.x == pytest.approx(expected.x)
        assert world_corner.y == pytest.approx(expected.y)


# ---------------------------------------------------------------------------
# Uncalibrated camera regression (ticket 022-004's own honesty
# requirement: world coordinates must be purely additive)
# ---------------------------------------------------------------------------


def test_uncalibrated_camera_output_is_byte_for_byte_sprint_021_shape():
    """A camera with no ``Calibration`` -- even when a *different*
    camera known to the same engine instance is calibrated -- reports
    exactly sprint 021's shape: ``calibrated=False``, every world/motion
    field ``None``, only ``pixel``/``pixel_corners`` populated. This is
    the ticket's explicit regression test, not just an assumption
    inherited from sprint 021's own suite (which still passes unchanged
    -- see :func:`test_run_burst_builds_an_honestly_uncalibrated_tag_record`
    above)."""
    calibration = _fixture_calibration()

    def calibration_lookup(slug: str) -> Optional[Calibration]:
        return calibration if slug == "calibrated-cam" else None

    hit = _detection_at(TagFamily.APRILTAG, 1, center_world=(0.0, 0.0))
    engine = DetectionEngine(
        detector_factory=lambda: _CountingDetector(hits=[hit]),
        calibration_lookup=calibration_lookup,
    )
    cam = _camera("uncalibrated-cam", frames=[_blank_frame()])

    tag_frame = engine.run_burst("uncalibrated-cam", cam)

    assert len(tag_frame.tags) == 1
    record = tag_frame.tags[0]
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


# ---------------------------------------------------------------------------
# Opportunistic staleness drift check (ticket 023-007) -- piggybacked on
# run_burst's already-read frame/already-run detections, per sprint.md
# Decision 5. Real geometry throughout (a real Calibrator-solved
# Calibration, real Calibration.to_world conversion), scripted detections
# for a controlled marker trajectory -- same fixture style as the
# world-coordinate suite above.
# ---------------------------------------------------------------------------


def _aruco_hits(positions: Dict[int, Tuple[float, float]]) -> List[Detection]:
    """One ArUco ``Detection`` per *positions* entry (id -> world cm),
    via :func:`_detection_at` -- simulates a burst's detector output
    that happens to include some of a calibration's matched static
    corner markers alongside whatever else was in frame."""
    return [_detection_at(TagFamily.ARUCO, tag_id, center_world=pos) for tag_id, pos in positions.items()]


def test_drift_beyond_tolerance_marks_the_calibration_stale():
    """A camera bump, simulated: one matched marker re-detected 5 cm from
    its surveyed position -- well past the 2.0 cm tolerance -- flips
    stale via the injected ``mark_stale`` callable."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 5.0, true_y)
    mark_stale_calls: List[str] = []
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([_aruco_hits(positions)]),
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: mark_stale_calls.append(slug),
    )
    cam = _camera("cal-camera", frames=[_blank_frame()])

    engine.run_burst("cal-camera", cam)

    assert mark_stale_calls == ["cal-camera"]


def test_drift_within_tolerance_does_not_mark_stale():
    """A small, sub-tolerance shift (0.5 cm, comfortably inside the 2.0
    cm tolerance) must not flip stale -- the false-positive guard on
    ordinary detection jitter."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 0.5, true_y)
    mark_stale_calls: List[str] = []
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([_aruco_hits(positions)]),
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: mark_stale_calls.append(slug),
    )
    cam = _camera("cal-camera", frames=[_blank_frame()])

    engine.run_burst("cal-camera", cam)

    assert mark_stale_calls == []


def test_no_markers_visible_does_not_mark_stale():
    """Darkness, a covered field, a lens cap -- zero of the calibration's
    matched markers detected this burst must never be treated as
    evidence of drift (the design question this ticket explicitly
    resolves: 'cannot see markers' is not 'drift detected')."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    mark_stale_calls: List[str] = []
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([[]]),  # nothing detected at all
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: mark_stale_calls.append(slug),
    )
    cam = _camera("cal-camera", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cal-camera", cam)

    assert mark_stale_calls == []
    assert tag_frame.tags == ()
    assert tag_frame.calibration_stale is False


def test_insufficient_markers_does_not_mark_stale_even_with_a_huge_shift():
    """Fewer than :data:`MIN_DRIFT_CHECK_MARKERS` matched markers visible
    -- even with an enormous single-marker shift that would trivially
    exceed the tolerance on its own -- must still read as 'cannot
    verify,' never as drift. Exercises the same guard as
    ``test_compute_drift_cm_returns_none_below_the_minimum_marker_count``
    in ``test_calibration.py``, through the real ``run_burst`` call path
    this time."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    assert MIN_DRIFT_CHECK_MARKERS > 1
    huge_shift = {1: (MAIN_PLAYFIELD_ARUCO_POSITIONS[1][0] + 50.0, MAIN_PLAYFIELD_ARUCO_POSITIONS[1][1])}
    mark_stale_calls: List[str] = []
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([_aruco_hits(huge_shift)]),
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: mark_stale_calls.append(slug),
    )
    cam = _camera("cal-camera", frames=[_blank_frame()])

    engine.run_burst("cal-camera", cam)

    assert mark_stale_calls == []


def test_drift_check_is_rate_limited_to_at_most_once_per_interval():
    """Three consecutive bursts, each with the same drift-beyond-tolerance
    marker shift, but an interval far longer than this test can possibly
    take to run -- only the first burst's check may actually run."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 5.0, true_y)
    hits = _aruco_hits(positions)
    mark_stale_calls: List[str] = []
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([hits, hits, hits]),
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: mark_stale_calls.append(slug),
        drift_check_interval_s=1000.0,
    )
    cam = _camera("cal-camera", frames=[_blank_frame() for _ in range(3)])

    engine.run_burst("cal-camera", cam)
    engine.run_burst("cal-camera", cam)
    engine.run_burst("cal-camera", cam)

    assert mark_stale_calls == ["cal-camera"]


def test_drift_check_runs_again_once_the_interval_elapses():
    """The inverse of the rate-limit test above: a zero-second interval
    means every burst is due for its own check -- proves the interval is
    a genuine, honoured parameter, not a one-shot latch."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 5.0, true_y)
    hits = _aruco_hits(positions)
    mark_stale_calls: List[str] = []
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([hits, hits]),
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: mark_stale_calls.append(slug),
        drift_check_interval_s=0.0,
    )
    cam = _camera("cal-camera", frames=[_blank_frame(), _blank_frame()])

    engine.run_burst("cal-camera", cam)
    engine.run_burst("cal-camera", cam)

    assert mark_stale_calls == ["cal-camera", "cal-camera"]


def test_drift_constants_default_from_the_calibration_module():
    """``DetectionEngine``'s own defaults are the stakeholder-confirmed
    module constants, not independently hardcoded numbers -- a change to
    one must change the other, never silently drift apart."""
    engine = DetectionEngine()
    assert engine._drift_tolerance_cm == DRIFT_TOLERANCE_CM  # noqa: SLF001 - internal, pinning the default
    assert engine._drift_check_interval_s == DRIFT_CHECK_INTERVAL_S  # noqa: SLF001


def test_drift_check_reuses_the_burst_own_detections_no_extra_detect_call():
    """The idle-efficiency budget's other half: even when a drift check
    genuinely runs (a calibrated camera, due for its check), it triggers
    no independent frame read or detection pass -- exactly one
    ``detect()`` call per burst, drift check included."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    hits = _aruco_hits(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    counting = _CountingDetector(hits=hits)
    engine = DetectionEngine(
        detector_factory=lambda: counting,
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: None,
    )
    cam = _camera("cal-camera", frames=[_blank_frame()])

    engine.run_burst("cal-camera", cam)

    assert counting.calls == 1


def test_uncalibrated_camera_never_calls_playfield_lookup_or_mark_stale():
    """A camera with no calibration incurs zero extra work from the
    drift-check machinery -- ``playfield_lookup``/``mark_stale`` are
    never even consulted, matching the idle-efficiency budget's "a
    camera with no calibration... incurs zero extra detection work"
    acceptance criterion."""
    playfield_calls: List[str] = []
    mark_stale_calls: List[str] = []

    def playfield_lookup(name: str) -> None:
        playfield_calls.append(name)
        return None

    engine = DetectionEngine(
        detector_factory=lambda: _CountingDetector(),
        calibration_lookup=lambda slug: None,
        playfield_lookup=playfield_lookup,
        mark_stale=lambda slug: mark_stale_calls.append(slug),
    )
    cam = _camera("cam-uncalibrated", frames=[_blank_frame()])

    engine.run_burst("cam-uncalibrated", cam)

    assert playfield_calls == []
    assert mark_stale_calls == []


def test_never_queried_camera_incurs_zero_drift_check_work():
    """No ``run_burst`` call at all -> the drift-check machinery never
    runs, trivially -- mirrors ``test_idle_engine_runs_zero_detections``
    above for the drift check specifically."""
    playfield_calls: List[str] = []
    DetectionEngine(playfield_lookup=lambda name: (playfield_calls.append(name), None)[1])
    assert playfield_calls == []


def test_default_playfield_lookup_and_mark_stale_are_inert():
    """The constructor defaults (module docstring) -- pinned directly,
    the same way :func:`test_default_calibration_lookup_is_always_none`
    pins its own default."""
    from aprilcam.daemon.detection import _default_mark_stale, _default_playfield_lookup

    assert _default_playfield_lookup("main-playfield") is None
    assert _default_mark_stale("any-slug") is None


def test_tag_frame_calibration_stale_reflects_a_just_flipped_calibration():
    """``TagFrame.calibration_stale`` (ticket 023-007's other wiring
    half) reflects the *post*-drift-check calibration state on the same
    burst that found the drift, not only on a subsequent call --
    ``run_burst`` re-fetches ``calibration_lookup`` after the drift
    check runs, precisely so a mid-burst mark-stale is visible
    immediately."""
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 5.0, true_y)
    calibrations: Dict[str, Calibration] = {"cal-camera": calibration}

    def calibration_lookup(slug: str) -> Optional[Calibration]:
        return calibrations.get(slug)

    def mark_stale(slug: str) -> None:
        calibrations[slug] = replace(calibrations[slug], stale=True)

    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([_aruco_hits(positions)]),
        calibration_lookup=calibration_lookup,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=mark_stale,
    )
    cam = _camera("cal-camera", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cal-camera", cam)

    assert tag_frame.calibration_stale is True
    assert calibrations["cal-camera"].stale is True


def test_tag_frame_calibration_stale_is_false_when_nothing_drifted():
    calibration = _fixture_calibration("cal-camera")
    definition = main_playfield_definition()
    engine = DetectionEngine(
        detector_factory=lambda: _ScriptedDetector([_aruco_hits(MAIN_PLAYFIELD_ARUCO_POSITIONS)]),
        calibration_lookup=lambda slug: calibration,
        playfield_lookup=lambda name: definition if name == calibration.playfield else None,
        mark_stale=lambda slug: None,
    )
    cam = _camera("cal-camera", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cal-camera", cam)

    assert tag_frame.calibration_stale is False


def test_tag_frame_calibration_stale_is_false_for_an_uncalibrated_camera():
    engine = DetectionEngine(detector_factory=lambda: _CountingDetector())
    cam = _camera("cam-n", frames=[_blank_frame()])

    tag_frame = engine.run_burst("cam-n", cam)

    assert tag_frame.calibration_stale is False
