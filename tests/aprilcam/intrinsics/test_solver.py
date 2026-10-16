"""Solver, good-enough metric, hint and banner tests on synthetic views."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from aprilcam.intrinsics.collector import object_points
from aprilcam.intrinsics.interfaces import (
    BANNER_GOOD_ENOUGH,
    BANNER_KEEP_GOING,
    BANNER_SHOW_BOARD,
    BoardSpec,
    DEFAULT_BOARD_SPEC,
    IntrinsicsFinishResult,
    IntrinsicsStatus,
    KeptView,
    derive_banner,
)
from aprilcam.intrinsics.solver import (
    GOOD_RESIDUAL_PX,
    GOOD_RMS_PX,
    LensSolver,
    check_plausible,
    next_hint,
)

SPEC = BoardSpec.parse(DEFAULT_BOARD_SPEC)
SIZE = (1280, 720)
FX = 900.0
K = np.array([[FX, 0, 640.0], [0, FX, 360.0], [0, 0, 1]])
BARREL = np.array([-0.30, 0.10, 0.0, 0.0, 0.0])
FLAT = np.zeros(5)
IDS = tuple(range((SPEC.cols - 1) * (SPEC.rows - 1)))
BOARD_CENTRE = np.array([(SPEC.cols * SPEC.square_mm) / 2000.0,
                         (SPEC.rows * SPEC.square_mm) / 2000.0, 0.0])


def make_view(dist, centre, depth, tilt, az, rng, noise=0.15):
    obj = object_points(SPEC, IDS)
    axis = np.array([-np.sin(np.radians(az)), np.cos(np.radians(az)), 0.0]) * np.radians(tilt)
    rvec = axis.reshape(3, 1)
    rot, _ = cv2.Rodrigues(rvec)
    t = np.array([(centre[0] - 640) / FX * depth, (centre[1] - 360) / FX * depth, depth])
    tvec = (t - rot @ BOARD_CENTRE).reshape(3, 1)
    pts, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
    pts = pts.reshape(-1, 2) + rng.normal(0, noise, (len(IDS), 2))
    return KeptView(corners=tuple((float(x), float(y)) for x, y in pts), ids=IDS,
                    tilt_deg=tilt, azimuth_deg=az,
                    extent=(float(pts[:, 0].min()), float(pts[:, 1].min()),
                            float(pts[:, 0].max()), float(pts[:, 1].max())),
                    timestamp=0.0)


def spread_views(dist, n=20, seed=1):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        centre = (rng.uniform(250, 1030), rng.uniform(150, 570))
        out.append(make_view(dist, centre, rng.uniform(0.45, 0.7),
                             rng.uniform(15, 45), rng.uniform(0, 360), rng))
    return out


@pytest.fixture(scope="module")
def barrel_eval():
    solver = LensSolver(SPEC, SIZE)
    return solver.evaluate(spread_views(BARREL))


def test_solve_none_until_six_usable_views():
    solver = LensSolver(SPEC)
    assert solver.solve(spread_views(BARREL, n=5), SIZE) is None
    assert solver.solve(spread_views(BARREL, n=6), SIZE) is not None


def test_solve_recovers_fx(barrel_eval):
    sol = barrel_eval.solution
    assert sol.fx == pytest.approx(FX, rel=0.03)
    assert sol.hfov_deg == pytest.approx(
        np.degrees(2 * np.arctan(640 / FX)), rel=0.04)
    assert sol.dist_coeffs[0] == pytest.approx(-0.30, abs=0.06)
    assert sol.rms_px < 0.5
    assert sol.plausibility_error is None


def test_well_spread_set_is_good(barrel_eval):
    assert barrel_eval.residual_px is not None
    assert barrel_eval.residual_px < GOOD_RESIDUAL_PX
    assert barrel_eval.rms_px < GOOD_RMS_PX
    assert barrel_eval.good


def test_evaluate_is_deterministic():
    views = spread_views(BARREL)
    a = LensSolver(SPEC, SIZE).evaluate(views)
    b = LensSolver(SPEC, SIZE).evaluate(views)
    assert a.residual_px == b.residual_px


def test_residual_none_below_min_views():
    ev = LensSolver(SPEC, SIZE).evaluate(spread_views(BARREL, n=8))
    assert ev is not None and ev.residual_px is None and not ev.good


def test_clustered_flat_views_not_good():
    rng = np.random.default_rng(3)
    views = [make_view(BARREL, (640 + rng.uniform(-20, 20), 360 + rng.uniform(-20, 20)),
                       0.6, rng.uniform(0, 3), rng.uniform(0, 360), rng, noise=0.6)
             for _ in range(14)]
    ev = LensSolver(SPEC, SIZE).evaluate(views)
    assert ev is not None
    assert not ev.good


def test_correction_small_for_rectilinear_large_for_barrel(barrel_eval):
    flat = LensSolver(SPEC, SIZE).evaluate(spread_views(FLAT))
    assert flat.correction_px < 1.5
    assert barrel_eval.correction_px > 10 * flat.correction_px
    assert barrel_eval.correction_px > 8.0


def test_plausibility_gate():
    bad = np.array([[50.0, 0, 640], [0, 50.0, 360], [0, 0, 1]])
    assert check_plausible(bad, SIZE) is not None
    assert check_plausible(K, SIZE) is None
    huge = np.array([[9000.0, 0, 640], [0, 9000.0, 360], [0, 0, 1]])
    assert check_plausible(huge, SIZE) is not None
    skew = np.array([[900.0, 0, 640], [0, 100.0, 360], [0, 0, 1]])
    assert check_plausible(skew, SIZE) is not None


def test_implausible_solve_is_not_good(monkeypatch):
    import aprilcam.intrinsics.solver as s
    monkeypatch.setattr(s, "FX_RATIO_MIN", 5.0)
    monkeypatch.setattr(s, "FX_RATIO_MAX", 6.0)
    ev = LensSolver(SPEC, SIZE).evaluate(spread_views(BARREL))
    assert ev.solution.plausibility_error
    assert not ev.good


# ---- hints -----------------------------------------------------------------

FULL = (3,) * 9
ALL_DIRS = {"N": 2, "E": 2, "S": 2, "W": 2}


def test_hint_few_views():
    assert "HOLD THE BOARD UP HIGH" in next_hint(FULL, ALL_DIRS, 8, 3)


def test_hint_missing_corner_with_tilt_direction():
    cov = (0, 3, 3, 3, 3, 3, 3, 3, 3)
    h = next_hint(cov, {"N": 1, "E": 0, "S": 1, "W": 1}, 3, 10)
    assert h == "TOP-LEFT corner, tilt it (left edge up)"


def test_hint_missing_corner_all_dirs_present():
    cov = (0, 3, 3, 3, 3, 3, 3, 3, 3)
    assert next_hint(cov, ALL_DIRS, 8, 10).startswith("TOP-LEFT corner, tilt it")


def test_hint_missing_tilt_directions():
    h = next_hint(FULL, {"N": 1, "E": 1, "S": 0, "W": 1}, 3, 10)
    assert h == "tilt more: top edge up, anywhere"


def test_hint_missing_edge():
    cov = (3, 3, 3, 3, 3, 3, 3, 1, 3)
    assert next_hint(cov, ALL_DIRS, 8, 10) == "BOTTOM edge, tilted"


def test_hint_more_tilted_views():
    assert "TILTED" in next_hint(FULL, ALL_DIRS, 9, 20)


def test_hint_keep_sweeping_and_mapping_input():
    cov = dict(zip(("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE"), FULL))
    assert next_hint(cov, ALL_DIRS, 10, 20) == "keep sweeping slowly, high and tilted"


# ---- banner / status --------------------------------------------------------

def test_banner_derivation():
    assert derive_banner(board_visible=False, view_count=0, good=False) == BANNER_SHOW_BOARD
    assert derive_banner(board_visible=True, view_count=0, good=False) == BANNER_KEEP_GOING
    assert derive_banner(board_visible=False, view_count=5, good=False) == BANNER_KEEP_GOING
    assert derive_banner(board_visible=True, view_count=15, good=True) == BANNER_GOOD_ENOUGH


def test_status_and_finish_result_records():
    st = IntrinsicsStatus(camera="cam0", banner=derive_banner(
        board_visible=True, view_count=1, good=False))
    assert st.state == "collecting" and st.layout.value == "unknown"
    assert st.banner == BANNER_KEEP_GOING and st.errors == ()
    res = IntrinsicsFinishResult(installed=False, reason="implausible", status=st)
    assert res.lens is None
    with pytest.raises(Exception):
        st.view_count = 3  # frozen
