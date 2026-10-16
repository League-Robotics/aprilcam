"""ChArUco target observer and the Calibrator target path (sprint 045 ticket 011).

Synthetic, hardware-free: a real ChArUco board is resampled through the true
camera pose and lens (the same ``cv2.remap`` approach as the marker scenes in
``tests/aprilcam/calibration/test_calibration_pose.py``), so detect -> pose ->
gates runs on actual pixels. The board's physical placement is written here
independently of ``intrinsics/target.py`` so the y-flip and rotation
conventions are pinned, not echoed.
"""
from __future__ import annotations

import ast
import dataclasses
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from aprilcam.config.interfaces import CalibrationTarget
from aprilcam.intrinsics.interfaces import BoardSpec
from aprilcam.intrinsics.target import ChArUcoTargetObserver, board_to_field_cm
from aprilcam.types import PixelPoint
from tests.aprilcam.calibration.test_calibration_pose import (
    CAM,
    FIELD_POINTS,
    H,
    LAYOUT4,
    W,
    calibrator,
    definition,
    lens_model,
    render as render_markers,
    world_error,
)
from tests.aprilcam.calibration.test_pose import DIST, K, true_pose

BOARD = "5x7:60:45:DICT_5X5_100"
SPEC = BoardSpec.parse(BOARD)
PX_PER_MM = 8.0
BW_MM, BH_MM = SPEC.cols * SPEC.square_mm, SPEC.rows * SPEC.square_mm
_BOARD_IMG = None


def board_image() -> np.ndarray:
    global _BOARD_IMG
    if _BOARD_IMG is None:
        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, SPEC.dict_name))
        b = cv2.aruco.CharucoBoard(
            (SPEC.cols, SPEC.rows), SPEC.square_mm / 1000, SPEC.marker_mm / 1000, d)
        _BOARD_IMG = b.generateImage(
            (int(BW_MM * PX_PER_MM), int(BH_MM * PX_PER_MM)), marginSize=0, borderBits=1)
    return _BOARD_IMG


def render_board(centre=(0.0, 0.0), rotation_deg=0.0) -> np.ndarray:
    """BGR frame of the board lying on the field at *centre*, turned CCW."""
    rvec, tvec = true_pose()
    rot, _ = cv2.Rodrigues(rvec.reshape(3, 1))
    us, vs = np.meshgrid(np.arange(W), np.arange(H))
    grid = np.stack([us.ravel(), vs.ravel()], axis=1).astype(np.float64)
    xy = cv2.undistortPoints(grid.reshape(-1, 1, 2), K, np.asarray(DIST, float)).reshape(-1, 2)
    d = np.column_stack([xy, np.ones(len(xy))])
    rt_t = rot.T @ tvec.reshape(3)
    rt_d = d @ rot
    lam = rt_t[2] / rt_d[:, 2]
    world = lam[:, None] * rt_d - rt_t  # field cm per pixel
    t = math.radians(rotation_deg)
    c, s = math.cos(t), math.sin(t)
    dx, dy = world[:, 0] - centre[0], world[:, 1] - centre[1]
    bx = c * dx + s * dy  # inverse (clockwise) rotation into the board's own frame
    by = -s * dx + c * dy
    map_x = ((bx * 10.0 + BW_MM / 2) * PX_PER_MM).astype(np.float32).reshape(H, W)
    map_y = ((-by * 10.0 + BH_MM / 2) * PX_PER_MM).astype(np.float32).reshape(H, W)
    gray = cv2.remap(board_image(), map_x, map_y, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def target_def(*, layout=None, centre=(0.0, 0.0), rotation=0.0):
    pf = definition(layout or {})
    return dataclasses.replace(pf, calibration_target=CalibrationTarget(
        kind="charuco", board=BOARD, centre_x_cm=centre[0], centre_y_cm=centre[1],
        rotation_deg=rotation))


def cal_with_observer(pf, frame, **kw):
    cal = calibrator(pf, frame, lens=kw.pop("lens", lens_model()), **kw)
    cal._observe_target = ChArUcoTargetObserver()
    return cal


def pixel_of(x, y):
    rvec, tvec = true_pose()
    px, _ = cv2.projectPoints(np.array([[x, y, 0.0]]), rvec, tvec, K, DIST)
    return PixelPoint(x=float(px[0, 0, 0]), y=float(px[0, 0, 1]))


@pytest.fixture(scope="module")
def board_frame():
    return render_board()


# ---- conventions (unit) -----------------------------------------------------


def test_first_corner_is_north_west_of_an_upright_board():
    t = CalibrationTarget(kind="charuco", board=BOARD)
    p = board_to_field_cm(SPEC, [0, 1, 4], t)
    # id 0 is lattice (col 0, row 0): one square in from the top-left board corner.
    assert p[0] == pytest.approx([6 - 15, -(6 - 21)])  # west, NORTH (the one y flip)
    assert p[1, 0] > p[0, 0] and p[1, 1] == pytest.approx(p[0, 1])  # ids increase eastward
    assert p[0, 1] > 0  # top row is north of the centre


def test_rotation_is_counter_clockwise_and_centre_translates():
    t = CalibrationTarget(kind="charuco", board=BOARD, centre_x_cm=10.0, centre_y_cm=-5.0,
                          rotation_deg=90.0)
    p0 = board_to_field_cm(SPEC, [0], CalibrationTarget(kind="charuco", board=BOARD))[0]
    p = board_to_field_cm(SPEC, [0], t)[0]
    # (x, y) -> (-y, x) under a CCW quarter turn, then moved to the centre.
    assert p == pytest.approx([10.0 - p0[1], -5.0 + p0[0]])


# ---- observer ---------------------------------------------------------------


def test_observer_returns_field_corners(board_frame):
    gray = cv2.cvtColor(board_frame, cv2.COLOR_BGR2GRAY)
    corners = ChArUcoTargetObserver()(gray, CalibrationTarget(kind="charuco", board=BOARD))
    assert len(corners) >= 24
    rvec, tvec = true_pose()
    for c in corners:
        px, _ = cv2.projectPoints(
            np.array([[c.field_cm[0], c.field_cm[1], 0.0]]), rvec, tvec, K, DIST)
        assert np.hypot(px[0, 0, 0] - c.pixel.x, px[0, 0, 1] - c.pixel.y) < 1.5


def test_observer_blank_frame_and_bad_kind():
    obs = ChArUcoTargetObserver()
    assert obs(np.full((H, W), 255, np.uint8), CalibrationTarget(kind="charuco", board=BOARD)) == []
    with pytest.raises(ValueError):
        obs(np.zeros((H, W), np.uint8), CalibrationTarget(kind="qr", board=BOARD))


# ---- calibrator target path -------------------------------------------------


def test_marker_free_definition_calibrates_from_the_board(board_frame):
    cal = cal_with_observer(target_def(), board_frame)
    result = cal.calibrate(CAM)
    assert result.succeeded, result.reason
    assert result.method == "pose"
    assert result.corner_count >= 24
    assert result.matched_ids == ()
    assert any("scale agreement not applicable" in n for n in result.notes)
    assert world_error(cal.get_calibration(CAM), FIELD_POINTS) < 0.6


def test_board_and_four_marker_calibrations_agree(board_frame):
    markers = calibrator(definition(LAYOUT4), render_markers(LAYOUT4), lens=lens_model())
    assert markers.calibrate(CAM).succeeded
    board = cal_with_observer(target_def(), board_frame)
    assert board.calibrate(CAM).succeeded
    a, b = markers.get_calibration(CAM), board.get_calibration(CAM)
    worst = 0.0
    # A grid over the field, mostly away from the board (which spans x +/-15, y +/-21).
    for x in range(-50, 51, 10):
        for y in range(-30, 31, 10):
            px = pixel_of(x, y)
            wa, wb = a.to_world(px), b.to_world(px)
            worst = max(worst, math.hypot(wa.x - wb.x, wa.y - wb.y))
    print(f"board vs four-marker agreement: worst {worst:.3f} cm")
    assert worst < 0.5


def test_declared_centre_shifts_the_field_frame(board_frame):
    cal = cal_with_observer(target_def(centre=(20.0, 10.0)), board_frame)
    assert cal.calibrate(CAM).succeeded
    w = cal.get_calibration(CAM).to_world(pixel_of(0.0, 0.0))  # physical board centre
    assert (w.x, w.y) == pytest.approx((20.0, 10.0), abs=0.5)


def test_declared_rotation_turns_the_field_frame_without_mirror(board_frame):
    cal = cal_with_observer(target_def(rotation=90.0), board_frame)
    assert cal.calibrate(CAM).succeeded
    # The board is physically upright; declaring it turned 90 deg CCW means the
    # physical east point (5, 0) is declared north: (0, 5).
    w = cal.get_calibration(CAM).to_world(pixel_of(5.0, 0.0))
    assert (w.x, w.y) == pytest.approx((0.0, 5.0), abs=0.5)


def test_board_rendered_rotated_and_declared_rotated_agree():
    frame = render_board(centre=(15.0, -8.0), rotation_deg=30.0)
    cal = cal_with_observer(target_def(centre=(15.0, -8.0), rotation=30.0), frame)
    result = cal.calibrate(CAM)
    assert result.succeeded, result.reason
    assert world_error(cal.get_calibration(CAM), FIELD_POINTS) < 0.6


def test_board_not_found_fails_and_keeps_previous(board_frame):
    cal = cal_with_observer(target_def(), board_frame)
    assert cal.calibrate(CAM).succeeded
    before = cal.get_calibration(CAM)
    blank = np.full((H, W, 3), 255, np.uint8)
    cal._capture_frames = lambda slug: [blank]
    result = cal.calibrate(CAM)
    assert not result.succeeded
    assert "not found" in result.reason
    assert cal.get_calibration(CAM) is before


def test_too_few_corners_fails_with_reason(board_frame):
    cal = cal_with_observer(target_def(), board_frame)
    real = cal._observe_target
    cal._observe_target = lambda gray, target: real(gray, target)[:3]
    result = cal.calibrate(CAM)
    assert not result.succeeded
    assert "only 3 board corners" in result.reason
    assert cal.get_calibration(CAM) is None


def test_target_without_measured_lens_fails_clearly(board_frame):
    cal = calibrator(target_def(), board_frame, lens=None)
    cal._observe_target = ChArUcoTargetObserver()
    result = cal.calibrate(CAM)
    assert not result.succeeded
    assert "measured lens" in result.reason


def test_markers_and_target_are_pooled():
    frame = np.minimum(render_markers(LAYOUT4), render_board())
    cal = cal_with_observer(target_def(layout=LAYOUT4), frame)
    result = cal.calibrate(CAM)
    assert result.succeeded, result.reason
    assert "markers+target" in result.method_reason
    assert result.corner_count >= 16 + 24
    assert world_error(cal.get_calibration(CAM), FIELD_POINTS) < 0.6


def test_unreadable_target_falls_back_to_markers_with_a_note():
    frame = render_markers(LAYOUT4)  # markers only: the board is absent
    cal = cal_with_observer(target_def(layout=LAYOUT4), frame)
    result = cal.calibrate(CAM)
    assert result.succeeded, result.reason
    assert result.corner_count == 16
    assert any("calibration target not usable" in n for n in result.notes)


def test_no_observer_ignores_the_target():
    frame = render_markers(LAYOUT4)
    cal = calibrator(target_def(layout=LAYOUT4), frame, lens=lens_model())
    result = cal.calibrate(CAM)
    assert result.succeeded and result.corner_count == 16
    assert not any("target" in n for n in result.notes)


# ---- import graph -----------------------------------------------------------


def test_calibration_never_imports_intrinsics():
    root = Path(__file__).resolve().parents[3] / "src" / "aprilcam" / "calibration"
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [
                    f"{node.module}.{a.name}" for a in node.names]
            if any(n == "aprilcam.intrinsics" or n.startswith("aprilcam.intrinsics.")
                   for n in names):
                offenders.append(f"{path.name}: {names}")
    assert not offenders
