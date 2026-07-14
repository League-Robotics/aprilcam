"""Tests for the dark-frame / zero-detection advisory signal.

Sprint 016, ticket 002.  Covers, fully synthetically (no camera, no live
detection):

- Sustained dark + zero-tag samples over the configured window set
  ``frame_dark = True`` and write ``info.json`` exactly once at the
  transition.
- Bright frames, or frames with nonzero tag detections, keep the flag
  clear (or clear it once it was set).
- ``info.json`` is only rewritten when the flag's value actually changes,
  not on every sampled frame/window (write-on-change, mirroring
  ``_maybe_update_stale_flag``).
- The luminance floor and window-size constants are isolated, named
  module-level constants.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.config import Config  # noqa: E402
from aprilcam.daemon import camera_pipeline as cp_mod  # noqa: E402
from aprilcam.daemon.camera_pipeline import CameraPipeline  # noqa: E402

pytestmark = pytest.mark.needs_cv2


def _pipeline(tmp_path: Path) -> CameraPipeline:
    cfg = Config(data_dir=tmp_path, socket_dir=tmp_path / "sock")
    pipe = CameraPipeline("cam0", 0, cfg)

    class _FakeBoundary:
        calibration_stale = False

    class _FakeCam:
        playfield = _FakeBoundary()

    pipe._april_cam = _FakeCam()
    pipe._info_frame_size = (640, 480)
    pipe._info_homography = None
    pipe._info_device_name = "cam0"
    pipe._last_stale_written = False
    return pipe


def _dark_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _bright_frame() -> np.ndarray:
    return np.full((480, 640, 3), 200, dtype=np.uint8)


def _feed(pipe: CameraPipeline, frame: np.ndarray, tag_count: int, n_frames: int) -> None:
    """Drive ``n_frames`` worth of the frame_id/sample cadence through the pipeline.

    Mirrors the capture loop's call site: ``_maybe_update_frame_dark`` is
    invoked once per frame with the current ``_frame_id``, then the frame
    id is advanced — matching ``_capture_loop``'s ``self._frame_id += 1``.
    """
    for _ in range(n_frames):
        pipe._maybe_update_frame_dark(frame, tag_count)
        pipe._frame_id += 1


def _info_path(pipe: CameraPipeline) -> Path:
    return pipe.config.cameras_dir / pipe.cam_name / "info.json"


# ---------------------------------------------------------------------------
# Constants are isolated / named
# ---------------------------------------------------------------------------


def test_dark_constants_are_isolated_module_level():
    assert hasattr(cp_mod, "_DARK_LUMINANCE_FLOOR")
    assert hasattr(cp_mod, "_DARK_WINDOW_SAMPLES")
    assert hasattr(cp_mod, "_DARK_SAMPLE_EVERY_N_FRAMES")
    assert cp_mod._DARK_LUMINANCE_FLOOR > 0
    assert cp_mod._DARK_WINDOW_SAMPLES > 0
    assert cp_mod._DARK_SAMPLE_EVERY_N_FRAMES >= 1


# ---------------------------------------------------------------------------
# Sustained dark + zero detections sets the flag
# ---------------------------------------------------------------------------


def test_sustained_dark_and_zero_tags_sets_frame_dark(tmp_path: Path):
    pipe = _pipeline(tmp_path)
    assert pipe._frame_dark is False
    assert not _info_path(pipe).exists()

    # Enough frames to cover _DARK_WINDOW_SAMPLES consecutive *samples*
    # (sampled every _DARK_SAMPLE_EVERY_N_FRAMES frames).
    n_frames = cp_mod._DARK_WINDOW_SAMPLES * cp_mod._DARK_SAMPLE_EVERY_N_FRAMES
    _feed(pipe, _dark_frame(), tag_count=0, n_frames=n_frames)

    assert pipe._frame_dark is True
    data = json.loads(_info_path(pipe).read_text())
    assert data["frame_dark"] is True


def test_info_json_written_exactly_once_at_dark_transition(tmp_path: Path, monkeypatch):
    pipe = _pipeline(tmp_path)
    write_calls = []
    orig_write = CameraPipeline._write_info_json

    def _spy(self, *args, **kwargs):
        write_calls.append(1)
        return orig_write(self, *args, **kwargs)

    monkeypatch.setattr(CameraPipeline, "_write_info_json", _spy)

    n_frames = cp_mod._DARK_WINDOW_SAMPLES * cp_mod._DARK_SAMPLE_EVERY_N_FRAMES
    # Feed well past the transition point; the write must happen exactly once.
    _feed(pipe, _dark_frame(), tag_count=0, n_frames=n_frames + 5 * cp_mod._DARK_SAMPLE_EVERY_N_FRAMES)

    assert pipe._frame_dark is True
    assert len(write_calls) == 1


# ---------------------------------------------------------------------------
# Bright frames / nonzero detections keep the flag clear
# ---------------------------------------------------------------------------


def test_bright_frames_keep_flag_clear(tmp_path: Path):
    pipe = _pipeline(tmp_path)
    n_frames = cp_mod._DARK_WINDOW_SAMPLES * cp_mod._DARK_SAMPLE_EVERY_N_FRAMES
    _feed(pipe, _bright_frame(), tag_count=0, n_frames=n_frames)

    assert pipe._frame_dark is False
    # No transition ever occurred, so info.json was never written by this path.
    assert not _info_path(pipe).exists()


def test_nonzero_detections_keep_flag_clear_even_when_dark(tmp_path: Path):
    pipe = _pipeline(tmp_path)
    n_frames = cp_mod._DARK_WINDOW_SAMPLES * cp_mod._DARK_SAMPLE_EVERY_N_FRAMES
    _feed(pipe, _dark_frame(), tag_count=1, n_frames=n_frames)

    assert pipe._frame_dark is False
    assert not _info_path(pipe).exists()


def test_flag_clears_after_recovery(tmp_path: Path):
    pipe = _pipeline(tmp_path)
    n_frames = cp_mod._DARK_WINDOW_SAMPLES * cp_mod._DARK_SAMPLE_EVERY_N_FRAMES
    _feed(pipe, _dark_frame(), tag_count=0, n_frames=n_frames)
    assert pipe._frame_dark is True

    # A single bright/tags-present sample clears the sustained condition.
    _feed(pipe, _bright_frame(), tag_count=0, n_frames=cp_mod._DARK_SAMPLE_EVERY_N_FRAMES)
    assert pipe._frame_dark is False
    data = json.loads(_info_path(pipe).read_text())
    assert data["frame_dark"] is False


# ---------------------------------------------------------------------------
# Write-on-change: no rewrite when the value hasn't changed
# ---------------------------------------------------------------------------


def test_no_rewrite_when_value_unchanged(tmp_path: Path, monkeypatch):
    pipe = _pipeline(tmp_path)
    n_frames = cp_mod._DARK_WINDOW_SAMPLES * cp_mod._DARK_SAMPLE_EVERY_N_FRAMES
    # Drive to the dark transition first (one write).
    _feed(pipe, _dark_frame(), tag_count=0, n_frames=n_frames)
    assert pipe._frame_dark is True

    write_calls = []
    orig_write = CameraPipeline._write_info_json

    def _spy(self, *args, **kwargs):
        write_calls.append(1)
        return orig_write(self, *args, **kwargs)

    monkeypatch.setattr(CameraPipeline, "_write_info_json", _spy)

    # Continue feeding dark frames — value stays True, no further writes.
    _feed(pipe, _dark_frame(), tag_count=0, n_frames=n_frames * 2)
    assert pipe._frame_dark is True
    assert len(write_calls) == 0


# ---------------------------------------------------------------------------
# Sampling cadence: not every frame does luminance work
# ---------------------------------------------------------------------------


def test_sampling_skips_non_cadence_frames(tmp_path: Path, monkeypatch):
    pipe = _pipeline(tmp_path)
    sample_calls = []
    orig_sample = CameraPipeline._sample_mean_luminance

    def _spy(frame):
        sample_calls.append(1)
        return orig_sample(frame)

    monkeypatch.setattr(CameraPipeline, "_sample_mean_luminance", staticmethod(_spy))

    _feed(pipe, _dark_frame(), tag_count=0, n_frames=cp_mod._DARK_SAMPLE_EVERY_N_FRAMES * 3)
    assert len(sample_calls) == 3


# ---------------------------------------------------------------------------
# frame_dark is purely advisory: no gating anywhere in the codebase
# ---------------------------------------------------------------------------


def test_frame_dark_is_not_read_for_gating_anywhere():
    """Grep the source tree: frame_dark must only be written/tested, never
    consulted to gate/refuse an operation (mirrors the calibration_stale
    advisory-only audit)."""
    import subprocess

    src_root = Path(__file__).resolve().parent.parent / "src"
    result = subprocess.run(
        ["grep", "-rn", "--include=*.py", "frame_dark", str(src_root)],
        capture_output=True,
        text=True,
    )
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    # frame_dark must only appear in camera_pipeline.py (the producer).
    offenders = [l for l in lines if "camera_pipeline.py" not in l]
    assert not offenders, f"frame_dark referenced outside camera_pipeline.py: {offenders}"
