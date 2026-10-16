"""Hardware-free tests for the ChArUco board spec and multi-board detector."""
from __future__ import annotations

import ast
import pathlib

import cv2
import numpy as np
import pytest

from aprilcam.intrinsics import interfaces
from aprilcam.intrinsics.detector import BoardDetector, marker_clusters
from aprilcam.intrinsics.interfaces import (
    BOARD_ENV_VAR,
    DEFAULT_BOARD_SPEC,
    BoardLayout,
    BoardSpec,
    resolve_board_spec,
)

SPEC = BoardSpec.parse(DEFAULT_BOARD_SPEC)


def _board(legacy: bool = False, spec: BoardSpec = SPEC):
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, spec.dict_name))
    b = cv2.aruco.CharucoBoard((spec.cols, spec.rows), spec.square_mm / 1000,
                               spec.marker_mm / 1000, d)
    b.setLegacyPattern(legacy)
    return b


def _place(canvas, legacy, origin, width, jitter=0, spec=SPEC):
    """Draw a board of pixel *width* with its top-left near *origin*, mildly warped."""
    h = int(width * spec.rows / spec.cols)
    img = _board(legacy, spec).generateImage((width, h), marginSize=0, borderBits=1)
    src = np.float32([[0, 0], [width, 0], [width, h], [0, h]])
    ox, oy = origin
    dst = np.float32([[ox, oy], [ox + width, oy + jitter], [ox + width - jitter, oy + h],
                      [ox, oy + h - jitter]])
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, m, canvas.shape[::-1], flags=cv2.INTER_LINEAR,
                                 borderValue=0)
    mask = cv2.warpPerspective(np.full_like(img, 255), m, canvas.shape[::-1])
    canvas[mask > 0] = warped[mask > 0]


def _canvas(w=1200, h=800):
    return np.full((h, w), 255, np.uint8)


def _field_marker(canvas, marker_id, x, y, size=60):
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)
    m = cv2.aruco.generateImageMarker(d, marker_id, size)
    canvas[y:y + size, x:x + size] = m


# ---- BoardSpec ---------------------------------------------------------------

def test_default_constant():
    assert DEFAULT_BOARD_SPEC == "5x7:37:28:DICT_5X5_100"


def test_parse_ok():
    s = BoardSpec.parse("5x7:37:28:DICT_5X5_100")
    assert (s.cols, s.rows, s.square_mm, s.marker_mm, s.dict_name) == (
        5, 7, 37.0, 28.0, "DICT_5X5_100")


@pytest.mark.parametrize("bad", [
    "", "5x7", "5x7:37:28", "5x7:37:28:DICT_5X5_100:x", "5:37:28:DICT_5X5_100",
    "axb:37:28:DICT_5X5_100", "5x7:x:28:DICT_5X5_100", "1x7:37:28:DICT_5X5_100",
    "5x7:-1:28:DICT_5X5_100", "5x7:28:37:DICT_5X5_100", "5x7:37:28:ARUCO",
])
def test_parse_refuses_malformed(bad):
    with pytest.raises(ValueError):
        BoardSpec.parse(bad)


def test_resolution_order(monkeypatch):
    monkeypatch.delenv(BOARD_ENV_VAR, raising=False)
    assert resolve_board_spec() == SPEC
    monkeypatch.setenv(BOARD_ENV_VAR, "6x8:28:22:DICT_4X4_50")
    assert resolve_board_spec().cols == 6
    assert resolve_board_spec("3x4:20:15:DICT_4X4_50").cols == 3  # request wins
    monkeypatch.setenv(BOARD_ENV_VAR, "junk")
    with pytest.raises(ValueError):
        resolve_board_spec()


def test_interfaces_imports_no_cv2():
    tree = ast.parse(pathlib.Path(interfaces.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "cv2" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "cv2"


# ---- clustering --------------------------------------------------------------

def _sq(cx, cy, s=10):
    return np.float32([[cx, cy], [cx + s, cy], [cx + s, cy + s], [cx, cy + s]]).reshape(1, 4, 2)


def test_clusters_split_and_filters():
    # Group A: 4 markers close together; B: 4 far away; C: 2 markers (too few);
    # D: 4 markers with a duplicated id (rejected).
    corners, ids = [], []
    for k in range(4):
        corners.append(_sq(k * 15, 0)); ids.append(k)
    for k in range(4):
        corners.append(_sq(500 + k * 15, 0)); ids.append(10 + k)
    for k in range(2):
        corners.append(_sq(k * 15, 500)); ids.append(20 + k)
    for k in range(4):
        corners.append(_sq(500 + k * 15, 500)); ids.append(30 + (k % 3))
    out = marker_clusters(corners, np.array(ids).reshape(-1, 1))
    assert sorted(sorted(c[1].ravel().tolist()) for c in out) == [[0, 1, 2, 3], [10, 11, 12, 13]]


def test_clusters_empty():
    assert marker_clusters([], None) == []


# ---- detector ----------------------------------------------------------------

def test_single_board_native_coordinates():
    canvas = _canvas()
    _place(canvas, False, (300, 100), 400, jitter=20)
    det = BoardDetector(SPEC)
    recs = det.detect(canvas)
    assert len(recs) == 1
    r = recs[0]
    assert r.marker_count >= 10 and len(r.ids) >= 12
    xs = [p[0] for p in r.corners]; ys = [p[1] for p in r.corners]
    assert 290 <= min(xs) and max(xs) <= 710 and 90 <= min(ys) and max(ys) <= 640
    assert det.layout is BoardLayout.CURRENT


def test_field_markers_do_not_contaminate_board():
    canvas = _canvas()
    _place(canvas, False, (300, 100), 400)
    # Same dictionary, ids overlapping the board's own, scattered far away
    # from it (and close to nothing else).
    for i, (x, y) in enumerate([(60, 60), (60, 400), (60, 700), (1100, 60), (1100, 700)]):
        _field_marker(canvas, i, x, y, 50)
    recs = BoardDetector(SPEC).detect(canvas)
    assert len(recs) == 1
    assert recs[0].marker_count >= 10


def test_two_boards_two_records():
    canvas = _canvas(1600, 700)
    _place(canvas, False, (50, 100), 350)
    _place(canvas, False, (900, 100), 350)
    recs = BoardDetector(SPEC).detect(canvas)
    assert len(recs) == 2
    cx = sorted(np.mean([p[0] for p in r.corners]) for r in recs)
    assert cx[0] < 500 < 800 < cx[1]


def test_upscale_finds_small_markers():
    canvas = _canvas(800, 600)
    _place(canvas, False, (200, 150), 130)  # ~14 px markers
    recs = BoardDetector(SPEC).detect(canvas)
    assert len(recs) == 1 and len(recs[0].ids) >= 8


# Legacy and current layouts only differ on boards with an even row count
# (5x7 is identical in both), so the layout tests use the 6x8 calib.io board.
EVEN = BoardSpec.parse("6x8:28:22:DICT_4X4_50")


def test_layout_autodetect_legacy_and_lock():
    det = BoardDetector(EVEN)
    assert det.layout is BoardLayout.UNKNOWN
    canvas = _canvas()
    _place(canvas, True, (300, 50), 360, spec=EVEN)
    recs = det.detect(canvas)
    assert len(recs) == 1 and recs[0].layout is BoardLayout.LEGACY
    assert det.layout is BoardLayout.LEGACY
    # Locked: a current-layout board is no longer interpolated by this instance,
    # and a fresh instance is unaffected (no module-global state).
    cur = _canvas()
    _place(cur, False, (300, 50), 360, spec=EVEN)
    assert det.detect(cur) == []
    fresh = BoardDetector(EVEN)
    assert fresh.layout is BoardLayout.UNKNOWN
    assert len(fresh.detect(cur)) == 1 and fresh.layout is BoardLayout.CURRENT


def test_blank_frame():
    assert BoardDetector(SPEC).detect(_canvas()) == []
