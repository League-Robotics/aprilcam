"""Tests for ``aprilcam.calibration.flatfield`` (sprint 037, ticket 001).

Fully headless and pure: no daemon, no camera hardware, no filesystem.
``FlatFieldCapturer`` is exercised directly against synthetic BGR/mono
frame bursts (linear gradients standing in for uneven lighting) and
precomputed boolean masks -- exactly the two inputs the concrete
``build`` accepts per ``calibration.interfaces.FlatFieldCapturer``.
"""

from __future__ import annotations

from unittest.mock import patch

import cv2 as cv
import numpy as np
import pytest

from aprilcam.calibration.flatfield import (
    CHECK_CLIPPING,
    CHECK_DISAGREEMENT,
    CHECK_SPREAD,
    FlatFieldCapturer,
)
from aprilcam.calibration.interfaces import FlatFieldResult

# ---------------------------------------------------------------------------
# Synthetic frame helpers
# ---------------------------------------------------------------------------

_SHAPE = (180, 240)  # (H, W) -- small enough to run fast, large enough that
# GAUSSIAN_BLUR_SIGMA=40 stays well inside the frame.


def _gradient_frame(
    *,
    shape=_SHAPE,
    low: float = 100.0,
    high: float = 200.0,
    channels: int = 3,
    flipped: bool = False,
) -> np.ndarray:
    """A frame whose luminance ramps linearly left-to-right from *low*
    to *high* (or *high* to *low* if *flipped*) -- a stand-in for an
    uneven light field. ``channels=1`` returns a 2-D mono frame."""
    h, w = shape
    if flipped:
        low, high = high, low
    row = np.linspace(low, high, w, dtype=np.float32)
    field = np.tile(row, (h, 1))
    if channels == 1:
        return np.clip(field, 0, 255).astype(np.uint8)
    frame = np.repeat(field[:, :, np.newaxis], channels, axis=2)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _full_mask(shape=_SHAPE) -> np.ndarray:
    return np.ones(shape, dtype=bool)


def _luma(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame.astype(np.float32)
    b, g, r = frame[:, :, 0], frame[:, :, 1], frame[:, :, 2]
    return 0.299 * r.astype(np.float32) + 0.587 * g.astype(np.float32) + 0.114 * b.astype(np.float32)


# ---------------------------------------------------------------------------
# 1. Known synthetic gradient flattens to within a stated tolerance
# ---------------------------------------------------------------------------


def test_gradient_flattens_within_tolerance():
    frames = [_gradient_frame(low=80.0, high=220.0) for _ in range(4)]
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask)

    assert result.succeeded is True
    assert result.gain is not None
    assert result.gain.shape[:2] == _SHAPE

    # Independently verify: applying the returned gain to the batch
    # average should flatten the raw 220/80 = 2.75x gradient down to
    # near 1x -- residual is the Gaussian blur's own reflect-padded
    # edge handling, which never fully recovers the true field right at
    # the frame border, so the tolerance allows a modest residual
    # rather than demanding an exact 1.0x.
    avg = np.mean([f.astype(np.float32) for f in frames], axis=0)
    corrected = avg * result.gain
    luma = _luma(corrected)
    p2, p98 = np.percentile(luma, 2), np.percentile(luma, 98)
    spread = p98 / p2
    assert spread < 1.35, f"corrected spread {spread:.3f}x exceeds tolerance"

    # The result's own self-reported spread_after tells the same story.
    assert all(s < 1.35 for s in result.spread_after)
    assert all(s > 1.3 for s in result.spread_before)  # the uncorrected gradient was real


def test_gradient_flattens_for_mono_frames():
    frames = [_gradient_frame(low=90.0, high=210.0, channels=1) for _ in range(3)]
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask)

    assert result.succeeded is True
    assert result.gain is not None
    assert result.gain.ndim == 2  # mono input -> mono gain, not (H, W, 1)
    assert len(result.spread_before) == 1
    assert len(result.spread_after) == 1
    assert result.spread_after[0] < 1.35


# ---------------------------------------------------------------------------
# 2. Wrinkle / speckle noise does not leak into the built gain map
# ---------------------------------------------------------------------------


def test_wrinkle_speckle_noise_does_not_leak_into_gain_map():
    base_frames = [_gradient_frame(low=100.0, high=200.0) for _ in range(4)]
    mask = _full_mask()
    clean_result = FlatFieldCapturer().build(base_frames, mask)
    assert clean_result.succeeded is True

    rng = np.random.default_rng(1234)
    noisy_frames = []
    for frame in base_frames:
        noisy = frame.copy()
        for _ in range(20):
            y = int(rng.integers(10, _SHAPE[0] - 10))
            x = int(rng.integers(10, _SHAPE[1] - 10))
            size = int(rng.integers(2, 5))
            delta = int(rng.choice([-70, 70]))
            patch = noisy[y : y + size, x : x + size].astype(np.int32) + delta
            noisy[y : y + size, x : x + size] = np.clip(patch, 0, 250).astype(np.uint8)
        noisy_frames.append(noisy)

    noisy_result = FlatFieldCapturer().build(noisy_frames, mask)

    assert noisy_result.succeeded is True
    assert noisy_result.gain is not None
    # The median blur (kernel 31) should suppress a handful of 2-4 px
    # speckles almost entirely -- the gain map built from the noisy
    # burst should stay close to the one built from the clean burst.
    assert np.allclose(noisy_result.gain, clean_result.gain, atol=0.08)
    # And the noisy result's own reported spread should not have
    # meaningfully worsened either.
    for before, after in zip(clean_result.spread_after, noisy_result.spread_after):
        assert after < before + 0.1


# ---------------------------------------------------------------------------
# 3. In-field clipping refusal, and force override
# ---------------------------------------------------------------------------


def test_in_field_clipping_is_refused_without_force():
    frames = [_gradient_frame(low=150.0, high=170.0) for _ in range(3)]
    # Saturate a small patch of the first frame, inside the mask.
    frames[0][80:95, 100:115] = 255
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask)

    assert result.succeeded is False
    assert result.gain is None
    assert result.reason is not None
    assert CHECK_CLIPPING in result.reason
    assert result.forced_checks == ()
    # Spread is still reported even on refusal.
    assert isinstance(result.spread_before, tuple) and result.spread_before
    assert isinstance(result.spread_after, tuple) and result.spread_after


def test_in_field_clipping_proceeds_with_force_and_names_the_check():
    frames = [_gradient_frame(low=150.0, high=170.0) for _ in range(3)]
    frames[0][80:95, 100:115] = 255
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask, force=True)

    assert result.succeeded is True
    assert result.gain is not None
    assert CHECK_CLIPPING in result.forced_checks


# ---------------------------------------------------------------------------
# 4. Implausible (>10x) spread refusal, and force override
# ---------------------------------------------------------------------------


def test_implausible_spread_is_refused_without_force():
    frames = [_gradient_frame(low=5.0, high=250.0) for _ in range(4)]
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask)

    assert result.succeeded is False
    assert result.gain is None
    assert CHECK_SPREAD in result.reason
    assert result.forced_checks == ()


def test_implausible_spread_proceeds_with_force_and_names_the_check():
    frames = [_gradient_frame(low=5.0, high=250.0) for _ in range(4)]
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask, force=True)

    assert result.succeeded is True
    assert result.gain is not None
    assert CHECK_SPREAD in result.forced_checks


# ---------------------------------------------------------------------------
# 5. Frame-to-frame disagreement ("sheet moved") refusal, and force override
# ---------------------------------------------------------------------------


def test_frame_disagreement_sheet_moved_is_refused_without_force():
    steady = [_gradient_frame(low=120.0, high=180.0) for _ in range(3)]
    moved = _gradient_frame(low=120.0, high=180.0, flipped=True)
    frames = steady + [moved]
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask)

    assert result.succeeded is False
    assert result.gain is None
    assert CHECK_DISAGREEMENT in result.reason


def test_frame_disagreement_proceeds_with_force_and_names_the_check():
    steady = [_gradient_frame(low=120.0, high=180.0) for _ in range(3)]
    moved = _gradient_frame(low=120.0, high=180.0, flipped=True)
    frames = steady + [moved]
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask, force=True)

    assert result.succeeded is True
    assert result.gain is not None
    assert CHECK_DISAGREEMENT in result.forced_checks


def test_steady_burst_alone_does_not_trigger_disagreement():
    frames = [_gradient_frame(low=120.0, high=180.0) for _ in range(4)]
    mask = _full_mask()

    result = FlatFieldCapturer().build(frames, mask)

    assert result.succeeded is True
    assert result.forced_checks == ()


# ---------------------------------------------------------------------------
# 6. spread_before/spread_after are always reported, pass or refuse
# ---------------------------------------------------------------------------


def test_spread_always_reported_on_pass_and_refusal():
    passing = FlatFieldCapturer().build(
        [_gradient_frame(low=140.0, high=160.0) for _ in range(3)], _full_mask()
    )
    assert passing.succeeded is True
    assert len(passing.spread_before) == 3
    assert len(passing.spread_after) == 3
    assert all(isinstance(v, float) for v in passing.spread_before)
    assert all(isinstance(v, float) for v in passing.spread_after)

    refusing = FlatFieldCapturer().build(
        [_gradient_frame(low=5.0, high=250.0) for _ in range(3)], _full_mask()
    )
    assert refusing.succeeded is False
    assert len(refusing.spread_before) == 3
    assert len(refusing.spread_after) == 3


# ---------------------------------------------------------------------------
# 7. Never touches a filesystem, a camera, or daemon state
# ---------------------------------------------------------------------------


def test_build_never_opens_a_file_or_a_camera():
    frames = [_gradient_frame(low=140.0, high=160.0) for _ in range(3)]
    mask = _full_mask()

    with patch("builtins.open") as mock_open, patch.object(cv, "VideoCapture") as mock_capture:
        result = FlatFieldCapturer().build(frames, mask)

    assert result.succeeded is True
    mock_open.assert_not_called()
    mock_capture.assert_not_called()


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_build_requires_at_least_one_frame():
    with pytest.raises(ValueError):
        FlatFieldCapturer().build([], _full_mask())


def test_build_returns_flatfield_result_type():
    result = FlatFieldCapturer().build(
        [_gradient_frame(low=140.0, high=160.0) for _ in range(3)], _full_mask()
    )
    assert isinstance(result, FlatFieldResult)
