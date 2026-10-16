"""Tests for ``calibration/pose.py`` (sprint 045 ticket 004): synthetic ground truth."""

from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

import cv2
import numpy as np
import pytest

from aprilcam.calibration import gates, pose
from aprilcam.calibration.interfaces import MarkerObservation
from aprilcam.calibration.pose import SurveyedMarker
from aprilcam.types import PixelPoint

K = np.array([[1000.0, 0, 640.0], [0, 1000.0, 360.0], [0, 0, 1]])
DIST = np.array([-0.25, 0.08, 0.0, 0.0, 0.0])
SIZE_MM = 46.0
TRUE_HEIGHT, TRUE_NADIR = 144.0, (3.47, -2.85)

# Secondary-field style corners, then a main-field-like ten marker layout (cm).
CORNERS4 = {1: (-55.0, 35.0), 2: (55.0, 35.0), 3: (55.0, -35.0), 4: (-55.0, -35.0)}
MAIN10 = {
    1: (-60.0, 38.0), 2: (0.0, 40.0), 3: (60.0, 38.0), 4: (62.0, 0.0), 5: (60.0, -38.0),
    6: (20.0, -40.0), 7: (-20.0, -40.0), 8: (-60.0, -38.0), 9: (-62.0, 0.0), 10: (-30.0, 10.0),
}
MAIN_TURNS = {1: 0, 2: 0, 3: 0, 4: 1, 8: 3, 5: 2, 6: 2, 7: 2, 9: 2, 10: 2}


def true_pose(tilt_deg: float = 4.0, azim_deg: float = 30.0) -> Tuple[np.ndarray, np.ndarray]:
    r0 = np.diag([1.0, -1.0, -1.0])  # looking straight down, north up, no mirror
    axis = np.array([math.cos(math.radians(azim_deg)), math.sin(math.radians(azim_deg)), 0.0])
    rt, _ = cv2.Rodrigues((axis * math.radians(tilt_deg)).reshape(3, 1))
    rot = rt @ r0
    cam = np.array([TRUE_NADIR[0], TRUE_NADIR[1], TRUE_HEIGHT])
    tvec = -rot @ cam
    rvec, _ = cv2.Rodrigues(rot)
    return rvec.reshape(3), tvec


def project(pts3: np.ndarray, rvec, tvec) -> np.ndarray:
    px, _ = cv2.projectPoints(np.asarray(pts3, float).reshape(-1, 3), rvec, tvec, K, DIST)
    return px.reshape(-1, 2)


def scene(
    layout: Dict[int, Tuple[float, float]],
    turns: Optional[Dict[int, int]] = None,
    noise: float = 0.0,
    seed: int = 0,
    size_mm: float = SIZE_MM,
    tilt: float = 4.0,
):
    rng = np.random.default_rng(seed)
    rvec, tvec = true_pose(tilt)
    turns = turns or {}
    surveyed = {i: SurveyedMarker(tag_id=i, centre_cm=c, size_mm=SIZE_MM) for i, c in layout.items()}
    obs = []
    for i, c in layout.items():
        corners = project(pose.marker_object_points(c, size_mm, turns.get(i, 0)), rvec, tvec)
        corners = corners + rng.normal(0, noise, corners.shape)
        centre = project(np.array([[c[0], c[1], 0.0]]), rvec, tvec)[0] + rng.normal(0, noise, 2)
        obs.append(MarkerObservation(
            tag_id=i, centre=PixelPoint(x=float(centre[0]), y=float(centre[1])),
            corners=tuple(PixelPoint(x=float(x), y=float(y)) for x, y in corners),  # type: ignore[arg-type]
            frames=1, corner_spread_px=0.0))
    return obs, surveyed, (rvec, tvec)


def solve(obs, surveyed, turns):
    obj, img = pose.correspondences(obs, surveyed, turns)
    return pose.solve_planar_pose(obj, img, K, DIST), obj, img


# ---- conventions -----------------------------------------------------------


def test_upright_marker_top_edge_toward_north():
    p = pose.marker_object_points((10.0, 20.0), 46.0, 0)
    assert p.shape == (4, 3) and np.all(p[:, 2] == 0)
    # TL, TR, BR, BL
    assert p[0, :2] == pytest.approx([10 - 2.3, 20 + 2.3])
    assert p[1, :2] == pytest.approx([10 + 2.3, 20 + 2.3])
    assert p[2, :2] == pytest.approx([10 + 2.3, 20 - 2.3])
    assert p[3, :2] == pytest.approx([10 - 2.3, 20 - 2.3])


def test_quarter_turn_is_counter_clockwise_seen_from_above():
    # one CCW turn: the top edge (TL->TR, originally facing north) faces west
    p = pose.marker_object_points((0.0, 0.0), 46.0, 1)
    top_mid = (p[0, :2] + p[1, :2]) / 2
    assert top_mid == pytest.approx([-2.3, 0.0])
    assert p[0, :2] == pytest.approx([-2.3, -2.3])  # TL went north-west -> south-west
    # two turns: upside down, top edge faces south
    p2 = pose.marker_object_points((0.0, 0.0), 46.0, 2)
    assert (p2[0, :2] + p2[1, :2]) / 2 == pytest.approx([0.0, -2.3])
    # three turns == minus one: top edge faces east
    p3 = pose.marker_object_points((0.0, 0.0), 46.0, 3)
    assert (p3[0, :2] + p3[1, :2]) / 2 == pytest.approx([2.3, 0.0])
    assert pose.marker_object_points((0, 0), 46.0, 4) == pytest.approx(pose.marker_object_points((0, 0), 46.0, 0))


def test_look_down_camera_has_no_mirror():
    obs, surveyed, (rvec, tvec) = scene(CORNERS4, tilt=0.0)
    # marker 2 is north-east of marker 4; in the image east is right, north is up
    c = {o.tag_id: np.array([o.centre.x, o.centre.y]) for o in obs}
    assert c[2][0] > c[1][0] and c[2][1] == pytest.approx(c[1][1], abs=40)
    assert c[1][1] < c[4][1]  # north (y larger) is higher in the image (smaller v)


# ---- pose recovery ---------------------------------------------------------


def test_recovers_camera_two_noise_free():
    obs, surveyed, _ = scene(CORNERS4)
    turns = {o.tag_id: 0 for o in obs}
    ps, obj, img = solve(obs, surveyed, turns)
    height, nadir, tilt = pose.camera_position_from_pose(ps)
    assert len(obj) == 16
    assert height == pytest.approx(TRUE_HEIGHT, abs=0.1)
    assert (nadir.x, nadir.y) == pytest.approx(TRUE_NADIR, abs=0.1)
    assert tilt == pytest.approx(4.0, abs=0.1)
    assert ps.rms_px < 0.05


def test_recovers_camera_with_noise_within_two_cm():
    for seed in range(10):
        obs, surveyed, _ = scene(CORNERS4, noise=0.3, seed=seed)
        ps, _, _ = solve(obs, surveyed, {o.tag_id: 0 for o in obs})
        height, nadir, _ = pose.camera_position_from_pose(ps)
        assert abs(height - TRUE_HEIGHT) < 2.0
        assert math.hypot(nadir.x - TRUE_NADIR[0], nadir.y - TRUE_NADIR[1]) < 2.0


def test_homography_matches_true_mapping_off_the_markers():
    obs, surveyed, (rvec, tvec) = scene(CORNERS4)
    ps, _, _ = solve(obs, surveyed, {o.tag_id: 0 for o in obs})
    h = pose.homography_from_pose(ps, K)
    world = np.array([[0.0, 0.0], [20.0, -10.0], [-33.0, 21.0], [47.0, 5.0], [-10.0, -30.0]])
    raw = project(np.column_stack([world, np.zeros(len(world))]), rvec, tvec)
    und = pose.undistort_points(raw, K, DIST)
    got = pose._apply_h(h, und)
    assert got == pytest.approx(world, abs=0.05)
    # north-up / east = +x: a point east of the origin maps to larger x
    assert got[1, 0] > got[0, 0] and got[2, 1] > got[0, 1]


def test_reprojection_error_cm_small_on_clean_and_large_on_displaced():
    obs, surveyed, _ = scene(CORNERS4)
    ps, obj, img = solve(obs, surveyed, {o.tag_id: 0 for o in obs})
    err = pose.reprojection_error_cm(ps, K, DIST, obj, img)
    assert err.shape == (16,) and err.max() < 0.05
    bad = obj.copy()
    bad[:4, 0] += 5.0
    assert pose.reprojection_error_cm(ps, K, DIST, bad, img)[:4].min() == pytest.approx(5.0, abs=0.1)


def test_two_marker_diagonal_and_single_marker_still_solve():
    obs2, surveyed, _ = scene({1: CORNERS4[1], 3: CORNERS4[3]})
    ps2, obj, _ = solve(obs2, surveyed, {1: 0, 3: 0})
    assert len(obj) == 8
    h2, n2, _ = pose.camera_position_from_pose(ps2)
    assert h2 == pytest.approx(TRUE_HEIGHT, abs=2.0)
    assert ps2.ambiguity_ratio >= gates.MIN_AMBIGUITY_RATIO

    obs1, surveyed1, _ = scene({1: (30.0, 20.0)}, tilt=0.0)
    ps1, obj1, _ = solve(obs1, surveyed1, {1: 0})
    assert len(obj1) == 4
    assert ps1.ambiguity_ratio >= 1.0  # returned and flagged by the ratio; no exception


def test_pose_above_the_field_plane_not_mirrored():
    obs, surveyed, _ = scene(CORNERS4)
    ps, _, _ = solve(obs, surveyed, {o.tag_id: 0 for o in obs})
    assert pose.camera_position_from_pose(ps)[0] > 0


def test_solve_rejects_bad_input():
    with pytest.raises(ValueError):
        pose.solve_planar_pose(np.zeros((3, 3)), np.zeros((3, 2)), K, DIST)
    with pytest.raises(ValueError):
        pose.solve_planar_pose(np.ones((4, 3)), np.zeros((4, 2)), K, DIST)


# ---- orientation detection -------------------------------------------------


@pytest.mark.parametrize("q", [0, 1, 2, 3])
def test_all_markers_at_one_rotation_solve_like_upright(q):
    layout = {**CORNERS4, 5: (0.0, 0.0)}
    obs, surveyed, _ = scene(layout, turns={i: q for i in layout}, noise=0.2, seed=q)
    rots = pose.detect_marker_rotations(obs, surveyed, K, DIST)
    assert {i: r.quarter_turns for i, r in rots.items()} == {i: q for i in layout}
    ps, _, _ = solve(obs, surveyed, {i: r.quarter_turns for i, r in rots.items()})
    height, nadir, _ = pose.camera_position_from_pose(ps)
    assert abs(height - TRUE_HEIGHT) < 2.0
    assert math.hypot(nadir.x - TRUE_NADIR[0], nadir.y - TRUE_NADIR[1]) < 2.0


def test_main_field_mixed_orientations_detected_and_solved():
    obs, surveyed, (rvec, tvec) = scene(MAIN10, turns=MAIN_TURNS, noise=0.3, seed=7)
    rots = pose.detect_marker_rotations(obs, surveyed, K, DIST)
    assert {i: r.quarter_turns for i, r in rots.items()} == MAIN_TURNS
    assert all(r.ratio >= gates.MIN_ROTATION_RATIO for r in rots.values())
    ps, obj, img = solve(obs, surveyed, {i: r.quarter_turns for i, r in rots.items()})
    height, nadir, _ = pose.camera_position_from_pose(ps)
    assert abs(height - TRUE_HEIGHT) < 2.0
    err = pose.reprojection_error_cm(ps, K, DIST, obj, img)
    assert math.sqrt(np.mean(err ** 2)) < 0.5


def test_wrong_rotation_forced_gives_large_residual_and_ratio_reported():
    obs, surveyed, _ = scene(CORNERS4, turns={1: 1, 2: 0, 3: 0, 4: 0})
    right = {1: 1, 2: 0, 3: 0, 4: 0}
    wrong = {1: 0, 2: 0, 3: 0, 4: 0}
    assert solve(obs, surveyed, right)[0].rms_px < 0.05
    assert solve(obs, surveyed, wrong)[0].rms_px > 5.0
    rots = pose.detect_marker_rotations(obs, surveyed, K, DIST)
    assert rots[1].quarter_turns == 1
    assert rots[1].ratio > 5.0
    assert rots[1].residuals_cm[1] < 0.1 and rots[1].residuals_cm[0] > 2.0


def test_rotation_detection_with_two_and_three_markers():
    for layout in ({1: CORNERS4[1], 3: CORNERS4[3]}, {1: CORNERS4[1], 2: CORNERS4[2], 4: CORNERS4[4]}):
        turns = {i: (i + 1) % 4 for i in layout}
        obs, surveyed, _ = scene(layout, turns=turns, noise=0.1)
        rots = pose.detect_marker_rotations(obs, surveyed, K, DIST)
        assert {i: r.quarter_turns for i, r in rots.items()} == turns


def test_single_marker_rotation_is_flagged_ambiguous():
    obs, surveyed, _ = scene({1: (10.0, 10.0)}, turns={1: 2}, tilt=0.0)
    rots = pose.detect_marker_rotations(obs, surveyed, K, DIST)
    assert rots[1].ratio < gates.MIN_ROTATION_RATIO


# ---- leave-one-out and scale -----------------------------------------------


def test_leave_one_out_clean_is_small():
    obs, surveyed, _ = scene(MAIN10, turns=MAIN_TURNS, noise=0.3, seed=3)
    loo = pose.leave_one_out_residuals(obs, surveyed, MAIN_TURNS, K, DIST)
    assert set(loo) == set(MAIN10)
    worst = gates.per_marker_leave_one_out(loo)
    assert worst is not None and worst[1] < gates.MAX_LOO_CM


def test_leave_one_out_names_displaced_marker():
    obs, surveyed, _ = scene(MAIN10, turns=MAIN_TURNS, noise=0.2, seed=4)
    bad = dict(surveyed)
    bad[6] = SurveyedMarker(tag_id=6, centre_cm=(25.0, -36.0), size_mm=SIZE_MM)  # 6 cm off
    loo = pose.leave_one_out_residuals(obs, bad, MAIN_TURNS, K, DIST)
    worst = gates.per_marker_leave_one_out(loo)
    assert worst[0] == 6 and worst[1] > 4.0


def test_leave_one_out_names_swapped_pair():
    obs, surveyed, _ = scene(MAIN10, turns=MAIN_TURNS, noise=0.2, seed=5)
    swapped = dict(surveyed)
    swapped[9] = SurveyedMarker(tag_id=9, centre_cm=MAIN10[10], size_mm=SIZE_MM)
    swapped[10] = SurveyedMarker(tag_id=10, centre_cm=MAIN10[9], size_mm=SIZE_MM)
    loo = pose.leave_one_out_residuals(obs, swapped, MAIN_TURNS, K, DIST)
    worst = gates.per_marker_leave_one_out(loo)
    assert worst[0] in (9, 10) and worst[1] > gates.MAX_LOO_CM


def test_leave_one_out_not_applicable_below_three():
    obs, surveyed, _ = scene({1: CORNERS4[1], 3: CORNERS4[3]})
    assert pose.leave_one_out_residuals(obs, surveyed, {1: 0, 3: 0}, K, DIST) == {}


def test_scale_agreement_is_one_when_size_right_and_off_when_wrong():
    obs, surveyed, _ = scene(CORNERS4, noise=0.2, seed=1)
    ps, _, _ = solve(obs, surveyed, {o.tag_id: 0 for o in obs})
    local, glob = pose.marker_distances(obs, surveyed, ps, K, DIST)
    assert gates.scale_ok(gates.scale_agreement(local, glob))
    # rendered at 10 % larger print than the definition claims
    obs2, surveyed2, _ = scene(CORNERS4, size_mm=SIZE_MM * 1.1, noise=0.2, seed=1)
    ps2, _, _ = solve(obs2, surveyed2, {o.tag_id: 0 for o in obs2})
    l2, g2 = pose.marker_distances(obs2, surveyed2, ps2, K, DIST)
    ratio = gates.scale_agreement(l2, g2)
    assert not gates.scale_ok(ratio) and ratio < 0.95


def test_viewing_rays_spread_wide_for_corners_narrow_for_one_marker():
    obs, _, _ = scene(CORNERS4)
    pts = np.vstack([pose._corners_array(o) for o in obs])
    assert gates.angular_spread_deg(pose.viewing_rays(pts, K, DIST)) > gates.MIN_ANGULAR_SPREAD_DEG
    one = pose._corners_array(obs[0])
    assert gates.angular_spread_deg(pose.viewing_rays(one, K, DIST)) < gates.MIN_ANGULAR_SPREAD_DEG


def test_noise_sweep_clean_rms_below_gates():
    """Seeded sweep behind the gate defaults (numbers in the ticket notes)."""
    worst_px = worst_loo = 0.0
    for seed in range(20):
        obs, surveyed, _ = scene(MAIN10, turns=MAIN_TURNS, noise=0.3, seed=100 + seed)
        rots = pose.detect_marker_rotations(obs, surveyed, K, DIST)
        turns = {i: r.quarter_turns for i, r in rots.items()}
        ps, obj, img = solve(obs, surveyed, turns)
        err = pose.reprojection_error_cm(ps, K, DIST, obj, img)
        assert math.sqrt(np.mean(err ** 2)) < gates.MAX_RMS_CM
        worst_px = max(worst_px, ps.rms_px)
        loo = pose.leave_one_out_residuals(obs, surveyed, turns, K, DIST)
        worst_loo = max(worst_loo, max(loo.values()))
    assert worst_px < gates.MAX_RMS_PX and worst_loo < gates.MAX_LOO_CM


def test_collinear_layout_is_flagged_by_extent_gate():
    c4 = np.array(list(CORNERS4.values()))
    assert gates.layout_extent_ratio(c4) > gates.MIN_LAYOUT_EXTENT_RATIO
    diag = np.array([CORNERS4[1], CORNERS4[3]])
    assert gates.layout_extent_ratio(diag) < gates.MIN_LAYOUT_EXTENT_RATIO
    line3 = np.array([(-55.0, 35.0), (0.0, 35.0), (55.0, 35.0)])
    assert gates.layout_extent_ratio(line3) < gates.MIN_LAYOUT_EXTENT_RATIO


def test_pose_module_types_are_importable():
    assert pose.TargetObserver is not None
