"""Regression tests for overlay-pixel correction in the world-correction block.

Bug: the live-view overlay drew each tag's marker at its raw detected pixel
(center_px/corners_px) while reporting a parallax- and mount-offset-corrected
world_xy.  A tag physically at world x=50 correctly reported world_xy
x~50.8, but its drawn box sat ~37px east of where x=50 actually is on
screen — visually inconsistent with the ruler and every other on-screen
marker at that world position.

Fix: camera_pipeline.py's world-correction block (in _capture_loop) now
also projects the corrected world position back to a pixel via the inverse
homography and shifts center_px/corners_px by the resulting delta, so the
overlay is drawn at the corrected ground position.

These tests mirror the block's logic exactly (following the pattern
established by test_parallax_integration.py's _run_pipeline_postprocess),
kept in sync with camera_pipeline.py's world-correction block (see the
comment at its top, currently ~line 1023).
"""

from __future__ import annotations

import dataclasses as _dc
from typing import Optional

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.calibration.calibration import CameraCalibration, CameraPosition
from aprilcam.core.detection import TagRecord
from aprilcam.daemon.camera_pipeline import CameraPipeline


def _minimal_cal(**kwargs) -> CameraCalibration:
    homography = kwargs.pop("homography", np.eye(3, dtype=float))
    return CameraCalibration(
        device_name="TestCam",
        resolution=(1920, 1080),
        homography=homography,
        **kwargs,
    )


def _make_tag_record(
    tag_id: int,
    center_px: tuple,
    corners_px: list,
    world_xy: Optional[tuple],
    orientation_yaw: float = 0.0,
) -> TagRecord:
    return TagRecord(
        id=tag_id,
        center_px=center_px,
        corners_px=corners_px,
        orientation_yaw=orientation_yaw,
        world_xy=world_xy,
        in_playfield=True,
        vel_px=None,
        speed_px=None,
        vel_world=None,
        speed_world=None,
        heading_rad=None,
        timestamp=0.0,
        frame_index=0,
    )


def _run_world_correction_block(pipeline, tag_records):
    """Faithful copy of camera_pipeline.py's _capture_loop world-correction
    block (the ``if self._calibration and (...)`` block), so the pixel
    -projection fix can be exercised without a live camera thread.  Keep in
    sync with the real block if it changes.
    """
    if not (
        pipeline._calibration
        and (
            pipeline._calibration.playfield_width_cm > 0
            or pipeline._calibration.camera_position is not None
        )
    ):
        return tag_records

    origin_x, origin_y = pipeline._a1_origin()
    homography = pipeline._calibration.homography
    Hinv = None
    if homography is not None:
        try:
            Hinv = np.linalg.inv(homography)
        except Exception:
            Hinv = None

    corrected = []
    for tr in tag_records:
        if tr.world_xy is None:
            corrected.append(tr)
            continue
        try:
            wx = tr.world_xy[0] - origin_x
            wy = tr.world_xy[1] - origin_y
            tag_h = pipeline._tag_heights.get(tr.id, 0.0)
            if pipeline._calibration.camera_position and tag_h > 0.0:
                wx, wy = pipeline._calibration.correct_world_for_height(wx, wy, tag_h)

            off = pipeline._tag_offsets.get(tr.id)
            new_yaw = None
            if off is not None and (off[0] or off[1] or off[2]):
                wx, wy, new_yaw = pipeline._robot_centre_from_tag(
                    wx, wy, tr.orientation_yaw, off
                )

            center_px = tr.center_px
            corners_px = tr.corners_px
            if Hinv is not None:
                vec = Hinv @ np.array([wx + origin_x, wy + origin_y, 1.0])
                if vec[2] != 0.0:
                    u = vec[0] / vec[2]
                    v = vec[1] / vec[2]
                    cx_raw, cy_raw = tr.center_px
                    du = u - cx_raw
                    dv = v - cy_raw
                    if abs(du) > 1e-6 or abs(dv) > 1e-6:
                        center_px = (u, v)
                        corners_px = [[c[0] + du, c[1] + dv] for c in tr.corners_px]

            if new_yaw is not None:
                tr = _dc.replace(
                    tr,
                    world_xy=(wx, wy),
                    orientation_yaw=new_yaw,
                    center_px=center_px,
                    corners_px=corners_px,
                )
            else:
                tr = _dc.replace(
                    tr, world_xy=(wx, wy), center_px=center_px, corners_px=corners_px
                )
        except Exception:
            pass
        corrected.append(tr)
    return corrected


def _pipeline(cal):
    p = CameraPipeline.__new__(CameraPipeline)
    p._calibration = cal
    p._tag_heights = {}
    p._tag_offsets = {}
    return p


# ---------------------------------------------------------------------------
# Core fix: corrected world_xy projects back to a shifted center_px/corners_px
# ---------------------------------------------------------------------------


def test_corrected_pixel_moves_toward_camera_offset_for_elevated_tag():
    """An elevated tag whose parallax correction pulls world_xy west must
    also have its drawn center_px/corners_px shifted west, so the overlay
    agrees with the corrected world position (the ticketed bug)."""
    scale = 0.1088  # cm/px
    H = np.array(
        [
            [scale, 0.0, 100.0],
            [0.0, -scale, 200.0],
            [0.0, 0.0, 1.0],
        ]
    )
    cal = _minimal_cal(
        homography=H,
        playfield_width_cm=126.32,
        playfield_height_cm=78.0,
        camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=127.0),
    )
    pipeline = _pipeline(cal)
    pipeline._tag_heights = {100: 4.0}

    raw_center = (1039.0, 375.0)
    raw_corners = [
        [raw_center[0] - 20, raw_center[1] - 20],
        [raw_center[0] + 20, raw_center[1] - 20],
        [raw_center[0] + 20, raw_center[1] + 20],
        [raw_center[0] - 20, raw_center[1] + 20],
    ]
    # world_xy as originally computed by AprilCam.process_frame (raw, not
    # yet A1-centred) — pick a value whose A1-centred, corrected result is
    # west of the raw pixel, matching the ticket's tag-100 scenario.
    raw_world = cal.pixel_to_world(*raw_center)
    tr = _make_tag_record(100, raw_center, raw_corners, raw_world)

    result = _run_world_correction_block(pipeline, [tr])[0]

    # world_xy shifted toward the camera's ground projection (x_offset=0),
    # i.e. corrected wx < raw-derived wx (raw world x is positive here).
    origin_x, origin_y = pipeline._a1_origin()
    raw_wx = raw_world[0] - origin_x
    assert result.world_xy[0] < raw_wx

    # center_px must have moved off the raw pixel, in the same (west,
    # i.e. decreasing u) direction as the world correction. (The parallax
    # correction pulls both wx and wy toward the camera's x/y offset, so a
    # dv shift is also expected here — only du's sign is asserted.)
    assert result.center_px != raw_center
    assert result.center_px[0] < raw_center[0]

    # corners_px shifted by the same rigid delta as center_px (shape/size
    # preserved — a pure translation).
    du = result.center_px[0] - raw_center[0]
    dv = result.center_px[1] - raw_center[1]
    for raw_c, corrected_c in zip(raw_corners, result.corners_px):
        assert abs(corrected_c[0] - (raw_c[0] + du)) < 1e-6
        assert abs(corrected_c[1] - (raw_c[1] + dv)) < 1e-6


def test_corrected_pixel_matches_ticket_tag100_scenario():
    """Reproduces the ticket's verified case in shape: a tag whose parallax
    correction pulls it toward the camera's ground offset must be redrawn
    at a pixel column consistent with the corrected world x — i.e. inverting
    the corrected pixel through H must reproduce the corrected world (not
    the raw world), closing the round trip the fix establishes."""
    scale = 0.1088
    H = np.array(
        [
            [scale, 0.0, 100.0],
            [0.0, -scale, 200.0],
            [0.0, 0.0, 1.0],
        ]
    )
    cal = _minimal_cal(
        homography=H,
        playfield_width_cm=126.32,
        playfield_height_cm=78.0,
        camera_position=CameraPosition(x_offset=30.0, y_offset=0.0, height=127.0),
    )
    pipeline = _pipeline(cal)
    pipeline._tag_heights = {100: 12.0}

    raw_center = (1039.0, 375.0)
    raw_corners = [[raw_center[0], raw_center[1]]] * 4
    raw_world = cal.pixel_to_world(*raw_center)
    tr = _make_tag_record(100, raw_center, raw_corners, raw_world)

    result = _run_world_correction_block(pipeline, [tr])[0]

    # Project the corrected pixel back through H (forward) — must land on
    # the corrected, A1-centred world_xy exactly (within float tolerance).
    reprojected = cal.pixel_to_world(*result.center_px)
    origin_x, origin_y = pipeline._a1_origin()
    reprojected_a1 = (reprojected[0] - origin_x, reprojected[1] - origin_y)
    assert abs(reprojected_a1[0] - result.world_xy[0]) < 1e-6
    assert abs(reprojected_a1[1] - result.world_xy[1]) < 1e-6

    # And the corrected pixel differs from the raw pixel (the whole point
    # of the fix) since a real parallax correction was applied.
    assert result.center_px != raw_center


# ---------------------------------------------------------------------------
# Guard conditions: no needless / incorrect pixel shifting
# ---------------------------------------------------------------------------


def test_no_pixel_shift_when_no_correction_applied():
    """When neither parallax nor mount offset applies (tag not in
    _tag_heights, no offset registered), center_px/corners_px are left
    unchanged (delta is ~0, guarded by the 1e-6 threshold)."""
    H = np.array([[0.1088, 0.0, 100.0], [0.0, -0.1088, 200.0], [0.0, 0.0, 1.0]])
    cal = _minimal_cal(
        homography=H,
        playfield_width_cm=126.32,
        playfield_height_cm=78.0,
        camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=127.0),
    )
    pipeline = _pipeline(cal)
    # tag 100 has no entry in _tag_heights -> no parallax correction applied.

    raw_center = (500.0, 400.0)
    raw_corners = [[490.0, 390.0], [510.0, 390.0], [510.0, 410.0], [490.0, 410.0]]
    raw_world = cal.pixel_to_world(*raw_center)
    tr = _make_tag_record(100, raw_center, raw_corners, raw_world)

    result = _run_world_correction_block(pipeline, [tr])[0]

    assert result.center_px == raw_center
    assert result.corners_px == raw_corners


def test_no_pixel_shift_when_homography_is_none():
    """If the calibration has no homography this frame, pixel projection is
    skipped entirely — center_px/corners_px pass through unchanged even
    though world_xy is still corrected."""
    cal = _minimal_cal(
        playfield_width_cm=126.32,
        playfield_height_cm=78.0,
        camera_position=CameraPosition(x_offset=0.0, y_offset=0.0, height=127.0),
    )
    cal.homography = None  # simulate "no homography this frame"
    pipeline = _pipeline(cal)
    pipeline._tag_heights = {100: 12.0}

    raw_center = (500.0, 400.0)
    raw_corners = [[490.0, 390.0], [510.0, 390.0], [510.0, 410.0], [490.0, 410.0]]
    tr = _make_tag_record(100, raw_center, raw_corners, (60.0, 30.0))

    result = _run_world_correction_block(pipeline, [tr])[0]

    # world_xy still gets origin-shifted + parallax-corrected...
    assert result.world_xy != (60.0, 30.0)
    # ...but pixels are untouched since there's no homography to invert.
    assert result.center_px == raw_center
    assert result.corners_px == raw_corners


def test_pixel_correction_survives_mount_offset_path():
    """Mobile-tag mount-offset correction (register_mobile_tag) also feeds
    into the corrected world used for reprojection — center_px still ends
    up consistent with the final (offset-corrected) world_xy."""
    H = np.array([[0.1088, 0.0, 100.0], [0.0, -0.1088, 200.0], [0.0, 0.0, 1.0]])
    cal = _minimal_cal(homography=H, playfield_width_cm=126.32, playfield_height_cm=78.0)
    pipeline = _pipeline(cal)
    pipeline._tag_offsets = {100: (50.0, 0.0, 0.0)}  # 5cm forward mount offset

    raw_center = (1000.0, 400.0)
    raw_corners = [[raw_center[0], raw_center[1]]] * 4
    raw_world = cal.pixel_to_world(*raw_center)
    tr = _make_tag_record(100, raw_center, raw_corners, raw_world, orientation_yaw=0.0)

    result = _run_world_correction_block(pipeline, [tr])[0]

    reprojected = cal.pixel_to_world(*result.center_px)
    origin_x, origin_y = pipeline._a1_origin()
    reprojected_a1 = (reprojected[0] - origin_x, reprojected[1] - origin_y)
    assert abs(reprojected_a1[0] - result.world_xy[0]) < 1e-6
    assert abs(reprojected_a1[1] - result.world_xy[1]) < 1e-6
    assert result.center_px != raw_center
