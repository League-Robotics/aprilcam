"""Tests for ``aprilcam.calibration.calibration`` (ticket 022-003).

Fully headless: no daemon, no camera hardware. ``Calibrator`` is
exercised directly against synthetic BGR frames with real ArUco 4x4
markers rendered onto them via ``cv2.aruco.generateImageMarker`` (the
same primitive ``tests/aprilcam/vision/_tag_fixtures.py`` uses for
sprint 021's detector tests) at pixel positions computed from a known,
hand-chosen synthetic camera model -- so every test here runs *real*
detection (``vision/detector.py``) and a *real* homography solve
(OpenCV's DLT via ``cv2.findHomography``), not a mocked pipeline.

``MAIN_PLAYFIELD_ARUCO_POSITIONS``/``main_playfield_definition``/
``render_playfield_frame`` are exported (no leading underscore) for
``tests/aprilcam/daemon/test_service.py`` to reuse -- the same
cross-module-fixture convention that module's own docstring already
follows for ``FakeBackend`` (imported from
``tests.aprilcam.camera.test_camera``).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration import calibration as calibration_module
from aprilcam.calibration import distortion, geometry
from aprilcam.calibration.calibration import (
    DRIFT_CHECK_INTERVAL_S,
    DRIFT_TOLERANCE_CM,
    MAX_REPROJECTION_ERROR_CM,
    MIN_DRIFT_CHECK_MARKERS,
    MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE,
    MIN_ELEVATED_Z_SPREAD_CM,
    MIN_SURVEYED_INTRINSICS_POINTS,
    Calibration,
    Calibrator,
    _estimate_intrinsics,
    _held_out_split,
    _sanity_check_intrinsics,
    compute_drift_cm,
)
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.errors import NoPlayfieldLinked
from aprilcam.types import PixelPoint, TagFamily, WorldPoint
from tests.aprilcam.vision._tag_fixtures import render_marker

__all__ = [
    "MAIN_PLAYFIELD_ARUCO_POSITIONS",
    "main_playfield_definition",
    "main_playfield_definition_with_april_tag",
    "render_playfield_frame",
    "render_projective_frame",
    "standard_pixel_for_world",
]

# ---------------------------------------------------------------------------
# Synthetic camera + playfield fixtures
# ---------------------------------------------------------------------------

#: main-playfield.json's real 10 surveyed ArUco positions (cm,
#: A1-centred, +x east +y north) -- perimeter 1-8, interior 9/10.
#: Copied from ``config/aprilcam-old/playfields/main-playfield.json``
#: rather than loaded from disk, so these tests do not depend on that
#: file's path or on ``config/loader.py`` parsing succeeding.
MAIN_PLAYFIELD_ARUCO_POSITIONS: Dict[int, Tuple[float, float]] = {
    1: (-67.0, 44.65),
    2: (0.0, 44.65),
    3: (67.0, 44.65),
    4: (67.0, 0.0),
    5: (67.0, -44.65),
    6: (0.0, -44.65),
    7: (-67.0, -44.65),
    8: (-67.0, 0.0),
    9: (-35.0, 0.0),
    10: (35.0, 0.0),
}

#: Canvas size (width, height) every rendered synthetic frame uses --
#: large enough to hold all 10 main-playfield marker positions (see
#: ``standard_pixel_for_world``) with real quiet-zone margins between
#: neighbouring markers.
CANVAS_SIZE = (900, 700)

#: The synthetic camera's own pixel<->world scale/origin -- a plain
#: top-down mount: pixel-x increases with world-x (east), pixel-y
#: *decreases* with world-y (a larger world-y/north sits higher in the
#: image, since image rows grow downward). This is fixture ground
#: truth for the test images below, not production code -- production
#: code never assumes a scale or origin; it solves for whatever
#: ``cv2.findHomography`` derives from the correspondences these
#: fixtures provide.
SCALE_PX_PER_CM = 3.0
ORIGIN_PX = (450.0, 350.0)

_ARUCO_DICT = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)

PixelForWorld = Callable[[float, float], Tuple[float, float]]


def standard_pixel_for_world(x: float, y: float) -> Tuple[float, float]:
    """The standard synthetic top-down, north-up camera mount."""
    cx, cy = ORIGIN_PX
    return (cx + x * SCALE_PX_PER_CM, cy - y * SCALE_PX_PER_CM)


def inverted_pixel_for_world(x: float, y: float) -> Tuple[float, float]:
    """A deliberately non-standard mount: pixel-y increases *with*
    world-y instead of against it -- see ``test_to_world_...`` in
    ``test_geometry.py``'s "no hardcoded y-flip" test for why this
    variant exists. Used here to prove the same invariant survives a
    real detect-and-solve pipeline, not just hand-built matrices.
    """
    cx, cy = ORIGIN_PX
    return (cx + x * SCALE_PX_PER_CM, cy + y * SCALE_PX_PER_CM)


def render_playfield_frame(
    marker_world_positions: Dict[int, Tuple[float, float]],
    *,
    pixel_for_world: PixelForWorld = standard_pixel_for_world,
    marker_size: int = 50,
    canvas_size: Tuple[int, int] = CANVAS_SIZE,
) -> np.ndarray:
    """A synthetic BGR frame with one ArUco 4x4 marker per
    ``marker_world_positions`` entry, each centred at
    ``pixel_for_world(x, y)``. A plain white canvas supplies the quiet
    zone every marker needs -- as long as neighbouring markers do not
    overlap (true for every layout these tests use), no explicit
    per-marker border is needed."""
    width, height = canvas_size
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    half = marker_size // 2
    for tag_id, (wx, wy) in marker_world_positions.items():
        px, py = pixel_for_world(wx, wy)
        marker = cv.aruco.generateImageMarker(_ARUCO_DICT, tag_id, marker_size)
        marker_bgr = cv.cvtColor(marker, cv.COLOR_GRAY2BGR)
        x0, y0 = int(round(px - half)), int(round(py - half))
        canvas[y0 : y0 + marker_size, x0 : x0 + marker_size] = marker_bgr
    return canvas


def main_playfield_definition(name: str = "main-playfield") -> PlayfieldDefinition:
    """A real (not toy) ``PlayfieldDefinition`` matching
    ``main-playfield.json``'s actual 10 surveyed ArUco positions."""
    return PlayfieldDefinition(
        name=name,
        width_cm=134.3,
        height_cm=89.3,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"aruco-{i}", x=x, y=y, tag_id=i)
            for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()
        ),
    )


def main_playfield_definition_with_april_tag(name: str = "main-playfield") -> PlayfieldDefinition:
    """:func:`main_playfield_definition` plus ``main-playfield.json``'s
    real AprilTag 1 (field centre) -- the full "10 ArUco + AprilTag 1"
    ground-truth set sprint.md's own SUC-002 describes (sprint 030
    ticket 002)."""
    base = main_playfield_definition(name)
    return PlayfieldDefinition(
        name=base.name,
        width_cm=base.width_cm,
        height_cm=base.height_cm,
        aruco_tags=base.aruco_tags,
        april_tags=(PlayfieldFeatureEntry(slug="apriltag-center-a1", x=0.0, y=0.0, tag_id=1),),
    )


def _calibrator_for(
    definition: PlayfieldDefinition,
    frames_holder: Dict[str, List[np.ndarray]],
    *,
    camera_slug: str = "test-camera",
    playfield_name: str = "main-playfield",
    max_reprojection_error_cm: float = MAX_REPROJECTION_ERROR_CM,
) -> Calibrator:
    """A ``Calibrator`` wired against in-memory config/playfield stores
    and a frame source that reads from *frames_holder* -- a plain
    ``dict`` so a test can swap ``frames_holder["frames"]`` between
    calls (e.g. the "failed retry" test below) without touching
    ``Calibrator`` internals. ``max_reprojection_error_cm`` defaults to
    the real module constant rather than being silently unset, so a
    test that does not care about the threshold still exercises the
    real default (ticket 023-006's "no regression" acceptance
    criterion); tests below that *do* care override it explicitly."""
    camera_configs = {camera_slug: CameraConfig(name=camera_slug, playfield=playfield_name, settings={})}
    playfields = {playfield_name: definition}

    def capture_frames(slug: str) -> List[np.ndarray]:
        return frames_holder["frames"]

    return Calibrator(
        get_camera_config=camera_configs.get,
        get_playfield=playfields.get,
        capture_frames=capture_frames,
        max_reprojection_error_cm=max_reprojection_error_cm,
    )


# ---------------------------------------------------------------------------
# Success / all-or-nothing failure (UC-04.S-1, UC-04.S-2)
# ---------------------------------------------------------------------------


def test_calibrate_with_all_ten_required_aruco_ids_succeeds():
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    calibrator = _calibrator_for(definition, {"frames": [frame, frame, frame]})

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True
    assert result.matched_ids == tuple(range(1, 11))
    assert result.missing_ids == ()
    assert result.reprojection_error_cm is not None
    # A noise-free synthetic affine camera should fit essentially
    # exactly -- a large error here would mean the solve or the
    # detection is subtly wrong, not fixture jitter.
    assert result.reprojection_error_cm < 1.0

    calibration = calibrator.get_calibration("test-camera")
    assert calibration is not None
    assert calibration.playfield == "main-playfield"
    assert calibration.matched_ids == tuple(range(1, 11))
    assert calibration.stale is False


def test_calibrate_with_one_missing_aruco_id_fails_naming_exactly_that_id():
    """UC-04.S-2: ArUco 6 occluded -> failure naming exactly (6,), never
    a count, never '9 of 10'."""
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    del positions[6]
    frame = render_playfield_frame(positions)
    calibrator = _calibrator_for(definition, {"frames": [frame]})

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is False
    assert result.missing_ids == (6,)
    assert result.matched_ids == (1, 2, 3, 4, 5, 7, 8, 9, 10)
    assert calibrator.get_calibration("test-camera") is None


def test_calibrate_with_camera_badly_aimed_names_every_missing_id():
    """UC-04.S-3: only two of ten fiducials visible -> every missing id
    named, not merely a count."""
    definition = main_playfield_definition()
    positions = {1: MAIN_PLAYFIELD_ARUCO_POSITIONS[1], 2: MAIN_PLAYFIELD_ARUCO_POSITIONS[2]}
    frame = render_playfield_frame(positions)
    calibrator = _calibrator_for(definition, {"frames": [frame]})

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is False
    assert result.missing_ids == (3, 4, 5, 6, 7, 8, 9, 10)


def test_a_failed_retry_leaves_the_prior_successful_calibration_untouched():
    """UC-04.S-2 / UC-15.S-3: a failure does not clear or overwrite a
    previously-good calibration."""
    definition = main_playfield_definition()
    good_frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    frames_holder = {"frames": [good_frame]}
    calibrator = _calibrator_for(definition, frames_holder)

    good_result = calibrator.calibrate("test-camera")
    assert good_result.succeeded is True
    first_calibration = calibrator.get_calibration("test-camera")

    partial_positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    del partial_positions[3]
    frames_holder["frames"] = [render_playfield_frame(partial_positions)]

    retry_result = calibrator.calibrate("test-camera")
    assert retry_result.succeeded is False
    assert retry_result.missing_ids == (3,)

    assert calibrator.get_calibration("test-camera") == first_calibration


def test_multi_frame_averaging_uses_every_frame_that_saw_a_given_marker():
    """A marker detected in only some of the captured frames still
    contributes to that id's averaged pixel centre -- mirrors
    ``archive/.../homography.py``'s ``detect_all_tags`` accumulate-then-
    mean behaviour. Two frames: one with all 10 markers at their
    standard positions, one with ArUco 5 shifted 6cm east. The averaged
    position for id 5 should land between the two, not merely at
    whichever frame happened to run first or last.
    """
    definition = main_playfield_definition()
    frame_a = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    shifted = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    shifted_x, shifted_y = shifted[5]
    shifted[5] = (shifted_x + 6.0, shifted_y)
    frame_b = render_playfield_frame(shifted)
    calibrator = _calibrator_for(definition, {"frames": [frame_a, frame_b]})

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True
    # Averaging genuinely happened: a small reprojection error, but not
    # zero, since id 5's two observations disagree by design.
    assert result.reprojection_error_cm is not None
    assert result.reprojection_error_cm > 0.01


# ---------------------------------------------------------------------------
# NoPlayfieldLinked (UC-15.S-2) -- precondition failure, zero frame captures
# ---------------------------------------------------------------------------


def test_calibrate_with_no_camera_config_at_all_raises_before_capturing_frames():
    capture_calls: List[str] = []

    def capture_frames(slug: str) -> List[np.ndarray]:
        capture_calls.append(slug)
        return []

    calibrator = Calibrator(
        get_camera_config=lambda slug: None,
        get_playfield=lambda name: None,
        capture_frames=capture_frames,
    )

    with pytest.raises(NoPlayfieldLinked) as excinfo:
        calibrator.calibrate("hd-usb-camera")

    assert excinfo.value.camera == "hd-usb-camera"
    assert capture_calls == []


def test_calibrate_with_explicit_none_playfield_raises_before_capturing_frames():
    capture_calls: List[str] = []

    def capture_frames(slug: str) -> List[np.ndarray]:
        capture_calls.append(slug)
        return []

    calibrator = Calibrator(
        get_camera_config=lambda slug: CameraConfig(name=slug, playfield=None, settings={}),
        get_playfield=lambda name: None,
        capture_frames=capture_frames,
    )

    with pytest.raises(NoPlayfieldLinked):
        calibrator.calibrate("hd-usb-camera")

    assert capture_calls == []


def test_calibrate_naming_a_playfield_the_daemon_does_not_hold_raises_before_capturing_frames():
    """A camera config names a playfield by name, but the daemon
    currently holds no definition of that name (e.g. after a restart
    that re-pushed the camera config but not the playfield) -- treated
    as the same "nothing to calibrate against" precondition."""
    capture_calls: List[str] = []

    def capture_frames(slug: str) -> List[np.ndarray]:
        capture_calls.append(slug)
        return []

    calibrator = Calibrator(
        get_camera_config=lambda slug: CameraConfig(
            name=slug, playfield="ghost-field", settings={}
        ),
        get_playfield=lambda name: None,
        capture_frames=capture_frames,
    )

    with pytest.raises(NoPlayfieldLinked):
        calibrator.calibrate("arducam")

    assert capture_calls == []


# ---------------------------------------------------------------------------
# Degenerate geometry rejection (UC-04's "reject rather than store a bad
# homography")
# ---------------------------------------------------------------------------


def test_calibrate_rejects_collinear_marker_geometry_naming_the_problem():
    positions = {1: (-60.0, 0.0), 2: (-20.0, 0.0), 3: (20.0, 0.0), 4: (60.0, 0.0)}
    definition = PlayfieldDefinition(
        name="degenerate-field",
        width_cm=200.0,
        height_cm=200.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"a{i}", x=x, y=y, tag_id=i) for i, (x, y) in positions.items()
        ),
    )
    frame = render_playfield_frame(positions, canvas_size=(900, 900))
    calibrator = _calibrator_for(definition, {"frames": [frame]}, playfield_name="degenerate-field")

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is False
    assert result.missing_ids == ()  # every id WAS detected -- it's a geometry problem, not a coverage one
    assert result.reason is not None
    assert "collinear" in result.reason.lower()
    assert calibrator.get_calibration("test-camera") is None


def test_calibrate_rejects_too_few_correspondence_points_naming_the_problem():
    """Fewer than 4 required ids can never support a homography solve
    at all, regardless of geometry -- named distinctly from
    collinearity."""
    positions = {1: (-20.0, 0.0), 2: (20.0, 20.0), 3: (0.0, -20.0)}
    definition = PlayfieldDefinition(
        name="tiny-field",
        width_cm=100.0,
        height_cm=100.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"a{i}", x=x, y=y, tag_id=i) for i, (x, y) in positions.items()
        ),
    )
    frame = render_playfield_frame(positions, canvas_size=(700, 700))
    calibrator = _calibrator_for(definition, {"frames": [frame]}, playfield_name="tiny-field")

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is False
    assert result.missing_ids == ()
    assert result.reason is not None
    assert "at least 4" in result.reason


# ---------------------------------------------------------------------------
# Reprojection-error acceptance threshold (ticket 023-006, UC-04's open
# question resolved -- SUC-005): complete (every required id found) but
# sloppy is still rejected, distinctly from missing-ids/degenerate-
# geometry failures above.
# ---------------------------------------------------------------------------


def test_calibrate_rejects_a_complete_solve_whose_reprojection_error_exceeds_the_threshold():
    """A synthetic complete-but-degenerate point set: every one of the
    ten required ArUco ids is detected (completeness holds -- UC-04's
    all-or-nothing rule is satisfied), but ArUco 5 is rendered 20 cm
    away from its surveyed position, dragging the homography fit's RMS
    reprojection error well past ``MAX_REPROJECTION_ERROR_CM``. The
    rejection must name the measured error and the threshold via
    ``reason`` -- not ``missing_ids``, since nothing is missing -- and
    must store nothing."""
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 20.0, true_y)
    frame = render_playfield_frame(positions)
    calibrator = _calibrator_for(definition, {"frames": [frame]})

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is False
    assert result.missing_ids == ()  # every id WAS found -- a threshold failure, not a coverage one
    assert result.matched_ids == tuple(range(1, 11))
    assert result.reprojection_error_cm is not None
    assert result.reprojection_error_cm > MAX_REPROJECTION_ERROR_CM
    assert result.reason is not None
    assert f"{result.reprojection_error_cm:.3f}" in result.reason
    assert f"{MAX_REPROJECTION_ERROR_CM:.3f}" in result.reason
    assert calibrator.get_calibration("test-camera") is None


def test_calibrate_accepts_a_solve_at_or_below_the_threshold_exactly_as_before():
    """A good, complete solve (the same fixture
    ``test_calibrate_with_all_ten_required_aruco_ids_succeeds`` uses,
    whose noise-free reprojection error is comfortably under 1.0 cm)
    succeeds exactly as it did before this ticket -- no regression on
    the ordinary case."""
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    calibrator = _calibrator_for(definition, {"frames": [frame, frame, frame]})

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True
    assert result.reprojection_error_cm is not None
    assert result.reprojection_error_cm <= MAX_REPROJECTION_ERROR_CM
    assert calibrator.get_calibration("test-camera") is not None


def test_calibrate_accepts_a_solve_whose_error_exactly_equals_the_threshold():
    """"At or below the threshold" is inclusive at the boundary --
    proven by pointing the threshold exactly at a real measured error
    rather than trying to hand-construct an exact-equality rms."""
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    probe = _calibrator_for(definition, {"frames": [frame]})
    probe_result = probe.calibrate("test-camera")
    assert probe_result.succeeded is True
    measured = probe_result.reprojection_error_cm
    assert measured is not None

    calibrator = _calibrator_for(
        definition, {"frames": [frame]}, max_reprojection_error_cm=measured
    )

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True
    assert result.reprojection_error_cm == pytest.approx(measured)


def test_a_threshold_rejection_leaves_the_prior_successful_calibration_untouched():
    """UC-04/SUC-005: a failed threshold check does not disturb an
    existing good calibration -- mirrors
    ``test_a_failed_retry_leaves_the_prior_successful_calibration_untouched``
    above, but for the new rejection branch specifically rather than
    the missing-ids one."""
    definition = main_playfield_definition()
    good_frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    frames_holder = {"frames": [good_frame]}
    calibrator = _calibrator_for(definition, frames_holder)

    good_result = calibrator.calibrate("test-camera")
    assert good_result.succeeded is True
    first_calibration = calibrator.get_calibration("test-camera")

    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 20.0, true_y)
    frames_holder["frames"] = [render_playfield_frame(positions)]

    retry_result = calibrator.calibrate("test-camera")
    assert retry_result.succeeded is False
    assert retry_result.missing_ids == ()
    assert retry_result.reason is not None

    assert calibrator.get_calibration("test-camera") == first_calibration


def test_max_reprojection_error_cm_is_genuinely_overridable_to_reject_a_normally_good_solve():
    """Lowering ``max_reprojection_error_cm`` well below what an
    otherwise-good solve measures must reject it -- proving the
    threshold used is the instance's own override, not a hardcoded
    module constant baked into the comparison."""
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    calibrator = _calibrator_for(
        definition, {"frames": [frame]}, max_reprojection_error_cm=1e-6
    )

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is False
    assert result.missing_ids == ()
    assert result.reason is not None
    assert calibrator.get_calibration("test-camera") is None


def test_max_reprojection_error_cm_is_genuinely_overridable_to_accept_a_normally_rejected_solve():
    """The inverse direction: raising ``max_reprojection_error_cm``
    above what the default (1.0 cm) would reject lets the same
    otherwise-degenerate solve from
    ``test_calibrate_rejects_a_complete_solve_whose_reprojection_error_exceeds_the_threshold``
    succeed -- confirming the override is a real per-instance ceiling,
    not a one-directional safety clamp."""
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 20.0, true_y)
    frame = render_playfield_frame(positions)
    calibrator = _calibrator_for(
        definition, {"frames": [frame]}, max_reprojection_error_cm=10.0
    )

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True
    assert result.reprojection_error_cm is not None
    assert result.reprojection_error_cm > MAX_REPROJECTION_ERROR_CM
    assert calibrator.get_calibration("test-camera") is not None


# ---------------------------------------------------------------------------
# Geometry convention suite, at fixture level (permanent, never relaxed):
# east -> larger x, north -> larger y, origin -- exercised through the real
# detect + solve pipeline, not hand-built matrices (test_geometry.py's job).
# ---------------------------------------------------------------------------


def _calibrated(pixel_for_world: PixelForWorld = standard_pixel_for_world) -> Calibration:
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS, pixel_for_world=pixel_for_world)
    calibrator = _calibrator_for(definition, {"frames": [frame]})
    result = calibrator.calibrate("test-camera")
    assert result.succeeded is True
    calibration = calibrator.get_calibration("test-camera")
    assert calibration is not None
    return calibration


def test_a_marker_surveyed_east_of_another_produces_a_larger_to_world_x():
    calibration = _calibrated()

    near = calibration.to_world(PixelPoint(*standard_pixel_for_world(10.0, 0.0)))
    further_east = calibration.to_world(PixelPoint(*standard_pixel_for_world(40.0, 0.0)))

    assert further_east.x > near.x


def test_a_marker_surveyed_north_of_another_produces_a_larger_to_world_y():
    calibration = _calibrated()

    near = calibration.to_world(PixelPoint(*standard_pixel_for_world(0.0, 10.0)))
    further_north = calibration.to_world(PixelPoint(*standard_pixel_for_world(0.0, 30.0)))

    assert further_north.y > near.y


def test_apriltag_1_field_centre_reports_world_origin_within_tolerance():
    """The ticket's own "single best correctness check": AprilTag 1
    sits at ``main-playfield``'s surveyed centre, (0, 0). Its pixel
    position (via the synthetic camera's own ground truth) is *not*
    one of the 10 ArUco correspondences the homography was fit from, so
    this is a genuine interpolation check, not a tautology."""
    calibration = _calibrated()

    origin = calibration.to_world(PixelPoint(*standard_pixel_for_world(0.0, 0.0)))

    assert origin.x == pytest.approx(0.0, abs=0.5)
    assert origin.y == pytest.approx(0.0, abs=0.5)


def test_geometry_convention_survives_a_non_standard_camera_mount():
    """The regression ``test_geometry.py`` makes with hand-built
    matrices, repeated here through the real detect-and-solve pipeline:
    an inverted-mount synthetic camera (pixel-y increases *with*
    world-y) must still report "further north -> larger world y" using
    *its own* ground truth -- proof that neither the homography solve
    nor ``Calibration.to_world`` bakes in an assumption about which way
    is up, only what the correspondences say.
    """
    calibration = _calibrated(pixel_for_world=inverted_pixel_for_world)

    near = calibration.to_world(PixelPoint(*inverted_pixel_for_world(0.0, 10.0)))
    further_north = calibration.to_world(PixelPoint(*inverted_pixel_for_world(0.0, 30.0)))

    assert further_north.y > near.y


def test_origin_shift_is_never_a_sign_inversion_on_either_axis():
    """Sweeps a small grid of held-out points and confirms both axes
    move in lock-step with their surveyed sign -- a single accidental
    ``x = -x`` or ``y = -y`` anywhere in the pipeline would flip
    exactly one of these two assertions, never both, which is what
    makes this check specific rather than a restatement of the two
    tests above."""
    calibration = _calibrated()

    west = calibration.to_world(PixelPoint(*standard_pixel_for_world(-20.0, 0.0)))
    east = calibration.to_world(PixelPoint(*standard_pixel_for_world(20.0, 0.0)))
    south = calibration.to_world(PixelPoint(*standard_pixel_for_world(0.0, -20.0)))
    north = calibration.to_world(PixelPoint(*standard_pixel_for_world(0.0, 20.0)))

    assert east.x > 0 > west.x
    assert north.y > 0 > south.y


# ---------------------------------------------------------------------------
# compute_drift_cm (ticket 023-007) -- the opportunistic staleness drift
# check's pure geometry, exercised directly against a real ``Calibration``
# from the fixtures above rather than through ``DetectionEngine``/
# ``DaemonService`` (those modules' own tests cover the wiring; this module
# covers the math and the "cannot verify" contract it must honour).
# ---------------------------------------------------------------------------


def _detected_at(positions: Dict[int, Tuple[float, float]]) -> Dict[int, PixelPoint]:
    """*positions* (world cm, id -> (x, y)) as a drift-check ``detected``
    dict -- pixel positions via the same synthetic camera mount
    ``_calibrated()`` solved against, simulating "the marker was detected
    here" for whatever world position is passed in (whether or not that
    matches the marker's real surveyed position -- the caller decides,
    to simulate either an unmoved camera or a bumped one)."""
    return {tag_id: PixelPoint(*standard_pixel_for_world(x, y)) for tag_id, (x, y) in positions.items()}


def test_drift_constants_match_the_stakeholder_confirmed_values():
    """Pins the sprint.md Decisions values directly, the same way
    ``test_ring_buffer_size_defaults_to_v1s_300`` pins its own constant --
    a silent change to either number is exactly the kind of regression a
    plain equality assertion catches immediately."""
    assert DRIFT_TOLERANCE_CM == 2.0
    assert DRIFT_CHECK_INTERVAL_S == 10.0
    assert MIN_DRIFT_CHECK_MARKERS == 3


def test_compute_drift_cm_is_near_zero_when_nothing_has_moved():
    """An unmoved camera: every matched marker re-detected at exactly
    its surveyed position (the same synthetic camera mount the
    calibration was solved against) should reproject to within the
    homography's own noise-free fit error, comfortably under the 2.0 cm
    tolerance."""
    calibration = _calibrated()
    definition = main_playfield_definition()
    detected = _detected_at(MAIN_PLAYFIELD_ARUCO_POSITIONS)

    drift = compute_drift_cm(calibration, definition, detected)

    assert drift is not None
    assert drift < 0.5


def test_compute_drift_cm_flags_a_shift_beyond_tolerance():
    """One matched marker detected 5 cm from its surveyed position --
    well past the 2.0 cm tolerance -- simulating a bumped camera (or,
    equivalently for this pure-geometry function, a marker that
    genuinely moved). The max, not an average, decides the outcome, so
    a single bad marker among nine good ones is not diluted away."""
    calibration = _calibrated()
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 5.0, true_y)
    detected = _detected_at(positions)

    drift = compute_drift_cm(calibration, definition, detected)

    assert drift is not None
    assert drift > DRIFT_TOLERANCE_CM


def test_compute_drift_cm_within_tolerance_is_not_flagged():
    """A small, sub-tolerance shift (0.5 cm -- comfortably inside the
    2.0 cm tolerance, itself ~11x the observed real noise floor) must
    not be reported as drift -- the false-positive guard on ordinary
    detection jitter."""
    calibration = _calibrated()
    definition = main_playfield_definition()
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    true_x, true_y = positions[5]
    positions[5] = (true_x + 0.5, true_y)
    detected = _detected_at(positions)

    drift = compute_drift_cm(calibration, definition, detected)

    assert drift is not None
    assert drift < DRIFT_TOLERANCE_CM


def test_compute_drift_cm_returns_none_when_no_markers_are_visible():
    """Darkness, a covered field, a lens cap -- zero matched markers
    detected must read as "cannot verify," never as "confirmed no
    drift" (never ``0.0``)."""
    calibration = _calibrated()
    definition = main_playfield_definition()

    drift = compute_drift_cm(calibration, definition, {})

    assert drift is None


def test_compute_drift_cm_returns_none_below_the_minimum_marker_count():
    """Fewer than ``MIN_DRIFT_CHECK_MARKERS`` matched markers visible --
    even if every one of them individually shows a huge apparent shift --
    is still "cannot verify," not evidence of drift. Proven with a
    deliberately enormous single-marker shift that would trivially
    exceed the tolerance if it were allowed to count on its own."""
    calibration = _calibrated()
    definition = main_playfield_definition()
    assert MIN_DRIFT_CHECK_MARKERS > 1
    huge_shift = {1: (MAIN_PLAYFIELD_ARUCO_POSITIONS[1][0] + 50.0, MAIN_PLAYFIELD_ARUCO_POSITIONS[1][1])}
    detected = _detected_at(huge_shift)
    assert len(detected) < MIN_DRIFT_CHECK_MARKERS

    drift = compute_drift_cm(calibration, definition, detected)

    assert drift is None


def test_compute_drift_cm_exactly_at_the_minimum_marker_count_does_verify():
    """The boundary case: exactly ``MIN_DRIFT_CHECK_MARKERS`` matched
    markers visible is enough to draw a conclusion -- the guard is "fewer
    than," not "fewer than or equal to.\""""
    calibration = _calibrated()
    definition = main_playfield_definition()
    ids = list(calibration.matched_ids)[:MIN_DRIFT_CHECK_MARKERS]
    positions = {tag_id: MAIN_PLAYFIELD_ARUCO_POSITIONS[tag_id] for tag_id in ids}
    detected = _detected_at(positions)
    assert len(detected) == MIN_DRIFT_CHECK_MARKERS

    drift = compute_drift_cm(calibration, definition, detected)

    assert drift is not None


def test_compute_drift_cm_ignores_a_detected_id_the_calibration_never_matched():
    """A detected id outside ``calibration.matched_ids`` (a stray ArUco
    marker in frame, or an id that stopped being part of the playfield
    definition) is simply not counted towards *visible* -- it must not
    let an otherwise-insufficient reading sneak past the minimum-marker
    guard."""
    calibration = _calibrated()
    definition = main_playfield_definition()
    detected = _detected_at(
        {
            1: MAIN_PLAYFIELD_ARUCO_POSITIONS[1],
            2: MAIN_PLAYFIELD_ARUCO_POSITIONS[2],
        }
    )
    detected[999] = PixelPoint(*standard_pixel_for_world(0.0, 0.0))
    assert 999 not in calibration.matched_ids

    drift = compute_drift_cm(calibration, definition, detected)

    assert drift is None  # still only 2 real matched markers -- below the minimum


# ---------------------------------------------------------------------------
# Lens distortion (sprint 030 ticket 001): ``Calibration.camera_matrix``/
# ``dist_coeffs`` storage round-trip, ``to_world``/``to_pixel`` applying
# them, and ``Calibrator.calibrate``'s undistort-before-solve +
# carry-forward mechanics. This ticket lands storage/application only --
# ticket 002 is what lets an operator *estimate* real intrinsics, so
# every ``Calibration`` here that "already has" intrinsics is hand-built.
# ---------------------------------------------------------------------------

#: A synthetic intrinsic matrix/distortion vector used only by this
#: section's tests. Deliberately a strong-enough model (a small focal
#: length relative to ``CANVAS_SIZE``) that the pixel shift it produces
#: at main-playfield's own marker positions is many pixels -- large
#: enough that a test comparing "corrected" against "known good" would
#: fail loudly if the undistort-before-solve step were skipped or
#: mis-ordered, not a sub-pixel effect detection noise could hide.
_SYNTHETIC_CAMERA_MATRIX: Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]] = (
    (400.0, 0.0, 450.0),
    (0.0, 400.0, 350.0),
    (0.0, 0.0, 1.0),
)
_SYNTHETIC_DIST_COEFFS: Tuple[float, float, float, float, float] = (-0.5, 0.0, 0.0, 0.0, 0.0)


def _forward_distort_pixel(px: float, py: float) -> Tuple[float, float]:
    """OpenCV's standard forward radial/tangential distortion formula
    (``docs.opencv.org``'s own camera-calibration model), applied
    directly against :data:`_SYNTHETIC_CAMERA_MATRIX`/
    :data:`_SYNTHETIC_DIST_COEFFS` -- deliberately **independent** of
    ``distortion.distort_point``'s own fixed-point-iteration
    implementation, so the tests below check against an
    independently-computed ground truth rather than merely round-
    tripping through the same code under test. Generates the *known,
    ground-truth-distorted* marker render positions the ticket's own
    acceptance criterion asks for ("known ground-truth distortion
    applied to synthetic points")."""
    fx, _ox, cx = _SYNTHETIC_CAMERA_MATRIX[0]
    _oy, fy, cy = _SYNTHETIC_CAMERA_MATRIX[1]
    k1, k2, p1, p2, k3 = _SYNTHETIC_DIST_COEFFS
    x = (px - cx) / fx
    y = (py - cy) / fy
    r2 = x * x + y * y
    radial = 1 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    x_d = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    y_d = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    return (fx * x_d + cx, fy * y_d + cy)


def _distorted_pixel_for_world(x: float, y: float) -> Tuple[float, float]:
    """``standard_pixel_for_world`` with :func:`_forward_distort_pixel`
    additionally applied -- what this fixture's camera would actually
    render a marker at, if its lens really distorted like this. A
    ``PixelForWorld`` callable, passable to ``render_playfield_frame``
    exactly like ``standard_pixel_for_world``/``inverted_pixel_for_world``
    already are."""
    return _forward_distort_pixel(*standard_pixel_for_world(x, y))


def _seed_prior_calibration(calibrator: Calibrator, camera_slug: str, calibration: Calibration) -> None:
    """Directly seed *calibrator*'s internal calibration cache with
    *calibration*. There is no public setter -- a fresh ``Calibrator``
    only ever populates its cache via a successful :meth:`Calibrator.
    calibrate` call -- and this ticket's own acceptance criteria call
    for testing the undistort-before-solve/carry-forward mechanics
    against a *hand-constructed* prior ``Calibration`` (ticket 002 is
    what lets an operator produce one for real)."""
    calibrator._calibrations[camera_slug] = calibration


def _dummy_prior_calibration(
    *,
    camera_matrix: Tuple[Tuple[float, float, float], ...] | None = None,
    dist_coeffs: Tuple[float, ...] | None = None,
) -> Calibration:
    """A syntactically-valid but otherwise-arbitrary prior
    ``Calibration`` -- only its ``camera_matrix``/``dist_coeffs`` are
    read by :meth:`Calibrator.calibrate`'s carry-forward mechanic, so
    every other field here is a placeholder."""
    return Calibration(
        stale=False,
        calibrated_at=0.0,
        playfield="main-playfield",
        homography=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        matched_ids=(),
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
    )


# -- Calibration.to_dict/from_dict round trip -------------------------------


def test_to_dict_omits_camera_matrix_and_dist_coeffs_when_none():
    """Mirrors ``config.interfaces.CameraConfig.to_dict``'s identical
    treatment of its own optional ``camera_position`` (never a JSON
    ``null`` placeholder) -- a camera with no lens model produces
    exactly the same 6-key ``calibration.json`` shape this method
    produced before this ticket."""
    calibration = Calibration(
        stale=False,
        calibrated_at=123.0,
        playfield="main-playfield",
        homography=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        matched_ids=(1, 2, 3, 4),
        reprojection_error_cm=0.1,
    )

    data = calibration.to_dict()

    assert "camera_matrix" not in data
    assert "dist_coeffs" not in data
    assert set(data.keys()) == {
        "stale",
        "calibrated_at",
        "playfield",
        "homography",
        "matched_ids",
        "reprojection_error_cm",
    }


def test_to_dict_and_from_dict_round_trip_camera_matrix_and_dist_coeffs():
    calibration = Calibration(
        stale=False,
        calibrated_at=123.0,
        playfield="main-playfield",
        homography=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        matched_ids=(1, 2, 3, 4),
        reprojection_error_cm=0.1,
        camera_matrix=_SYNTHETIC_CAMERA_MATRIX,
        dist_coeffs=_SYNTHETIC_DIST_COEFFS,
    )

    data = calibration.to_dict()

    assert data["camera_matrix"] == [list(row) for row in _SYNTHETIC_CAMERA_MATRIX]
    assert data["dist_coeffs"] == list(_SYNTHETIC_DIST_COEFFS)

    restored = Calibration.from_dict(data)

    assert restored == calibration


def test_from_dict_loads_a_pre_sprint_fixture_with_no_intrinsics_keys():
    """A ``calibration.json`` written before this ticket (no
    ``camera_matrix``/``dist_coeffs`` keys at all -- the real shape
    ``daemon/service.py``'s ``_persist_calibration`` wrote for every
    camera prior to this sprint) loads successfully, both fields
    ``None`` -- backward compatibility, sprint.md's Migration Concerns
    ("no forced conversion, no schema version bump")."""
    pre_sprint_data = {
        "stale": False,
        "calibrated_at": 1700000000.0,
        "playfield": "main-playfield",
        "homography": [
            [0.138, -0.002, -87.6],
            [-0.003, -0.139, 52.7],
            [-3e-5, 1.9e-5, 1.0],
        ],
        "matched_ids": list(range(1, 11)),
        "reprojection_error_cm": 0.132,
    }
    assert "camera_matrix" not in pre_sprint_data
    assert "dist_coeffs" not in pre_sprint_data

    calibration = Calibration.from_dict(pre_sprint_data)

    assert calibration.camera_matrix is None
    assert calibration.dist_coeffs is None
    assert calibration.matched_ids == tuple(range(1, 11))


# -- Calibration.to_world/to_pixel applying the lens model -------------------


def test_to_world_and_to_pixel_are_unaffected_when_intrinsics_are_none():
    """The ticket's own "byte-for-byte identical to today's behaviour"
    acceptance criterion: with no lens model, ``to_world``/``to_pixel``
    do exactly what ``geometry.pixel_to_world``/``world_to_pixel``
    alone already do -- no undistort/redistort step runs at all."""
    homography = ((2.0, 0.0, -100.0), (0.0, -3.0, 150.0), (0.0, 0.0, 1.0))
    calibration = Calibration(
        stale=False,
        calibrated_at=0.0,
        playfield="main-playfield",
        homography=homography,
        matched_ids=(1, 2, 3, 4),
    )
    matrix = np.array(homography, dtype=np.float64)
    pixel = PixelPoint(x=10.0, y=5.0)

    world = calibration.to_world(pixel)
    assert world == geometry.pixel_to_world(matrix, pixel)

    pixel_back = calibration.to_pixel(world)
    assert pixel_back == geometry.world_to_pixel(matrix, world)


def test_to_world_undistorts_before_applying_the_homography_when_intrinsics_are_set():
    """Isolates the undistort step from the homography math: with an
    **identity** homography, ``to_world(pixel)`` reports exactly
    ``distortion.undistort_point(pixel, ...)``'s own output (interpreted
    as a world point), proving the undistort happens *before* the
    homography is applied, not skipped."""
    identity_homography = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    calibration = Calibration(
        stale=False,
        calibrated_at=0.0,
        playfield="main-playfield",
        homography=identity_homography,
        matched_ids=(),
        camera_matrix=_SYNTHETIC_CAMERA_MATRIX,
        dist_coeffs=_SYNTHETIC_DIST_COEFFS,
    )
    camera_matrix_array = np.array(_SYNTHETIC_CAMERA_MATRIX, dtype=np.float64)
    dist_coeffs_array = np.array(_SYNTHETIC_DIST_COEFFS, dtype=np.float64)
    distorted_pixel = PixelPoint(x=500.0, y=380.0)
    expected = distortion.undistort_point(distorted_pixel, camera_matrix_array, dist_coeffs_array)

    world = calibration.to_world(distorted_pixel)

    assert world.x == pytest.approx(expected.x)
    assert world.y == pytest.approx(expected.y)


def test_to_pixel_redistorts_after_applying_the_inverse_homography_when_intrinsics_are_set():
    """The ``to_pixel`` half of the isolation above: with an identity
    homography, ``to_pixel(world)`` reports exactly
    ``distortion.distort_point``'s own output for that same point,
    proving the redistort happens *after* the inverse homography."""
    identity_homography = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    calibration = Calibration(
        stale=False,
        calibrated_at=0.0,
        playfield="main-playfield",
        homography=identity_homography,
        matched_ids=(),
        camera_matrix=_SYNTHETIC_CAMERA_MATRIX,
        dist_coeffs=_SYNTHETIC_DIST_COEFFS,
    )
    camera_matrix_array = np.array(_SYNTHETIC_CAMERA_MATRIX, dtype=np.float64)
    dist_coeffs_array = np.array(_SYNTHETIC_DIST_COEFFS, dtype=np.float64)
    world = WorldPoint(x=500.0, y=380.0)
    expected = distortion.distort_point(
        PixelPoint(x=world.x, y=world.y), camera_matrix_array, dist_coeffs_array
    )

    pixel = calibration.to_pixel(world)

    assert pixel.x == pytest.approx(expected.x)
    assert pixel.y == pytest.approx(expected.y)


def test_to_pixel_of_to_world_round_trips_when_intrinsics_are_set():
    """The ticket's own explicit round-trip requirement, at the
    ``Calibration`` object level: ``to_world(to_pixel(w)) ~= w`` for a
    nontrivial homography *and* a nontrivial lens model together."""
    homography = ((0.2, 0.0, -60.0), (0.0, -0.2, 40.0), (0.0, 0.0, 1.0))
    calibration = Calibration(
        stale=False,
        calibrated_at=0.0,
        playfield="main-playfield",
        homography=homography,
        matched_ids=(1, 2, 3, 4),
        camera_matrix=_SYNTHETIC_CAMERA_MATRIX,
        dist_coeffs=_SYNTHETIC_DIST_COEFFS,
    )

    for wx, wy in [(-30.0, 20.0), (0.0, 0.0), (25.0, -15.0)]:
        world = WorldPoint(x=wx, y=wy)
        pixel = calibration.to_pixel(world)
        recovered = calibration.to_world(pixel)
        assert recovered.x == pytest.approx(world.x, abs=1e-3)
        assert recovered.y == pytest.approx(world.y, abs=1e-3)


# -- Calibrator.calibrate: undistort-before-solve + carry-forward -----------


def test_calibrate_with_no_prior_calibration_produces_no_intrinsics():
    """The default path (no prior ``Calibration`` for this camera slug,
    ticket 002's ``estimate_intrinsics`` not yet landed) must still
    produce exactly what it did before this ticket: no lens model at
    all."""
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    calibrator = _calibrator_for(definition, {"frames": [frame]})

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True
    calibration = calibrator.get_calibration("test-camera")
    assert calibration is not None
    assert calibration.camera_matrix is None
    assert calibration.dist_coeffs is None


def test_calibrate_undistorts_detected_pixels_before_solving_when_intrinsics_are_known():
    """The ticket's own homography-solve-order acceptance criterion: a
    homography solved from a *synthetically-distorted* marker-pixel set
    (rendered at positions produced by the independent forward
    distortion model :func:`_forward_distort_pixel`) matches the
    known-good homography solved directly from the *undistorted*
    points -- once a prior ``Calibration`` carrying the matching
    ``camera_matrix``/``dist_coeffs`` is present for this camera slug
    (this ticket's carry-forward mechanic, exercised here against a
    hand-constructed prior per the ticket's own testing note)."""
    definition = main_playfield_definition()
    distorted_frame = render_playfield_frame(
        MAIN_PLAYFIELD_ARUCO_POSITIONS, pixel_for_world=_distorted_pixel_for_world
    )
    calibrator = _calibrator_for(definition, {"frames": [distorted_frame]})
    _seed_prior_calibration(
        calibrator,
        "test-camera",
        _dummy_prior_calibration(
            camera_matrix=_SYNTHETIC_CAMERA_MATRIX, dist_coeffs=_SYNTHETIC_DIST_COEFFS
        ),
    )

    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True
    assert result.matched_ids == tuple(range(1, 11))
    # A healthy residual -- proof the homography and the RMS were both
    # computed from the *same* undistorted point set (v1's own earned
    # lesson, ``archive/.../calibration.py:1342-1346``): if the RMS
    # were instead measured against the still-distorted pixels, or the
    # homography solved from a different point set than the RMS, this
    # would be many cm, not a fraction of one -- the strong synthetic
    # distortion this fixture applies (see _SYNTHETIC_DIST_COEFFS) is
    # chosen specifically to make that failure mode loud.
    assert result.reprojection_error_cm is not None
    assert result.reprojection_error_cm < 1.0

    calibration = calibrator.get_calibration("test-camera")
    assert calibration is not None
    # Carried forward, unchanged -- this ticket does not *estimate*
    # intrinsics (ticket 002), only stores/applies/carries them.
    assert calibration.camera_matrix == _SYNTHETIC_CAMERA_MATRIX
    assert calibration.dist_coeffs == _SYNTHETIC_DIST_COEFFS

    # The "known-good" homography, solved directly from the *ideal*
    # (undistorted) pixel positions. Compared functionally (via
    # to_world), not as raw matrix elements, since a homography is only
    # defined up to an overall scale factor.
    ideal_pixel_pts = np.array(
        [standard_pixel_for_world(*MAIN_PLAYFIELD_ARUCO_POSITIONS[i]) for i in range(1, 11)],
        dtype=np.float32,
    )
    world_pts = np.array(
        [MAIN_PLAYFIELD_ARUCO_POSITIONS[i] for i in range(1, 11)], dtype=np.float32
    )
    known_good, _mask = cv.findHomography(ideal_pixel_pts, world_pts, method=0)
    assert known_good is not None

    for tag_id in range(1, 11):
        ideal_px, ideal_py = standard_pixel_for_world(*MAIN_PLAYFIELD_ARUCO_POSITIONS[tag_id])
        expected_world = geometry.pixel_to_world(known_good, PixelPoint(x=ideal_px, y=ideal_py))
        distorted_px, distorted_py = _distorted_pixel_for_world(
            *MAIN_PLAYFIELD_ARUCO_POSITIONS[tag_id]
        )
        actual_world = calibration.to_world(PixelPoint(x=distorted_px, y=distorted_py))
        assert actual_world.x == pytest.approx(expected_world.x, abs=0.5)
        assert actual_world.y == pytest.approx(expected_world.y, abs=0.5)


# ---------------------------------------------------------------------------
# Intrinsics estimation (sprint 030 ticket 002). Two kinds of fixtures:
#
# 1. The affine ``render_playfield_frame`` fixtures above -- fine for
#    tests about *which points reach which step* (the homography/
#    intrinsics split, the point-count floor), which do not depend on a
#    real projective camera model.
# 2. A NEW real *projective* camera model below (``render_projective_frame``
#    et al.) for tests about the intrinsics fit *itself* -- the affine
#    fixture above has no real perspective at all (uniform scale
#    everywhere), which is actually the worst possible input for a
#    focal-length estimate (indistinguishable from fx -> infinity, this
#    module's own "fx/depth degeneracy" -- see calibration.calibration's
#    module docstring). A real ``cv2.projectPoints`` model, at a real
#    camera height and a real (if modest) mount tilt, is what this
#    ticket's own empirical investigation actually used.
# ---------------------------------------------------------------------------

#: Matches this ticket's own empirical investigation exactly (calibration.
#: calibration's module docstring) -- a plausible overhead-webcam
#: geometry: 127 cm mount height (sprint 029's own camera 3 height),
#: 1280x720, fx=fy=900 (~70 deg FOV), a modest real k1/k2.
_PROJECTIVE_IMAGE_SIZE: Tuple[int, int] = (1280, 720)
_PROJECTIVE_CAMERA_HEIGHT_CM = 127.0
_PROJECTIVE_WELL_CONDITIONED_TILT_DEG = 8.0
_PROJECTIVE_TRUE_FX = 900.0
_PROJECTIVE_TRUE_FY = 900.0
_PROJECTIVE_TRUE_K1 = -0.18
_PROJECTIVE_TRUE_K2 = 0.04
_PROJECTIVE_TRUE_CAMERA_MATRIX = np.array(
    [
        [_PROJECTIVE_TRUE_FX, 0.0, _PROJECTIVE_IMAGE_SIZE[0] / 2.0],
        [0.0, _PROJECTIVE_TRUE_FY, _PROJECTIVE_IMAGE_SIZE[1] / 2.0],
        [0.0, 0.0, 1.0],
    ]
)
_PROJECTIVE_TRUE_DIST_COEFFS = np.array(
    [_PROJECTIVE_TRUE_K1, _PROJECTIVE_TRUE_K2, 0.0, 0.0, 0.0]
)


def _nadir_camera_pose(tilt_deg: float, height_cm: float) -> Tuple[np.ndarray, np.ndarray]:
    """A camera mounted *height_cm* above the field's z=0 plane, aimed
    straight down and then tilted *tilt_deg* about the x axis -- 0 deg is
    a mathematically exact nadir mount (this module's own documented
    degeneracy for a purely coplanar view); a few degrees is an
    ordinary, realistic mounting imperfection."""
    angle = np.radians(tilt_deg)
    rot_x = np.array(
        [[1.0, 0.0, 0.0], [0.0, np.cos(angle), -np.sin(angle)], [0.0, np.sin(angle), np.cos(angle)]]
    )
    # Base orientation: camera looks along -z (down), image x tracks
    # world x (east), image y tracks -world y (a larger world-y/north
    # sits higher in the image) -- the same convention
    # ``standard_pixel_for_world`` uses above, expressed as a rotation
    # matrix instead of a flat affine map.
    base = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    rotation = rot_x @ base
    rvec, _ = cv.Rodrigues(rotation)
    tvec = np.array([0.0, 0.0, height_cm])
    return rvec, tvec


def _project_world_points(
    world_xyz: List[Tuple[float, float, float]],
    *,
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
) -> np.ndarray:
    obj = np.array(world_xyz, dtype=np.float64)
    projected, _ = cv.projectPoints(obj, rvec, tvec, camera_matrix, dist_coeffs)
    return projected.reshape(-1, 2)


def render_projective_frame(
    marker_world_positions: Dict[int, Tuple[float, float, float]],
    *,
    family: TagFamily = TagFamily.ARUCO,
    tilt_deg: float = _PROJECTIVE_WELL_CONDITIONED_TILT_DEG,
    camera_height_cm: float = _PROJECTIVE_CAMERA_HEIGHT_CM,
    camera_matrix: np.ndarray = _PROJECTIVE_TRUE_CAMERA_MATRIX,
    dist_coeffs: np.ndarray = _PROJECTIVE_TRUE_DIST_COEFFS,
    marker_size: int = 40,
    canvas_size: Tuple[int, int] = _PROJECTIVE_IMAGE_SIZE,
) -> np.ndarray:
    """Like :func:`render_playfield_frame`, but marker centres are the
    output of a *real* ``cv2.projectPoints`` call (a real camera matrix,
    a real distortion model, a real 3D pose -- ``height_cm`` genuinely
    used, unlike the affine fixture above) rather than a flat scale+flip.
    Real detection (``vision/detector.py``) still runs against the
    result -- only the marker *placement* math changes."""
    width, height = canvas_size
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    rvec, tvec = _nadir_camera_pose(tilt_deg, camera_height_cm)
    half = marker_size // 2
    for tag_id, world_xyz in marker_world_positions.items():
        (px, py) = _project_world_points(
            [world_xyz], camera_matrix=camera_matrix, dist_coeffs=dist_coeffs, rvec=rvec, tvec=tvec
        )[0]
        marker = render_marker(family, tag_id, size=marker_size)
        marker_bgr = cv.cvtColor(marker, cv.COLOR_GRAY2BGR)
        x0, y0 = int(round(px - half)), int(round(py - half))
        assert 0 <= x0 and x0 + marker_size <= width, (
            f"tag {tag_id} projects to x={px:.1f}, outside the {width}px canvas -- fixture bug"
        )
        assert 0 <= y0 and y0 + marker_size <= height, (
            f"tag {tag_id} projects to y={py:.1f}, outside the {height}px canvas -- fixture bug"
        )
        canvas[y0 : y0 + marker_size, x0 : x0 + marker_size] = marker_bgr
    return canvas


def _estimate_intrinsics_for_positions(
    positions: Dict[int, Tuple[float, float, float]],
    *,
    tilt_deg: float,
    camera_matrix: np.ndarray = _PROJECTIVE_TRUE_CAMERA_MATRIX,
    dist_coeffs: np.ndarray = _PROJECTIVE_TRUE_DIST_COEFFS,
    camera_height_cm: float = _PROJECTIVE_CAMERA_HEIGHT_CM,
    image_size: Tuple[int, int] = _PROJECTIVE_IMAGE_SIZE,
):
    """Calls :func:`_estimate_intrinsics` directly against points
    computed by the same real projective model :func:`render_projective_frame`
    uses -- skips image rendering/detection entirely for tests that only
    care about the fit itself, not the detection pipeline. Derives
    ``elevated_feature_count``/``z_spread_cm`` from *positions*' own
    height column, matching exactly what :meth:`Calibrator.calibrate`
    computes from real detections."""
    rvec, tvec = _nadir_camera_pose(tilt_deg, camera_height_cm)
    world_xyz = list(positions.values())
    image_points = _project_world_points(
        world_xyz, camera_matrix=camera_matrix, dist_coeffs=dist_coeffs, rvec=rvec, tvec=tvec
    )
    object_points = np.array(world_xyz, dtype=np.float64)
    heights = object_points[:, 2]
    elevated_count = int(np.count_nonzero(heights > 0.0))
    z_spread_cm = float(heights.max() - heights.min()) if len(heights) else 0.0
    return _estimate_intrinsics(
        object_points,
        image_points,
        image_size,
        elevated_feature_count=elevated_count,
        z_spread_cm=z_spread_cm,
    )


def _estimate_intrinsics_for_main_playfield(*, tilt_deg: float):
    positions = {i: (x, y, 0.0) for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()}
    return _estimate_intrinsics_for_positions(positions, tilt_deg=tilt_deg)


# ---------------------------------------------------------------------------
# _held_out_split
# ---------------------------------------------------------------------------


def test_held_out_split_at_the_n_pts_floor_is_4_fit_2_holdout():
    fit_idx, holdout_idx = _held_out_split(MIN_SURVEYED_INTRINSICS_POINTS)
    assert len(fit_idx) == 4
    assert len(holdout_idx) == 2
    # Every index partitioned exactly once -- evenly spaced, not
    # necessarily a contiguous prefix/suffix block (see
    # _held_out_split's own docstring for why: a block split can
    # systematically exclude a clustered point *kind*, e.g. elevated
    # features conventionally appended last, from one side entirely).
    assert sorted(fit_idx + holdout_idx) == list(range(MIN_SURVEYED_INTRINSICS_POINTS))
    assert set(fit_idx).isdisjoint(holdout_idx)


def test_held_out_split_at_main_playfields_11_points_is_8_fit_3_holdout():
    fit_idx, holdout_idx = _held_out_split(11)
    assert len(fit_idx) == 8
    assert len(holdout_idx) == 3
    assert sorted(fit_idx + holdout_idx) == list(range(11))
    assert set(fit_idx).isdisjoint(holdout_idx)


def test_held_out_split_is_deterministic_across_repeated_calls():
    """No shuffling -- the same *n* always produces the same split, so
    a test (or an operator diagnosing a fit) can reproduce it."""
    first = _held_out_split(11)
    second = _held_out_split(11)
    assert first == second


def test_held_out_split_does_not_cluster_every_holdout_index_at_one_end():
    """The bug this ticket's own test suite caught: a trailing-block
    split held out every elevated point at once when they were appended
    last, silently starving the fit subset of the 3D structure elevated
    mode exists to use. Evenly spaced indices make that impossible --
    checked here directly by asserting the holdout indices are not all
    contiguous at the end of the range."""
    n = 13  # matches this ticket's own 10-coplanar + 3-elevated fixture shape
    fit_idx, holdout_idx = _held_out_split(n)
    assert len(holdout_idx) == 4
    # Not every point >= (n - len(holdout_idx)) is held out -- i.e. the
    # holdout set is not simply "the last k indices".
    assert holdout_idx != list(range(n - len(holdout_idx), n))
    # At least one of the last 3 indices (where this ticket's fixtures
    # place elevated points) survives into the fit subset.
    assert any(i in fit_idx for i in range(n - 3, n))


# ---------------------------------------------------------------------------
# _sanity_check_intrinsics -- v1's stored [0.42, -2.43, ...] is the
# concrete rejection example throughout (sprint.md, this ticket).
# ---------------------------------------------------------------------------

_PLAUSIBLE_CAMERA_MATRIX = np.array(
    [[900.0, 0.0, 640.0], [0.0, 900.0, 360.0], [0.0, 0.0, 1.0]]
)


def test_sanity_check_accepts_a_plausible_k1_only_fit():
    dist = np.array([-0.15, 0.0, 0.0, 0.0, 0.0])
    assert _sanity_check_intrinsics(
        _PLAUSIBLE_CAMERA_MATRIX, dist, _PROJECTIVE_IMAGE_SIZE, k2_free=False
    ) is None


def test_sanity_check_rejects_v1s_stored_k2_over_k1_ratio():
    """v1's actual archived ``dist_coeffs [0.42, -2.43, ...]``."""
    dist = np.array([0.42, -2.43, 0.0, 0.0, 0.0])
    reason = _sanity_check_intrinsics(
        _PLAUSIBLE_CAMERA_MATRIX, dist, _PROJECTIVE_IMAGE_SIZE, k2_free=True
    )
    assert reason is not None
    assert "k2" in reason.lower()
    assert "0.42" in reason


def test_sanity_check_does_not_apply_the_k2_ratio_bound_when_k2_is_fixed():
    """The coplanar-constrained default never frees k2 -- a stored
    ``dist_coeffs`` whose k2 slot happens to be nonzero (it should
    always be exactly 0.0 when CALIB_FIX_K2 is set, but this proves the
    *check* itself is correctly gated) is not evaluated against the
    ratio bound when k2_free=False."""
    dist = np.array([0.42, -2.43, 0.0, 0.0, 0.0])
    assert _sanity_check_intrinsics(
        _PLAUSIBLE_CAMERA_MATRIX, dist, _PROJECTIVE_IMAGE_SIZE, k2_free=False
    ) is None


def test_sanity_check_rejects_an_implausibly_large_k1():
    dist = np.array([5.0, 0.0, 0.0, 0.0, 0.0])
    reason = _sanity_check_intrinsics(
        _PLAUSIBLE_CAMERA_MATRIX, dist, _PROJECTIVE_IMAGE_SIZE, k2_free=False
    )
    assert reason is not None
    assert "k1" in reason.lower()


def test_sanity_check_rejects_an_implausible_focal_length():
    """The direct signature of this ticket's own fx/depth degeneracy
    finding -- caught even with an otherwise-small k1."""
    degenerate_matrix = np.array([[80000.0, 0.0, 640.0], [0.0, 80000.0, 360.0], [0.0, 0.0, 1.0]])
    dist = np.array([-0.1, 0.0, 0.0, 0.0, 0.0])
    reason = _sanity_check_intrinsics(
        degenerate_matrix, dist, _PROJECTIVE_IMAGE_SIZE, k2_free=False
    )
    assert reason is not None
    assert "focal length" in reason.lower()


def test_sanity_check_accepts_focal_lengths_at_both_edges_of_the_plausible_band():
    width = _PROJECTIVE_IMAGE_SIZE[0]
    low = np.array([[0.3 * width, 0.0, 640.0], [0.0, 0.3 * width, 360.0], [0.0, 0.0, 1.0]])
    high = np.array([[3.0 * width, 0.0, 640.0], [0.0, 3.0 * width, 360.0], [0.0, 0.0, 1.0]])
    dist = np.array([-0.1, 0.0, 0.0, 0.0, 0.0])
    assert _sanity_check_intrinsics(low, dist, _PROJECTIVE_IMAGE_SIZE, k2_free=False) is None
    assert _sanity_check_intrinsics(high, dist, _PROJECTIVE_IMAGE_SIZE, k2_free=False) is None


# ---------------------------------------------------------------------------
# _estimate_intrinsics -- the coplanar-constrained default: flags, held-out
# validation, and this ticket's own fx/depth degeneracy finding.
# ---------------------------------------------------------------------------


def test_estimate_intrinsics_coplanar_default_uses_the_constrained_flags(monkeypatch):
    """The ticket's headline acceptance criterion, checked at the actual
    ``cv2.calibrateCamera`` call boundary -- not merely described in a
    docstring. Wraps (not replaces) the real function, so the rest of
    this test still exercises a genuine fit."""
    calls: List[int] = []
    original = cv.calibrateCamera

    def spy(*args, **kwargs):
        calls.append(kwargs["flags"])
        return original(*args, **kwargs)

    monkeypatch.setattr(calibration_module.cv, "calibrateCamera", spy)

    fit = _estimate_intrinsics_for_main_playfield(tilt_deg=_PROJECTIVE_WELL_CONDITIONED_TILT_DEG)

    assert len(calls) == 1
    required_bits = (
        cv.CALIB_FIX_PRINCIPAL_POINT
        | cv.CALIB_FIX_ASPECT_RATIO
        | cv.CALIB_ZERO_TANGENT_DIST
        | cv.CALIB_FIX_K3
        | cv.CALIB_FIX_K2
    )
    assert (calls[0] & required_bits) == required_bits
    assert fit.mode == "coplanar-constrained"


def test_estimate_intrinsics_accepts_a_well_conditioned_coplanar_view():
    fit = _estimate_intrinsics_for_main_playfield(tilt_deg=_PROJECTIVE_WELL_CONDITIONED_TILT_DEG)

    assert fit.accepted is True, fit.reason
    assert fit.mode == "coplanar-constrained"
    assert fit.camera_matrix is not None
    assert fit.dist_coeffs is not None
    # k1 estimated, k2 fixed at exactly 0.0 (CALIB_FIX_K2) -- Open
    # Question 5's decision, visible in the actual output.
    assert fit.dist_coeffs[1] == 0.0
    fx = fit.camera_matrix[0][0]
    assert fx == pytest.approx(_PROJECTIVE_TRUE_FX, rel=0.2)


def test_estimate_intrinsics_reports_held_out_rms_not_silently_discarded():
    fit = _estimate_intrinsics_for_main_playfield(tilt_deg=_PROJECTIVE_WELL_CONDITIONED_TILT_DEG)

    assert fit.held_out_rms_px is not None
    assert fit.held_out_rms_px < 5.0  # a well-conditioned fit, real pixels


def test_estimate_intrinsics_rejects_a_near_exact_nadir_single_coplanar_view():
    """This ticket's own empirical finding (calibration.calibration's
    module docstring): a mathematically exact 0 deg mount tilt makes fx
    and camera-to-plane distance trade off along an exactly flat ridge,
    even under the constrained flags -- caught here, not silently
    stored."""
    fit = _estimate_intrinsics_for_main_playfield(tilt_deg=0.0)

    assert fit.accepted is False
    assert fit.reason is not None
    assert fit.camera_matrix is None
    assert fit.dist_coeffs is None
    assert fit.mode == "coplanar-constrained"


def test_estimate_intrinsics_held_out_rms_is_still_reported_on_rejection():
    """"Reported, not silently discarded" applies to a rejected fit too
    -- the residual is still computed and returned, only the coefficients
    themselves are withheld."""
    fit = _estimate_intrinsics_for_main_playfield(tilt_deg=0.0)

    assert fit.accepted is False
    assert fit.held_out_rms_px is not None


# ---------------------------------------------------------------------------
# Mode selection is graded (Open Question 6) -- a concrete, evidence-based
# threshold, never freed on a single elevated tag with a token height.
# ---------------------------------------------------------------------------


def _elevated_positions(count: int, height_cm: float) -> Dict[int, Tuple[float, float, float]]:
    candidates = [(-30.0, 30.0), (30.0, 30.0), (0.0, -30.0), (-30.0, -30.0)]
    assert count <= len(candidates)
    return {100 + i: (x, y, height_cm) for i, (x, y) in enumerate(candidates[:count])}


def _main_playfield_with_elevated(count: int, height_cm: float) -> Dict[int, Tuple[float, float, float]]:
    positions: Dict[int, Tuple[float, float, float]] = {
        i: (x, y, 0.0) for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()
    }
    positions.update(_elevated_positions(count, height_cm))
    return positions


def test_mode_stays_coplanar_constrained_below_the_elevated_feature_count_floor():
    """Two elevated points, well above the z-spread floor -- still not
    enough *count* (:data:`MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE` is
    3)."""
    positions = _main_playfield_with_elevated(2, 15.0)
    fit = _estimate_intrinsics_for_positions(positions, tilt_deg=0.0)
    assert fit.mode == "coplanar-constrained"


def test_mode_stays_coplanar_constrained_below_the_z_spread_floor():
    """Three elevated points -- count clears the floor -- but only 2 cm
    of z-spread, below :data:`MIN_ELEVATED_Z_SPREAD_CM` (5 cm): a token
    height must not free the model."""
    positions = _main_playfield_with_elevated(3, 2.0)
    fit = _estimate_intrinsics_for_positions(positions, tilt_deg=0.0)
    assert fit.mode == "coplanar-constrained"


def test_mode_switches_to_elevated_once_both_thresholds_clear():
    positions = _main_playfield_with_elevated(3, 15.0)
    fit = _estimate_intrinsics_for_positions(positions, tilt_deg=0.0)
    assert fit.mode == "elevated"
    assert fit.accepted is True, fit.reason


def test_elevated_mode_resolves_the_fx_depth_degeneracy_even_at_exact_nadir():
    """The point of elevated ground truth (sprint.md Design Rationale
    Decision 2): genuine 3D structure disambiguates fx from
    camera-to-plane distance *without needing any mount tilt at all* --
    unlike the coplanar-only case (0 deg tilt is rejected above), this
    succeeds and lands close to the true focal length."""
    positions = _main_playfield_with_elevated(3, 15.0)
    fit = _estimate_intrinsics_for_positions(positions, tilt_deg=0.0)

    assert fit.accepted is True, fit.reason
    fx = fit.camera_matrix[0][0]
    assert fx == pytest.approx(_PROJECTIVE_TRUE_FX, rel=0.2)


def test_elevated_mode_frees_more_of_the_model_than_the_coplanar_default(monkeypatch):
    """"Freed further" (sprint.md Design Rationale Decision 2) checked
    structurally: none of the coplanar-constrained CALIB_FIX_* bits are
    present once elevated mode runs."""
    calls: List[int] = []
    original = cv.calibrateCamera

    def spy(*args, **kwargs):
        calls.append(kwargs["flags"])
        return original(*args, **kwargs)

    monkeypatch.setattr(calibration_module.cv, "calibrateCamera", spy)

    positions = _main_playfield_with_elevated(3, 15.0)
    fit = _estimate_intrinsics_for_positions(positions, tilt_deg=0.0)

    assert fit.mode == "elevated"
    assert len(calls) == 1
    constrained_bits = (
        cv.CALIB_FIX_PRINCIPAL_POINT
        | cv.CALIB_FIX_ASPECT_RATIO
        | cv.CALIB_ZERO_TANGENT_DIST
        | cv.CALIB_FIX_K3
        | cv.CALIB_FIX_K2
    )
    assert (calls[0] & constrained_bits) == 0


# ---------------------------------------------------------------------------
# Calibrator.calibrate(estimate_intrinsics=...) end to end.
# ---------------------------------------------------------------------------


def test_estimate_intrinsics_fails_below_the_n_pts_floor_naming_the_count_before_any_frame():
    """The floor counts what the playfield *definition* surveys (4 here),
    never what a burst detects -- and is checked before any frame is
    captured (mirrors NoPlayfieldLinked's own "zero backend calls" proof
    shape)."""
    positions = {1: (-40.0, 30.0), 2: (40.0, 30.0), 3: (40.0, -30.0), 4: (-40.0, -30.0)}
    definition = PlayfieldDefinition(
        name="tiny-field",
        width_cm=120.0,
        height_cm=100.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"a{i}", x=x, y=y, tag_id=i) for i, (x, y) in positions.items()
        ),
    )
    capture_calls: List[str] = []

    def capture_frames(slug: str) -> List[np.ndarray]:
        capture_calls.append(slug)
        return []

    calibrator = Calibrator(
        get_camera_config=lambda slug: CameraConfig(name=slug, playfield="tiny-field", settings={}),
        get_playfield=lambda name: definition,
        capture_frames=capture_frames,
    )

    result = calibrator.calibrate("test-camera", estimate_intrinsics=True)

    assert result.succeeded is False
    assert result.intrinsics_estimated is False
    assert "4" in result.reason
    assert str(MIN_SURVEYED_INTRINSICS_POINTS) in result.reason
    assert capture_calls == []


def test_estimate_intrinsics_proceeds_past_the_floor_with_exactly_6_surveyed_points():
    """The boundary case: exactly the floor -- proceeds to frame capture
    (proven here by an empty ``capture_frames`` raising the *next*
    error, ``RuntimeError``, rather than the floor's own
    ``CalibrationResult`` failure)."""
    positions = {i: MAIN_PLAYFIELD_ARUCO_POSITIONS[i] for i in range(1, 7)}
    definition = PlayfieldDefinition(
        name="six-point-field",
        width_cm=200.0,
        height_cm=140.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"a{i}", x=x, y=y, tag_id=i) for i, (x, y) in positions.items()
        ),
    )
    capture_calls: List[str] = []

    def capture_frames(slug: str) -> List[np.ndarray]:
        capture_calls.append(slug)
        return []

    calibrator = Calibrator(
        get_camera_config=lambda slug: CameraConfig(name=slug, playfield="six-point-field", settings={}),
        get_playfield=lambda name: definition,
        capture_frames=capture_frames,
    )

    with pytest.raises(RuntimeError, match="no frames available"):
        calibrator.calibrate("test-camera", estimate_intrinsics=True)

    assert capture_calls == ["test-camera"]


def test_elevated_aruco_feature_is_excluded_from_the_homography_solve_point_set(monkeypatch):
    """The single easiest thing here to get wrong (ticket description),
    checked as a real structural assertion: patches
    ``_solve_homography``/``_reprojection_error_cm`` themselves and
    inspects the *actual arrays* handed to them, rather than checking a
    downstream value that could pass by luck."""
    coplanar_positions = {
        1: (-60.0, 40.0),
        2: (60.0, 40.0),
        3: (60.0, -40.0),
        4: (-60.0, -40.0),
        5: (0.0, 0.0),
    }
    elevated_id = 6
    elevated_position = (0.0, 30.0)
    elevated_height_cm = 10.0

    entries = [
        PlayfieldFeatureEntry(slug=f"a{i}", x=x, y=y, tag_id=i)
        for i, (x, y) in coplanar_positions.items()
    ]
    entries.append(
        PlayfieldFeatureEntry(
            slug=f"a{elevated_id}",
            x=elevated_position[0],
            y=elevated_position[1],
            tag_id=elevated_id,
            height_cm=elevated_height_cm,
        )
    )
    definition = PlayfieldDefinition(
        name="elevated-field", width_cm=200.0, height_cm=140.0, aruco_tags=tuple(entries)
    )

    render_positions = dict(coplanar_positions)
    render_positions[elevated_id] = elevated_position
    frame = render_playfield_frame(render_positions, canvas_size=(700, 700))

    homography_calls: List[Tuple[np.ndarray, np.ndarray]] = []
    rms_calls: List[Tuple[np.ndarray, np.ndarray]] = []
    original_solve = calibration_module._solve_homography
    original_rms = calibration_module._reprojection_error_cm

    def spy_solve(pixel_pts, world_pts):
        homography_calls.append((pixel_pts.copy(), world_pts.copy()))
        return original_solve(pixel_pts, world_pts)

    def spy_rms(homography, pixel_pts, world_pts):
        rms_calls.append((pixel_pts.copy(), world_pts.copy()))
        return original_rms(homography, pixel_pts, world_pts)

    monkeypatch.setattr(calibration_module, "_solve_homography", spy_solve)
    monkeypatch.setattr(calibration_module, "_reprojection_error_cm", spy_rms)

    calibrator = _calibrator_for(definition, {"frames": [frame]}, playfield_name="elevated-field")
    result = calibrator.calibrate("test-camera")

    assert result.succeeded is True, result.reason
    # Completeness still counts every detected+required id, elevated
    # included -- only the homography/RMS *input arrays* exclude it.
    assert result.matched_ids == (1, 2, 3, 4, 5, 6)

    assert len(homography_calls) == 1
    assert len(rms_calls) == 1
    elevated_world = np.array([elevated_position[0], elevated_position[1]])

    _, homography_world_pts = homography_calls[0]
    _, rms_world_pts = rms_calls[0]
    assert len(homography_world_pts) == 5
    assert len(rms_world_pts) == 5
    assert not any(np.allclose(row, elevated_world) for row in homography_world_pts)
    assert not any(np.allclose(row, elevated_world) for row in rms_world_pts)


def test_estimated_intrinsics_are_carried_forward_on_a_subsequent_no_flag_call():
    """SUC-002's main flow step 5: a later, ordinary ``calibrate`` (no
    flag) carries the stored intrinsics forward unchanged."""
    definition = main_playfield_definition()
    frame = render_projective_frame(
        {i: (x, y, 0.0) for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()},
        tilt_deg=_PROJECTIVE_WELL_CONDITIONED_TILT_DEG,
    )
    frames_holder = {"frames": [frame, frame, frame]}
    calibrator = _calibrator_for(definition, frames_holder)

    first = calibrator.calibrate("test-camera", estimate_intrinsics=True)
    assert first.succeeded is True, first.reason
    assert first.intrinsics_estimated is True
    assert first.intrinsics_mode == "coplanar-constrained"
    assert first.elevated_features_used == 0
    first_calibration = calibrator.get_calibration("test-camera")
    assert first_calibration is not None
    assert first_calibration.camera_matrix is not None
    assert first_calibration.dist_coeffs is not None

    second = calibrator.calibrate("test-camera")
    assert second.succeeded is True, second.reason
    assert second.intrinsics_estimated is False
    second_calibration = calibrator.get_calibration("test-camera")
    assert second_calibration is not None
    assert second_calibration.camera_matrix == first_calibration.camera_matrix
    assert second_calibration.dist_coeffs == first_calibration.dist_coeffs


def test_estimate_intrinsics_true_uses_april_tag_ground_truth_too(caplog):
    """``main-playfield``'s real shape: 10 ArUco + AprilTag 1 (sprint.md,
    SUC-002) -- the AprilTag contributes to the intrinsics fit (visible
    via the held-out-RMS log line, this ticket's own "reported, not
    silently discarded" mechanism) even though it never enters the
    homography (AprilTags never have before this ticket either)."""
    definition = main_playfield_definition_with_april_tag()
    positions = {i: (x, y, 0.0) for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()}
    frame = render_projective_frame(positions, tilt_deg=_PROJECTIVE_WELL_CONDITIONED_TILT_DEG)
    april_frame = render_projective_frame(
        {1: (0.0, 0.0, 0.0)}, family=TagFamily.APRILTAG, tilt_deg=_PROJECTIVE_WELL_CONDITIONED_TILT_DEG
    )
    combined = np.minimum(frame, april_frame)  # both are white-background, black-marker canvases

    calibrator = _calibrator_for(definition, {"frames": [combined, combined, combined]})

    import logging

    with caplog.at_level(logging.INFO, logger="aprilcam.calibration.calibration"):
        result = calibrator.calibrate("test-camera", estimate_intrinsics=True)

    assert result.succeeded is True, result.reason
    assert result.intrinsics_estimated is True
    assert any("held_out_rms_px" in record.message for record in caplog.records)
