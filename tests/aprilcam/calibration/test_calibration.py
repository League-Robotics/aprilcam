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

from typing import Callable, Dict, List, Tuple

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration.calibration import MAX_REPROJECTION_ERROR_CM, Calibration, Calibrator
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.errors import NoPlayfieldLinked
from aprilcam.types import PixelPoint

__all__ = [
    "MAIN_PLAYFIELD_ARUCO_POSITIONS",
    "main_playfield_definition",
    "render_playfield_frame",
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
