"""Tests for static-marker fill-in and movement invalidation.

Sprint 011, ticket 006.  Covers, fully synthetically (no camera / no real
ArUco markers — ``_detect_corners`` is mocked):

- fill-in: a static marker missing from the current frame HOLDS its stored
  pixel position; the polygon stays stable across the gap.
- dynamic not held: a dynamic AprilTag (id != 1) that disappears is NOT held.
- movement invalidation: a static marker displaced beyond the threshold sets
  the ``calibration_stale`` flag and logs a warning; below threshold does not.
- sentinel: AprilTag 1 trips invalidation even when corner markers are occluded.
- configurability: the threshold and the static set are configurable.
- daemon surfacing: the stale flag is written into ``info.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.calibration.geometry import DEFAULT_MOVEMENT_THRESHOLD_PX  # noqa: E402
from aprilcam.core.playfield import PlayfieldBoundary  # noqa: E402

pytestmark = pytest.mark.needs_cv2


# Stored calibration-time positions: four ArUco corners (UL/UR/LR/LL) plus the
# AprilTag 1 sentinel near the frame centre (away from the edges).
_CORNERS = {
    "corner:UL": (100.0, 100.0),
    "corner:UR": (500.0, 100.0),
    "corner:LR": (500.0, 400.0),
    "corner:LL": (100.0, 400.0),
}
_APRIL1 = {"apriltag:1": (300.0, 250.0)}
_STORED = {**_CORNERS, **_APRIL1}

_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


def _boundary(**kwargs) -> PlayfieldBoundary:
    return PlayfieldBoundary(static_markers=dict(_STORED), **kwargs)


def _mock_corners(boundary: PlayfieldBoundary, corners_present: Dict[int, Tuple[float, float]]):
    """Patch ``_detect_corners`` to return a fixed ArUco corner map.

    ArUco ids 0-3 map to UL/UR/LR/LL; pass a subset to simulate occlusion.
    """
    boundary._detect_corners = lambda frame, gray=None: dict(corners_present)


# ArUco id -> stored corner pixel (the layout the ordering produces).
_ARUCO = {
    0: _CORNERS["corner:UL"],
    1: _CORNERS["corner:UR"],
    2: _CORNERS["corner:LL"],
    3: _CORNERS["corner:LR"],
}


# ---------------------------------------------------------------------------
# Fill-in
# ---------------------------------------------------------------------------


def test_static_marker_missing_is_held_at_stored_position():
    b = _boundary()
    # Frame 1: all four corners + AprilTag 1 detected at stored positions.
    _mock_corners(b, _ARUCO)
    b.update(_FRAME, apriltags={1: _APRIL1["apriltag:1"]})
    poly_full = b.get_polygon()
    assert poly_full is not None

    # Frame 2: one corner (LR, ArUco 3) drops out + AprilTag 1 drops out.
    partial = {k: v for k, v in _ARUCO.items() if k != 3}
    _mock_corners(b, partial)
    b.update(_FRAME, apriltags={})  # nothing detected for the missing ones

    poly_held = b.get_polygon()
    assert poly_held is not None
    # The polygon is unchanged: the missing LR corner held its stored position.
    np.testing.assert_allclose(poly_held, poly_full, atol=1e-6)
    # The held AprilTag 1 stayed at its stored position too.
    assert b._held_static["apriltag:1"] == _APRIL1["apriltag:1"]


def test_static_set_seeds_polygon_before_any_frame():
    b = _boundary()
    # The held corner seed is present from construction; the first (even empty)
    # update materialises the polygon from the held corner positions.
    _mock_corners(b, {})
    b.update(_FRAME, apriltags={})
    poly = b.get_polygon()
    assert poly is not None
    expected = np.array(
        [_CORNERS["corner:UL"], _CORNERS["corner:UR"],
         _CORNERS["corner:LR"], _CORNERS["corner:LL"]],
        dtype=np.float32,
    )
    np.testing.assert_allclose(poly, expected, atol=1e-6)


# ---------------------------------------------------------------------------
# Dynamic tags are never held
# ---------------------------------------------------------------------------


def test_dynamic_apriltag_is_not_held():
    b = _boundary()
    _mock_corners(b, _ARUCO)
    # Frame 1: dynamic tag 7 present.
    b.update(_FRAME, apriltags={1: _APRIL1["apriltag:1"], 7: (320.0, 240.0)})
    # Dynamic id 7 must NOT enter the static hold set.
    assert "apriltag:7" not in b._held_static
    assert 7 not in {int(k.split(":")[1]) for k in b._held_static if k.startswith("apriltag:")}

    # Frame 2: dynamic tag 7 disappears — it must not be held anywhere.
    b.update(_FRAME, apriltags={1: _APRIL1["apriltag:1"]})
    assert "apriltag:7" not in b._held_static
    # And moving the dynamic tag wildly does NOT trip invalidation.
    b.update(_FRAME, apriltags={1: _APRIL1["apriltag:1"], 7: (10.0, 10.0)})
    assert b.calibration_stale is False


# ---------------------------------------------------------------------------
# Movement invalidation
# ---------------------------------------------------------------------------


def test_movement_beyond_threshold_trips_flag_and_warns(caplog):
    b = _boundary(movement_threshold_px=25.0)
    # Displace the LR corner (ArUco 3) far beyond threshold.
    moved = dict(_ARUCO)
    moved[3] = (_CORNERS["corner:LR"][0] + 80.0, _CORNERS["corner:LR"][1] + 80.0)
    _mock_corners(b, moved)
    with caplog.at_level(logging.WARNING, logger="aprilcam.core.playfield"):
        b.update(_FRAME, apriltags={1: _APRIL1["apriltag:1"]})
    assert b.calibration_stale is True
    assert any("moved" in rec.message and "corner:LR" in rec.message
               for rec in caplog.records)


def test_movement_below_threshold_does_not_trip():
    b = _boundary(movement_threshold_px=25.0)
    # Jitter every corner by < threshold.
    jittered = {k: (v[0] + 5.0, v[1] - 4.0) for k, v in _ARUCO.items()}
    _mock_corners(b, jittered)
    b.update(_FRAME, apriltags={1: (_APRIL1["apriltag:1"][0] + 3.0,
                                    _APRIL1["apriltag:1"][1] + 3.0)})
    assert b.calibration_stale is False


def test_threshold_is_configurable():
    # A drift of ~40px trips a small threshold but not a large one.
    drift = {k: (v[0] + 28.0, v[1] + 28.0) for k, v in _ARUCO.items()}  # ~39.6px

    b_small = _boundary(movement_threshold_px=10.0)
    _mock_corners(b_small, drift)
    b_small.update(_FRAME, apriltags={})
    assert b_small.calibration_stale is True

    b_large = _boundary(movement_threshold_px=100.0)
    _mock_corners(b_large, drift)
    b_large.update(_FRAME, apriltags={})
    assert b_large.calibration_stale is False


def test_default_threshold_used_when_unset():
    b = _boundary()  # movement_threshold_px left at 0 → default
    assert b._effective_movement_threshold() == DEFAULT_MOVEMENT_THRESHOLD_PX


# ---------------------------------------------------------------------------
# AprilTag 1 sentinel works with corners occluded
# ---------------------------------------------------------------------------


def test_apriltag1_sentinel_trips_with_corners_occluded(caplog):
    b = _boundary(movement_threshold_px=25.0)
    # No ArUco corners detected at all (all occluded), but AprilTag 1 is seen
    # displaced beyond threshold.
    _mock_corners(b, {})
    moved_a1 = (_APRIL1["apriltag:1"][0] + 60.0, _APRIL1["apriltag:1"][1])
    with caplog.at_level(logging.WARNING, logger="aprilcam.core.playfield"):
        b.update(_FRAME, apriltags={1: moved_a1})
    assert b.calibration_stale is True
    assert any("apriltag:1" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Configurable static set
# ---------------------------------------------------------------------------


def test_default_static_set_is_aruco_corners_plus_apriltag1():
    from aprilcam.calibration.calibration import DEFAULT_STATIC_MARKER_IDS
    assert DEFAULT_STATIC_MARKER_IDS == ["aruco_corners", "apriltag:1"]
    b = _boundary()  # static_marker_ids unset → default
    assert b._static_apriltag_ids() == {1}


def test_static_set_is_configurable():
    # Declare AprilTag 5 (not 1) as the sentinel; 1 then becomes dynamic.
    b = PlayfieldBoundary(
        static_markers={"corner:UL": (100.0, 100.0)},
        static_marker_ids=["aruco_corners", "apriltag:5"],
        movement_threshold_px=25.0,
    )
    assert b._static_apriltag_ids() == {5}
    _mock_corners(b, {})
    # Tag 1 is now dynamic: moving it wildly does NOT trip invalidation.
    b.update(_FRAME, apriltags={1: (1.0, 1.0)})
    assert b.calibration_stale is False


# ---------------------------------------------------------------------------
# Live-corner mode is unaffected (no static markers)
# ---------------------------------------------------------------------------


def test_live_corner_mode_not_static():
    b = PlayfieldBoundary()
    assert b.is_static_mode is False
    assert b.calibration_stale is False
    # update() with apriltags is accepted but ignored for static handling.
    _mock_corners(b, _ARUCO)
    b.update(_FRAME, apriltags={1: (300.0, 250.0)})
    assert b.get_polygon() is not None
    assert b.calibration_stale is False


# ---------------------------------------------------------------------------
# Daemon surfacing: stale flag is written into info.json
# ---------------------------------------------------------------------------


def test_info_json_carries_stale_flag(tmp_path: Path):
    from aprilcam.config import Config
    from aprilcam.daemon.camera_pipeline import CameraPipeline

    cfg = Config(data_dir=tmp_path, socket_dir=tmp_path / "sock")
    pipe = CameraPipeline("cam0", 0, cfg)

    # Fake an AprilCam whose playfield boundary reports stale calibration.
    class _FakeBoundary:
        calibration_stale = True

    class _FakeCam:
        playfield = _FakeBoundary()

    pipe._april_cam = _FakeCam()
    pipe._write_info_json(640, 480, None, "cam0")

    info_path = cfg.cameras_dir / "cam0" / "info.json"
    data = json.loads(info_path.read_text())
    assert data["calibration_stale"] is True

    # Flip the flag and confirm _maybe_update_stale_flag rewrites the file.
    pipe._info_frame_size = (640, 480)
    pipe._info_homography = None
    pipe._info_device_name = "cam0"
    pipe._last_stale_written = True
    _FakeBoundary.calibration_stale = False
    pipe._maybe_update_stale_flag()
    data = json.loads(info_path.read_text())
    assert data["calibration_stale"] is False
