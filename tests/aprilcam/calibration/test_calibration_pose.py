"""Calibrator pose path (sprint 045 ticket 006): strategy, policy, gates,
degradation ladder and the centre-point fallback.

Two kinds of synthetic scene, both with known ground truth:

* **Rendered**: a texture of real ArUco markers on the field plane is
  resampled through the true camera pose and lens (``cv2.remap``), so the
  full detect -> pose -> gates pipeline runs on actual pixels.
* **Injected**: ``MarkerObservation`` objects projected with
  ``cv2.projectPoints`` (``test_pose.scene``) are fed straight in by
  replacing ``Calibrator._detect_markers``. Used where a gate must be driven
  into failure by a controlled amount of noise, mis-survey or mis-printing.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import pytest

from aprilcam.calibration import gates
from aprilcam.calibration.calibration import Calibration, Calibrator
from aprilcam.calibration.lens import LensModel
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.types import PixelPoint, TagFamily, WorldPoint
from tests.aprilcam.calibration.test_pose import (
    CORNERS4,
    DIST,
    K,
    MAIN10,
    MAIN_TURNS,
    SIZE_MM,
    TRUE_HEIGHT,
    TRUE_NADIR,
    scene,
    true_pose,
)

W, H = 1280, 720
CAM = "test-camera"
RENDER_MM = 96.0  # printed size of the rendered markers (16 px per cell at 10 px/cm)
_ARUCO = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def lens_model(dist=DIST, size=(W, H), source: str = "measured") -> LensModel:
    return LensModel(
        camera_matrix=tuple(tuple(float(v) for v in row) for row in K),  # type: ignore[arg-type]
        dist_coeffs=tuple(float(v) for v in dist),
        image_size=size,
        source=source,
        views=10,
        installed_at=1.0,
    )


def definition(
    layout: Dict[int, Tuple[float, float]],
    *,
    size_mm: Optional[float] = RENDER_MM,
    policy: Optional[Dict[int, str]] = None,
    heights: Optional[Dict[int, float]] = None,
    default_size: bool = True,
) -> PlayfieldDefinition:
    policy = policy or {}
    heights = heights or {}
    return PlayfieldDefinition(
        name="pf",
        width_cm=120.0,
        height_cm=80.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(
                slug=f"aruco-{i}", x=x, y=y, tag_id=i,
                policy=policy.get(i), height_cm=heights.get(i, 0.0),
            )
            for i, (x, y) in layout.items()
        ),
        marker_size_mm_default=size_mm if default_size else None,
    )


def calibrator(
    pf: PlayfieldDefinition,
    frame: np.ndarray,
    *,
    lens: Optional[LensModel] = None,
    warning: Optional[str] = None,
    prior: Optional[Calibration] = None,
    frames: Optional[List[np.ndarray]] = None,
) -> Calibrator:
    holder = {"frames": frames if frames is not None else [frame]}
    return Calibrator(
        get_camera_config={CAM: CameraConfig(name=CAM, playfield="pf", settings={})}.get,
        get_playfield={"pf": pf}.get,
        capture_frames=lambda slug: holder["frames"],
        get_lens_model=(lambda slug, size: lens if lens is not None and lens.image_size == size else None),
        get_lens_warning=lambda slug, size: warning,
        get_prior_calibration=lambda slug: prior,
    )


def render(
    layout: Dict[int, Tuple[float, float]],
    turns: Optional[Dict[int, int]] = None,
    *,
    dist=DIST,
    tilt: float = 4.0,
    size_mm: float = RENDER_MM,
) -> np.ndarray:
    """A 1280x720 BGR frame of real markers on the field, seen through the true camera."""
    turns = turns or {}
    ppc = 10
    x0, y1 = -90.0, 60.0  # texture covers x in [-90, 90], y in [-60, 60]
    tex = np.full((1200, 1800), 255, np.uint8)
    side = int(round(size_mm / 10.0 * ppc))
    for tag_id, (cx, cy) in layout.items():
        img = np.rot90(cv2.aruco.generateImageMarker(_ARUCO, tag_id, side), turns.get(tag_id, 0))
        c0 = int(round((cx - x0) * ppc - side / 2))
        r0 = int(round((y1 - cy) * ppc - side / 2))
        tex[r0:r0 + side, c0:c0 + side] = img
    rvec, tvec = true_pose(tilt)
    rot, _ = cv2.Rodrigues(rvec.reshape(3, 1))
    us, vs = np.meshgrid(np.arange(W), np.arange(H))
    grid = np.stack([us.ravel(), vs.ravel()], axis=1).astype(np.float64)
    xy = cv2.undistortPoints(grid.reshape(-1, 1, 2), K, np.asarray(dist, float)).reshape(-1, 2)
    d = np.column_stack([xy, np.ones(len(xy))])
    rt_t = rot.T @ tvec.reshape(3)
    rt_d = d @ rot  # rows are R^T d
    lam = rt_t[2] / rt_d[:, 2]
    world = lam[:, None] * rt_d - rt_t
    map_x = ((world[:, 0] - x0) * ppc).astype(np.float32).reshape(H, W)
    map_y = ((y1 - world[:, 1]) * ppc).astype(np.float32).reshape(H, W)
    gray = cv2.remap(tex, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def inject(cal: Calibrator, observations) -> None:
    cal._detect_markers = lambda frames: {TagFamily.ARUCO: {o.tag_id: o for o in observations}}  # type: ignore[method-assign]


BLANK = np.zeros((H, W, 3), np.uint8)

# Rendered 4-corner scene layout: far enough apart for 96 mm markers.
LAYOUT4 = CORNERS4


@pytest.fixture(scope="module")
def frame4() -> np.ndarray:
    return render(LAYOUT4)


def world_error(cal: Calibration, truth_pairs: Sequence[Tuple[float, float]]) -> float:
    """Max distance (cm) between the calibration's mapping and the true camera's."""
    rvec, tvec = true_pose()
    errs = []
    for (x, y) in truth_pairs:
        px, _ = cv2.projectPoints(np.array([[x, y, 0.0]]), rvec, tvec, K, DIST)
        w = cal.to_world(PixelPoint(x=float(px[0, 0, 0]), y=float(px[0, 0, 1])))
        errs.append(np.hypot(w.x - x, w.y - y))
    return float(max(errs))


FIELD_POINTS = [(0.0, 0.0), (20.0, -10.0), (-33.0, 21.0), (47.0, 5.0), (-10.0, -30.0), (50.0, 30.0)]


# ---------------------------------------------------------------------------
# Strategy and the full pose path
# ---------------------------------------------------------------------------


def test_four_markers_measured_lens_sized_definition_uses_pose(frame4):
    cal = calibrator(definition(LAYOUT4), frame4, lens=lens_model())
    result = cal.calibrate(CAM)

    assert result.succeeded, result.reason
    assert result.method == "pose"
    assert result.method_reason
    assert result.confidence == "full"
    assert result.corner_count == 16
    assert result.matched_ids == (1, 2, 3, 4) and result.missing_ids == ()
    assert result.lens_model_source == "measured"
    assert abs(result.camera_height_cm - TRUE_HEIGHT) < 2.0
    assert np.hypot(result.camera_nadir.x - TRUE_NADIR[0], result.camera_nadir.y - TRUE_NADIR[1]) < 2.0
    for value in (
        result.reprojection_error_cm, result.reprojection_error_px, result.scale_agreement,
        result.angular_spread_deg, result.camera_tilt_deg, result.layout_extent_ratio,
        result.leave_one_out_worst_id, result.leave_one_out_worst_cm,
    ):
        assert value is not None
    assert result.reprojection_error_px < gates.MAX_RMS_PX and result.reprojection_error_cm < 1.0
    assert abs(result.scale_agreement - 1.0) < 0.05
    assert abs(result.camera_tilt_deg - 4.0) < 1.5
    assert result.helpful_missing_ids == ()

    stored = cal.get_calibration(CAM)
    assert stored is not None and stored.method == "pose" and stored.confidence == "full"
    assert stored.lens_source == "measured" and stored.corner_count == 16
    assert stored.camera_geometry() is not None
    assert world_error(stored, FIELD_POINTS) < 0.5


@pytest.mark.parametrize("turns", [{1: 0, 2: 1, 3: 2, 4: 3}, {1: 2, 2: 2, 3: 2, 4: 2}])
def test_mixed_orientations_calibrate_like_upright(frame4, turns):
    upright = calibrator(definition(LAYOUT4), frame4, lens=lens_model())
    ref = upright.calibrate(CAM)
    rotated = calibrator(definition(LAYOUT4), render(LAYOUT4, turns), lens=lens_model())
    result = rotated.calibrate(CAM)

    assert result.succeeded, result.reason
    assert result.method == "pose" and result.confidence == "full" and result.corner_count == 16
    assert abs(result.camera_height_cm - ref.camera_height_cm) < 1.0
    assert world_error(rotated.get_calibration(CAM), FIELD_POINTS) < 0.5


def test_ten_marker_layout_mixed_orientations_pose():
    # The main-field-like layout shrunk to fit the rendered camera's view.
    layout = {i: (0.8 * x, 0.8 * y) for i, (x, y) in MAIN10.items()}
    cal = calibrator(definition(layout), render(layout, MAIN_TURNS), lens=lens_model())
    result = cal.calibrate(CAM)
    assert result.succeeded, result.reason
    assert result.method == "pose" and result.corner_count == 40
    assert world_error(cal.get_calibration(CAM), FIELD_POINTS) < 0.5


# ---------------------------------------------------------------------------
# Policy and degradation
# ---------------------------------------------------------------------------


def _visible(ids: Sequence[int]) -> Dict[int, Tuple[float, float]]:
    return {i: LAYOUT4[i] for i in ids}


def test_two_markers_with_others_helpful_is_reduced_with_a_note():
    policy = {1: "required", 3: "required", 2: "helpful", 4: "helpful"}
    cal = calibrator(definition(LAYOUT4, policy=policy), render(_visible([1, 3])), lens=lens_model())
    result = cal.calibrate(CAM)

    assert result.succeeded, result.reason
    assert result.method == "pose" and result.confidence == "reduced"
    assert result.corner_count == 8
    assert result.helpful_missing_ids == (2, 4)
    assert any("not seen" in n and "[2, 4]" in n for n in result.notes)
    assert cal.get_calibration(CAM).confidence == "reduced"


def test_two_marker_definition_calibrates_with_a_warning():
    layout = _visible([1, 3])
    cal = calibrator(definition(layout), render(layout), lens=lens_model())
    result = cal.calibrate(CAM)
    assert result.succeeded, result.reason
    assert result.confidence == "reduced"
    assert result.notes  # a warning is attached


def test_one_marker_with_others_helpful_is_low():
    policy = {1: "required", 2: "helpful", 3: "helpful", 4: "helpful"}
    cal = calibrator(definition(LAYOUT4, policy=policy), render(_visible([1])), lens=lens_model())
    result = cal.calibrate(CAM)

    assert result.succeeded, result.reason
    assert result.method == "pose" and result.confidence == "low"
    assert result.corner_count == 4
    assert any("low confidence" in n for n in result.notes)
    assert result.helpful_missing_ids == (2, 3, 4)


def test_covered_required_marker_fails_like_today_and_keeps_prior(frame4):
    cal = calibrator(definition(LAYOUT4), frame4, lens=lens_model())
    first = cal.calibrate(CAM)
    assert first.succeeded
    prior = cal.get_calibration(CAM)

    covered = calibrator(definition(LAYOUT4), render(_visible([1, 2, 4])), lens=lens_model())
    covered._calibrations[CAM] = prior
    result = covered.calibrate(CAM)

    assert result.succeeded is False
    assert result.missing_ids == (3,)
    assert result.matched_ids == (1, 2, 4)
    assert result.reason is None  # today's shape: ids alone say why
    assert result.method is None
    assert covered.get_calibration(CAM) is prior


def test_covered_required_with_helpful_others_still_fails():
    policy = {1: "required", 2: "required", 3: "helpful", 4: "helpful"}
    cal = calibrator(definition(LAYOUT4, policy=policy), render(_visible([1, 3, 4])), lens=lens_model())
    result = cal.calibrate(CAM)
    assert result.succeeded is False and result.missing_ids == (2,)
    assert cal.get_calibration(CAM) is None


# ---------------------------------------------------------------------------
# Centre-point fallback
# ---------------------------------------------------------------------------


def test_definition_without_size_falls_back_to_centre_point_identically(frame4):
    pf = definition(LAYOUT4, size_mm=None, default_size=False)
    sized_less = calibrator(pf, frame4, lens=lens_model())
    result = sized_less.calibrate(CAM)
    assert result.succeeded
    assert result.method == "centre-point"
    assert result.method_reason == "definition has no marker size"
    assert result.confidence is None and result.corner_count is None

    # The pre-sprint behaviour: the same lens carried as a prior calibration.
    legacy_prior = Calibration(
        stale=False, calibrated_at=0.0, playfield="pf",
        homography=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), matched_ids=(),
        camera_matrix=lens_model().camera_matrix, dist_coeffs=lens_model().dist_coeffs,
        lens_source="legacy",
    )
    before = calibrator(pf, frame4, lens=None, prior=legacy_prior)
    ref = before.calibrate(CAM)
    assert ref.method == "centre-point" and ref.method_reason == "lens source is legacy"
    assert sized_less.get_calibration(CAM).homography == before.get_calibration(CAM).homography
    assert result.reprojection_error_cm == ref.reprojection_error_cm
    # The stored centre-point calibration keeps its pre-045 shape (no provenance).
    assert sized_less.get_calibration(CAM).method is None


def test_no_lens_falls_back_with_reason(frame4):
    cal = calibrator(definition(LAYOUT4), frame4, lens=None)
    result = cal.calibrate(CAM)
    assert result.succeeded and result.method == "centre-point"
    assert result.method_reason == "no measured lens model"


def test_lens_at_other_resolution_falls_back_and_keeps_warning(frame4):
    warning = "measured lens is 1920x1080; frame is 1280x720"
    cal = calibrator(definition(LAYOUT4), frame4, lens=lens_model(size=(1920, 1080)), warning=warning)
    result = cal.calibrate(CAM)
    assert result.succeeded and result.method == "centre-point"
    assert result.method_reason == "lens measured at a different resolution"
    assert result.lens_warning == warning


def test_non_measured_lens_falls_back(frame4):
    cal = calibrator(definition(LAYOUT4), frame4, lens=lens_model(source="estimated"))
    result = cal.calibrate(CAM)
    assert result.succeeded and result.method == "centre-point"
    assert result.method_reason == "lens source is estimated"


def test_derived_homography_agrees_with_centre_point_without_distortion():
    zero = np.zeros(5)
    layout = {i: (0.8 * x, 0.8 * y) for i, (x, y) in LAYOUT4.items()}  # fits the undistorted view
    frame = render(layout, dist=zero)
    lens = lens_model(dist=zero)
    pose_cal = calibrator(definition(layout), frame, lens=lens)
    centre_cal = calibrator(definition(layout, size_mm=None, default_size=False), frame, lens=lens)
    assert pose_cal.calibrate(CAM).method == "pose"
    assert centre_cal.calibrate(CAM).method == "centre-point"
    a, b = pose_cal.get_calibration(CAM), centre_cal.get_calibration(CAM)

    worst = 0.0
    for x in np.linspace(-44, 44, 9):
        for y in np.linspace(-28, 28, 7):
            rvec, tvec = true_pose()
            px, _ = cv2.projectPoints(np.array([[x, y, 0.0]]), rvec, tvec, K, zero)
            p = PixelPoint(x=float(px[0, 0, 0]), y=float(px[0, 0, 1]))
            wa, wb = a.to_world(p), b.to_world(p)
            worst = max(worst, float(np.hypot(wa.x - wb.x, wa.y - wb.y)))
    assert worst < 0.2


# ---------------------------------------------------------------------------
# Gates, each failing in isolation (injected observations)
# ---------------------------------------------------------------------------


def _pf46(layout, **kw) -> PlayfieldDefinition:
    return definition(layout, size_mm=SIZE_MM, **kw)


def test_inflated_corner_noise_is_rejected_by_the_px_gate():
    obs, _, _ = scene(CORNERS4, noise=14.0, seed=3)
    cal = calibrator(_pf46(CORNERS4), BLANK, lens=lens_model())
    inject(cal, obs)
    result = cal.calibrate(CAM)
    assert result.succeeded is False
    assert "px" in result.reason and "12.0" in result.reason
    assert result.reprojection_error_px > gates.MAX_RMS_PX
    assert result.method == "pose" and result.corner_count == 16
    assert cal.get_calibration(CAM) is None


def test_wrong_marker_size_is_rejected_by_the_scale_gate():
    obs, _, _ = scene(CORNERS4, size_mm=SIZE_MM * 1.065)
    cal = calibrator(_pf46(CORNERS4), BLANK, lens=lens_model())
    inject(cal, obs)
    result = cal.calibrate(CAM)
    assert result.succeeded is False
    assert "scale" in result.reason
    assert abs(result.scale_agreement - 1.0) > 0.05
    assert cal.get_calibration(CAM) is None


def test_markers_clustered_in_one_corner_are_flagged_with_the_spread_reported():
    cluster = {1: (45.0, 25.0), 2: (55.0, 25.0), 3: (55.0, 31.0), 4: (45.0, 31.0)}
    obs, _, _ = scene(cluster)
    cal = calibrator(_pf46(cluster), BLANK, lens=lens_model())
    inject(cal, obs)
    result = cal.calibrate(CAM)
    assert result.angular_spread_deg is not None and result.angular_spread_deg < 10.0
    # Weak geometry caps the label (gates.classify) rather than failing.
    assert result.succeeded and result.confidence == "low"
    assert any("angular spread" in n for n in result.notes)


def test_collinear_three_in_a_row_is_capped_low():
    row = {1: (-50.0, 0.0), 2: (0.0, 0.0), 3: (50.0, 0.0)}
    obs, _, _ = scene(row)
    cal = calibrator(_pf46(row), BLANK, lens=lens_model())
    inject(cal, obs)
    result = cal.calibrate(CAM)
    assert result.layout_extent_ratio is not None and result.layout_extent_ratio < 0.2
    assert result.confidence == "low"
    assert any("collinear" in n for n in result.notes)


def test_one_marker_surveyed_in_the_wrong_place_is_rejected_and_named():
    true_layout = dict(MAIN10)
    wrong = dict(MAIN10)
    wrong[5] = (MAIN10[5][0] - 6.0, MAIN10[5][1] + 6.0)
    obs, _, _ = scene(true_layout, MAIN_TURNS)
    cal = calibrator(_pf46(wrong), BLANK, lens=lens_model())
    inject(cal, obs)
    result = cal.calibrate(CAM)
    assert result.succeeded is False
    assert result.leave_one_out_worst_id == 5
    assert result.leave_one_out_worst_cm > 2.0
    assert "marker 5" in result.reason
    assert cal.get_calibration(CAM) is None


def test_swapped_nine_and_ten_fail_naming_a_swapped_marker():
    swapped = dict(MAIN10)
    swapped[9], swapped[10] = MAIN10[10], MAIN10[9]
    obs, _, _ = scene(MAIN10, MAIN_TURNS)
    cal = calibrator(_pf46(swapped), BLANK, lens=lens_model())
    inject(cal, obs)
    result = cal.calibrate(CAM)
    assert result.succeeded is False
    assert result.leave_one_out_worst_id in (9, 10)
    assert result.leave_one_out_worst_cm > 2.0
    assert f"marker {result.leave_one_out_worst_id}" in result.reason
    assert cal.get_calibration(CAM) is None


def test_failed_gate_keeps_the_previous_calibration():
    good, _, _ = scene(CORNERS4)
    cal = calibrator(_pf46(CORNERS4), BLANK, lens=lens_model())
    inject(cal, good)
    assert cal.calibrate(CAM).succeeded
    prior = cal.get_calibration(CAM)
    bad, _, _ = scene(CORNERS4, noise=2.0, seed=1)
    inject(cal, bad)
    assert cal.calibrate(CAM).succeeded is False
    assert cal.get_calibration(CAM) is prior


# ---------------------------------------------------------------------------
# Elevated markers
# ---------------------------------------------------------------------------


def test_elevated_markers_are_ignored_by_the_pose_solve(caplog):
    layout = dict(CORNERS4)
    layout[7] = (0.0, 0.0)
    obs, _, _ = scene(CORNERS4)
    # Marker 7 sits 5 cm up; its pixels are arbitrary and must not matter.
    junk, _, _ = scene({7: (30.0, 10.0)}, size_mm=SIZE_MM)
    cal = calibrator(_pf46(layout, heights={7: 5.0}), BLANK, lens=lens_model())
    inject(cal, list(obs) + list(junk))
    with caplog.at_level(logging.INFO):
        result = cal.calibrate(CAM)
    assert result.succeeded, result.reason
    assert result.method == "pose" and result.corner_count == 16 and result.confidence == "full"
    assert any("marker 7 is elevated" in r.getMessage() for r in caplog.records)
