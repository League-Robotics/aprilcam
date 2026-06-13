"""Tests for ticket 007: pre-warp undistortion + static-camera mode wiring.

Sprint 011, ticket 007.  Covers, fully synthetically (no camera, no real
markers):

- Optional pre-warp undistortion in ``PlayfieldDisplay``'s deskew path:
  * applied (before the warp) when enabled AND intrinsics are present;
  * a no-op when disabled, or when intrinsics are absent — deskew still
    produces a warped view.
- Static-camera mode auto-on when a saved homography exists, and the config
  override (``APRILCAM_STATIC_DESKEW=0``) that disables it.
- ``px_per_cm`` and the movement-invalidation threshold read from config,
  with the ticket-005 defaults.
- End-to-end: a calibrated fixed camera deskews to the metric ``W×H``
  top-down view with no live ArUco corner detection.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

import cv2 as cv  # noqa: E402

from aprilcam.calibration.calibration import CameraCalibration  # noqa: E402
from aprilcam.calibration.geometry import (  # noqa: E402
    DEFAULT_MOVEMENT_THRESHOLD_PX,
    DEFAULT_PX_PER_CM,
)
from aprilcam.config import Config  # noqa: E402
from aprilcam.core.playfield import PlayfieldBoundary  # noqa: E402
from aprilcam.ui.display import PlayfieldDisplay  # noqa: E402

pytestmark = pytest.mark.needs_cv2


# A non-axis-aligned (perspective-skewed) source polygon, UL/UR/LR/LL.
_POLY = np.array(
    [[120.0, 80.0], [840.0, 110.0], [900.0, 620.0], [60.0, 560.0]],
    dtype=np.float32,
)
_W, _H = 109.0, 79.5


def _homography(poly: np.ndarray = _POLY, w: float = _W, h: float = _H) -> np.ndarray:
    src = np.asarray(poly, dtype=np.float32).reshape(4, 2)
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    return cv.getPerspectiveTransform(src, dst).astype(np.float64)


def _calibration(with_intrinsics: bool) -> CameraCalibration:
    H = _homography()
    cm: Optional[np.ndarray] = None
    dc: Optional[np.ndarray] = None
    if with_intrinsics:
        # A plausible pinhole matrix for a 1280x720 frame plus barrel
        # distortion strong enough to be visible after undistort().
        cm = np.array(
            [[900.0, 0.0, 640.0], [0.0, 900.0, 360.0], [0.0, 0.0, 1.0]],
            dtype=float,
        )
        dc = np.array([-0.35, 0.12, 0.0, 0.0, 0.0], dtype=float)
    return CameraCalibration(
        device_name="test-cam",
        resolution=(1280, 720),
        homography=H,
        camera_matrix=cm,
        dist_coeffs=dc,
        playfield_width_cm=_W,
        playfield_height_cm=_H,
    )


def _textured_frame() -> np.ndarray:
    """A frame with high-frequency content so undistort changes pixels."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[::8, :, :] = 255  # horizontal stripes
    frame[:, ::8, :] = 200  # vertical stripes
    return frame


def _make_display(undistort: bool, with_intrinsics: bool) -> PlayfieldDisplay:
    H = _homography()
    boundary = PlayfieldBoundary(homography=H, width_cm=_W, height_cm=_H)
    disp = PlayfieldDisplay(
        boundary,
        headless=True,
        deskew_overlay=True,
        calibration=_calibration(with_intrinsics),
        undistort=undistort,
    )
    frame = _textured_frame()
    disp._update_deskew(frame)
    return disp


# ---------------------------------------------------------------------------
# Pre-warp undistortion
# ---------------------------------------------------------------------------


def test_undistort_applied_when_enabled_and_intrinsics_present(monkeypatch):
    disp = _make_display(undistort=True, with_intrinsics=True)
    frame = _textured_frame()

    # Spy on CameraCalibration.undistort to confirm it is called BEFORE the warp.
    calls = {"undistort": 0}
    real_undistort = disp.calibration.undistort

    def _spy(f):
        calls["undistort"] += 1
        return real_undistort(f)

    monkeypatch.setattr(disp.calibration, "undistort", _spy)

    out = disp.prepare_display(frame)
    assert calls["undistort"] == 1
    # Deskewed to the metric size.
    s = DEFAULT_PX_PER_CM
    assert (out.shape[1], out.shape[0]) == (round(_W * s), round(_H * s))


def test_undistort_changes_output_vs_no_undistort():
    frame = _textured_frame()
    disp_on = _make_display(undistort=True, with_intrinsics=True)
    disp_off = _make_display(undistort=False, with_intrinsics=True)
    out_on = disp_on.prepare_display(frame.copy())
    out_off = disp_off.prepare_display(frame.copy())
    assert out_on.shape == out_off.shape
    # Undistortion must visibly change the warped result.
    assert not np.array_equal(out_on, out_off)


def test_undistort_noop_when_disabled():
    frame = _textured_frame()
    disp = _make_display(undistort=False, with_intrinsics=True)
    # With undistort disabled, the warp is applied to the raw frame.
    M, (w, h) = disp.M_deskew, disp.deskew_size
    expected = cv.warpPerspective(frame, M, (w, h))
    out = disp.prepare_display(frame.copy())
    np.testing.assert_array_equal(out, expected)


def test_undistort_noop_when_no_intrinsics():
    frame = _textured_frame()
    disp = _make_display(undistort=True, with_intrinsics=False)
    # Intrinsics absent → undistort() is a pass-through; deskew still works.
    M, (w, h) = disp.M_deskew, disp.deskew_size
    expected = cv.warpPerspective(frame, M, (w, h))
    out = disp.prepare_display(frame.copy())
    assert (out.shape[1], out.shape[0]) == (round(_W * DEFAULT_PX_PER_CM),
                                            round(_H * DEFAULT_PX_PER_CM))
    np.testing.assert_array_equal(out, expected)


def test_undistort_noop_when_no_calibration():
    H = _homography()
    boundary = PlayfieldBoundary(homography=H, width_cm=_W, height_cm=_H)
    disp = PlayfieldDisplay(
        boundary, headless=True, deskew_overlay=True,
        calibration=None, undistort=True,
    )
    frame = _textured_frame()
    disp._update_deskew(frame)
    out = disp.prepare_display(frame)
    assert (out.shape[1], out.shape[0]) == (round(_W * DEFAULT_PX_PER_CM),
                                            round(_H * DEFAULT_PX_PER_CM))


# ---------------------------------------------------------------------------
# Static-camera mode auto-on + override
# ---------------------------------------------------------------------------


_STORED = {
    "corner:UL": (100.0, 100.0),
    "corner:UR": (500.0, 100.0),
    "corner:LR": (500.0, 400.0),
    "corner:LL": (100.0, 400.0),
}


def test_static_mode_auto_on_with_static_markers():
    b = PlayfieldBoundary(static_markers=dict(_STORED))
    # AUTO (static_mode None): static markers present → mode on.
    assert b.static_mode is None
    assert b.is_static_mode is True


def test_static_mode_override_disables():
    b = PlayfieldBoundary(static_markers=dict(_STORED), static_mode=False)
    assert b.is_static_mode is False


def test_static_mode_override_force_on():
    b = PlayfieldBoundary(static_markers=dict(_STORED), static_mode=True)
    assert b.is_static_mode is True


def test_static_mode_no_markers_is_off_regardless():
    # Force-on with no stored markers is still off (nothing to hold).
    assert PlayfieldBoundary(static_mode=True).is_static_mode is False
    assert PlayfieldBoundary().is_static_mode is False


# ---------------------------------------------------------------------------
# Config knobs: static_deskew, px_per_cm, undistort, movement_threshold
# ---------------------------------------------------------------------------


def _clean_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("APRILCAM_"):
            monkeypatch.delenv(key, raising=False)


def test_config_defaults(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    cfg = Config.load(start=tmp_path)
    assert cfg.static_deskew is True
    assert cfg.deskew_px_per_cm == 0.0
    assert cfg.undistort is False
    assert cfg.movement_threshold_px == 0.0


def test_config_static_deskew_disable(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("APRILCAM_STATIC_DESKEW", "0")
    cfg = Config.load(start=tmp_path)
    assert cfg.static_deskew is False


def test_config_px_per_cm_and_threshold_and_undistort(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("APRILCAM_DESKEW_PX_PER_CM", "5.5")
    monkeypatch.setenv("APRILCAM_MOVEMENT_THRESHOLD_PX", "40")
    monkeypatch.setenv("APRILCAM_UNDISTORT", "true")
    cfg = Config.load(start=tmp_path)
    assert cfg.deskew_px_per_cm == 5.5
    assert cfg.movement_threshold_px == 40.0
    assert cfg.undistort is True


def test_config_env_overrides_dotfile(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    (tmp_path / ".aprilcam").write_text("APRILCAM_DESKEW_PX_PER_CM=2.0\n")
    monkeypatch.setenv("APRILCAM_DESKEW_PX_PER_CM", "9.0")
    cfg = Config.load(start=tmp_path)
    assert cfg.deskew_px_per_cm == 9.0


def test_config_invalid_floats_fall_back_to_default(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("APRILCAM_DESKEW_PX_PER_CM", "not-a-number")
    cfg = Config.load(start=tmp_path)
    assert cfg.deskew_px_per_cm == 0.0


def test_px_per_cm_default_resolves_to_geometry_default():
    # px_per_cm=0 → DEFAULT_PX_PER_CM via the boundary's effective getter.
    b = PlayfieldBoundary(homography=_homography(), width_cm=_W, height_cm=_H)
    assert b.px_per_cm == 0.0
    assert b._effective_px_per_cm() == DEFAULT_PX_PER_CM


def test_px_per_cm_override_changes_output_resolution():
    b = PlayfieldBoundary(
        homography=_homography(), width_cm=_W, height_cm=_H, px_per_cm=5.0
    )
    _, (out_w, out_h) = b.deskew_transform()
    assert (out_w, out_h) == (round(_W * 5.0), round(_H * 5.0))


def test_movement_threshold_default_resolves_to_geometry_default():
    b = PlayfieldBoundary(static_markers=dict(_STORED))
    assert b.movement_threshold_px == 0.0
    assert b._effective_movement_threshold() == DEFAULT_MOVEMENT_THRESHOLD_PX


# ---------------------------------------------------------------------------
# Pipeline wiring: _seed_static_geometry honours config
# ---------------------------------------------------------------------------


class _FakeBoundary:
    def __init__(self):
        self.homography = None
        self.width_cm = 0.0
        self.height_cm = 0.0
        self.px_per_cm = 0.0
        self.movement_threshold_px = 0.0
        self.static_mode = None
        self.static_marker_ids = None
        self.static_markers = None
        self._held_static = {}
        self._poly = None


class _FakeDisplay:
    def __init__(self):
        self.calibration = None
        self.undistort_enabled = False


class _FakeCam:
    def __init__(self):
        self.playfield = _FakeBoundary()
        self.display = _FakeDisplay()


def _pipeline_with(cfg) -> "object":
    from aprilcam.daemon.camera_pipeline import CameraPipeline
    pipe = CameraPipeline("cam0", 0, cfg)
    pipe._april_cam = _FakeCam()
    pipe._calibration = _calibration(with_intrinsics=True)
    return pipe


def test_pipeline_seed_auto_on_seeds_geometry(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    cfg = Config(data_dir=tmp_path, socket_dir=tmp_path / "s",
                 static_deskew=True, deskew_px_per_cm=5.0,
                 movement_threshold_px=33.0, undistort=True)
    pipe = _pipeline_with(cfg)
    pipe._seed_static_geometry()
    b = pipe._april_cam.playfield
    # Static mode forced on; geometry seeded from H⁻¹ (no corner_pixels).
    assert b.static_mode is True
    assert b.homography is not None
    assert b._poly is not None
    assert b.px_per_cm == 5.0
    assert b.movement_threshold_px == 33.0
    # Undistortion wired into the display from config.
    assert pipe._april_cam.display.undistort_enabled is True
    assert pipe._april_cam.display.calibration is pipe._calibration


def test_pipeline_seed_disabled_skips_seeding(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    cfg = Config(data_dir=tmp_path, socket_dir=tmp_path / "s",
                 static_deskew=False)
    pipe = _pipeline_with(cfg)
    pipe._seed_static_geometry()
    b = pipe._april_cam.playfield
    # Override disables static mode and skips all geometry seeding.
    assert b.static_mode is False
    assert b.homography is None
    assert b._poly is None
    assert b.static_markers is None


# ---------------------------------------------------------------------------
# End-to-end: fixed camera deskews to metric W×H with no live corner
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[1]
_GLOBAL_SHUTTER = (
    _REPO_ROOT / "data" / "aprilcam" / "cameras"
    / "global-shutter-camera" / "calibration.json"
)


def test_end_to_end_fixed_camera_metric_deskew_no_live_corner(tmp_path, monkeypatch):
    """A calibrated fixed camera produces a metric W×H top-down deskew with no
    live ArUco corner detection (the global-shutter scenario)."""
    import json
    if not _GLOBAL_SHUTTER.exists():
        pytest.skip("global-shutter-camera/calibration.json not present")
    data = json.loads(_GLOBAL_SHUTTER.read_text())
    H = np.array(data["homography"], dtype=float)
    W = float(data["playfield"]["width"])
    Hh = float(data["playfield"]["height"])
    res = data.get("resolution", [1920, 1080])

    # Seed boundary purely from saved geometry — never call update()/detect.
    boundary = PlayfieldBoundary(homography=H, width_cm=W, height_cm=Hh)
    assert boundary.get_polygon() is not None  # seeded, no live corner

    disp = PlayfieldDisplay(
        boundary, headless=True, deskew_overlay=True,
        calibration=CameraCalibration(
            device_name="gs", resolution=(int(res[0]), int(res[1])),
            homography=H, playfield_width_cm=W, playfield_height_cm=Hh,
        ),
        undistort=True,  # enabled but intrinsics absent → no-op
    )
    frame = np.full((int(res[1]), int(res[0]), 3), 200, dtype=np.uint8)
    disp._update_deskew(frame)
    out = disp.prepare_display(frame)
    s = DEFAULT_PX_PER_CM
    assert (out.shape[1], out.shape[0]) == (round(W * s), round(Hh * s))
