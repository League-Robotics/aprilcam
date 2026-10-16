"""Hardware-free tests for the ChArUco view collector."""
from __future__ import annotations

import ast
import pathlib

import cv2

from aprilcam.intrinsics import collector as collector_mod
from aprilcam.intrinsics.collector import ViewCollector
from aprilcam.intrinsics.interfaces import RejectReason

from .boards import SPEC, detect, render

CEN = (640, 360)


def _offer(col, gray, ts=0.0):
    boards = detect(gray)
    assert len(boards) == 1
    return col.offer(gray, boards, ts)[0]


def _prime(col, gray):
    """First sight of a board is 'moving' (no previous frame); offer twice."""
    first = _offer(col, gray, 0.0)
    assert first.reason is RejectReason.MOVING
    return _offer(col, gray, 1.0)


def test_flat_board_pose_untrusted_and_no_tilt_ticks():
    col = ViewCollector(SPEC)
    g = render(CEN, 0.5, 0)
    v = _prime(col, g)
    assert v.accepted and not v.pose_confident and v.tilt_deg is None
    cov = col.coverage()
    assert cov.tilted_count == 0 and sum(cov.tilt_directions.values()) == 0


def test_flat_duplicate_is_ambiguous_pose_not_novel_by_tilt():
    col = ViewCollector(SPEC)
    g = render(CEN, 0.5, 0)
    _prime(col, g)
    v = _offer(col, g, 2.0)
    assert not v.accepted and v.reason is RejectReason.AMBIGUOUS_POSE


def test_steep_tilt_accepted_and_counted():
    col = ViewCollector(SPEC)
    v = _prime(col, render(CEN, 0.5, 35, 90))
    assert v.accepted and v.pose_confident
    assert abs(v.tilt_deg - 35) < 3
    cov = col.coverage()
    assert cov.tilted_count == 1 and sum(cov.tilt_directions.values()) == 1
    kept = col.views[0]
    assert kept.tilt_deg == v.tilt_deg and len(kept.corners) == len(kept.ids) >= 18


def test_tilt_directions_distinct():
    col = ViewCollector(SPEC)
    for az, cx in ((0, 400), (90, 560), (180, 720), (270, 880)):
        _prime(col, render((cx, 360), 0.5, 35, az))
        col._prev = []  # forget the previous frame between distinct poses
    d = col.coverage().tilt_directions
    assert sorted(d.values()) == [1, 1, 1, 1]


def test_tilt_change_makes_same_position_novel():
    col = ViewCollector(SPEC)
    _prime(col, render(CEN, 0.5, 30, 90))
    col._prev = []
    v = _prime(col, render(CEN, 0.5, 45, 90))
    assert v.accepted


def test_not_novel_when_confident_and_unchanged():
    col = ViewCollector(SPEC)
    g = render(CEN, 0.5, 35, 90)
    _prime(col, g)
    v = _offer(col, g, 2.0)
    assert not v.accepted and v.reason is RejectReason.NOT_NOVEL


def test_novel_after_translation():
    col = ViewCollector(SPEC)
    _prime(col, render((540, 360), 0.5, 35, 90))
    col._prev = []
    v = _prime(col, render((700, 360), 0.5, 35, 90))
    assert v.accepted and len(col.views) == 2


def test_relative_blur_gate():
    col = ViewCollector(SPEC)
    sharp = render(CEN, 0.5, 35, 90)
    _prime(col, sharp)  # establishes the camera's best sharpness
    blurred = cv2.GaussianBlur(sharp, (0, 0), 2.5)
    boards = detect(blurred)
    assert len(boards) == 1
    col.offer(blurred, boards, 2.0)
    v = col.offer(blurred, boards, 3.0)[0]
    assert not v.accepted and v.reason is RejectReason.BLURRY


def test_motion_gate_fast_vs_slow():
    fast = ViewCollector(SPEC)
    _offer(fast, render((400, 360), 0.5, 35, 90), 0)
    v = _offer(fast, render((460, 360), 0.5, 35, 90), 1)
    assert not v.accepted and v.reason is RejectReason.MOVING

    slow = ViewCollector(SPEC)
    _offer(slow, render((400, 360), 0.5, 35, 90), 0)
    v = _offer(slow, render((406, 360), 0.5, 35, 90), 1)
    assert v.accepted


def test_too_few_corners():
    col = ViewCollector(SPEC)
    g = render(CEN, 0.5, 35, 90)
    boards = detect(g)
    from aprilcam.intrinsics.interfaces import BoardDetection
    b = boards[0]
    small = BoardDetection(corners=b.corners[:8], ids=b.ids[:8],
                           marker_count=b.marker_count, layout=b.layout)
    col.offer(g, [small], 0)
    v = col.offer(g, [small], 1)[0]
    assert not v.accepted and v.reason is RejectReason.TOO_FEW_CORNERS


def test_coverage_by_extent_not_centroid():
    col = ViewCollector(SPEC)
    v = _prime(col, render(CEN, 0.36, 35, 90))
    assert v.accepted
    cov = col.coverage()
    names = collector_mod.REGION_NAMES
    counts = dict(zip(names, cov.region_counts))
    # Board centroid is in C, but its extent reaches the outer bands too.
    assert counts["C"] == 1
    assert sum(1 for n in names if counts[n]) >= 4
    assert counts["W"] == 1 and counts["E"] == 1


def test_collectors_independent():
    a, b = ViewCollector(SPEC), ViewCollector(SPEC)
    _prime(a, render(CEN, 0.5, 35, 90))
    assert len(a.views) == 1 and len(b.views) == 0
    assert b.coverage().view_count == 0
    # b still has no previous frame: its first sight is "moving".
    assert _offer(b, render(CEN, 0.5, 35, 90)).reason is RejectReason.MOVING


def test_no_module_level_state_and_no_frames_kept():
    tree = ast.parse(pathlib.Path(collector_mod.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            assert not isinstance(node.value, (ast.List, ast.Dict, ast.Set, ast.Call))
    col = ViewCollector(SPEC)
    _prime(col, render(CEN, 0.5, 35, 90))
    assert not any(hasattr(v, "frame") or hasattr(v, "gray") for v in col.views)
