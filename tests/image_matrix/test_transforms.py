"""Pure unit tests for :mod:`tests.image_matrix.transforms` (sprint 035
ticket 007). No dependency on ``tests/images/`` or any fixture -- every
test here builds its own small synthetic array, so this module runs
identically whether or not the fixture corpus is present (ticket's own
acceptance criterion: renaming/removing ``tests/images/`` must not
affect these tests at all).
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.image_matrix import transforms as T

__all__ = []


def _solid(value: int, shape=(16, 16, 3)) -> np.ndarray:
    return np.full(shape, value, dtype=np.uint8)


def _checkerboard(low: int = 32, high: int = 224, shape=(32, 32, 3), block: int = 4) -> np.ndarray:
    """A high-frequency two-tone pattern -- gives blur/noise something
    real to smooth out or perturb, unlike a flat solid array."""
    tile = np.indices((shape[0] // block, shape[1] // block)).sum(axis=0) % 2
    tile = np.kron(tile, np.ones((block, block), dtype=np.uint8))
    img = np.where(tile[..., None].astype(bool), high, low).astype(np.uint8)
    return np.repeat(img[:, :, :1], shape[2], axis=2) if img.shape[2] != shape[2] else img


# ---------------------------------------------------------------------------
# exposure_scale
# ---------------------------------------------------------------------------


def test_exposure_scale_brightens_and_darkens_mean() -> None:
    base = _solid(100)
    brighter = T.exposure_scale(base, 1.5)
    darker = T.exposure_scale(base, 0.5)
    assert brighter.mean() > base.mean()
    assert darker.mean() < base.mean()
    assert brighter.mean() == pytest.approx(150.0, abs=0.5)
    assert darker.mean() == pytest.approx(50.0, abs=0.5)


def test_exposure_scale_clips_and_preserves_dtype_and_shape() -> None:
    base = _solid(200)
    result = T.exposure_scale(base, 3.0)
    assert result.dtype == base.dtype
    assert result.shape == base.shape
    assert result.max() == 255  # clipped, never wraps past the dtype range


# ---------------------------------------------------------------------------
# gamma
# ---------------------------------------------------------------------------


def test_gamma_below_one_brightens_above_one_darkens() -> None:
    base = _solid(128)
    brightened = T.gamma(base, 0.5)
    darkened = T.gamma(base, 2.0)
    assert brightened.mean() > base.mean()
    assert darkened.mean() < base.mean()


def test_gamma_identity_is_noop() -> None:
    base = _checkerboard()
    result = T.gamma(base, 1.0)
    assert np.array_equal(result, base)


def test_gamma_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        T.gamma(_solid(128), 0.0)
    with pytest.raises(ValueError):
        T.gamma(_solid(128), -1.0)


# ---------------------------------------------------------------------------
# contrast
# ---------------------------------------------------------------------------


def test_contrast_factor_above_one_increases_spread() -> None:
    base = _checkerboard()
    result = T.contrast(base, 2.0)
    assert result.astype(np.float32).std() > base.astype(np.float32).std()


def test_contrast_factor_below_one_decreases_spread() -> None:
    base = _checkerboard()
    result = T.contrast(base, 0.5)
    assert result.astype(np.float32).std() < base.astype(np.float32).std()


def test_contrast_zero_collapses_to_mid_gray() -> None:
    base = _checkerboard()
    result = T.contrast(base, 0.0)
    assert np.all(result == 127) or np.all(result == 128)  # 127.5 rounds/truncates per dtype cast


# ---------------------------------------------------------------------------
# brightness
# ---------------------------------------------------------------------------


def test_brightness_is_additive_on_mean() -> None:
    base = _solid(100)
    brighter = T.brightness(base, 20)
    darker = T.brightness(base, -20)
    assert brighter.mean() == pytest.approx(120.0, abs=0.5)
    assert darker.mean() == pytest.approx(80.0, abs=0.5)


def test_brightness_clips_at_range_bounds() -> None:
    assert T.brightness(_solid(250), 50).max() == 255
    assert T.brightness(_solid(5), -50).min() == 0


# ---------------------------------------------------------------------------
# gaussian_blur
# ---------------------------------------------------------------------------


def test_gaussian_blur_reduces_high_frequency_variance() -> None:
    base = _checkerboard()
    blurred = T.gaussian_blur(base, 7)
    assert blurred.astype(np.float32).var() < base.astype(np.float32).var()
    assert blurred.shape == base.shape
    assert blurred.dtype == base.dtype


def test_gaussian_blur_ksize_one_is_noop_copy() -> None:
    base = _checkerboard()
    result = T.gaussian_blur(base, 1)
    assert np.array_equal(result, base)
    assert result is not base  # a copy, not the same array object


def test_gaussian_blur_rejects_even_or_nonpositive_ksize() -> None:
    with pytest.raises(ValueError):
        T.gaussian_blur(_solid(100), 4)
    with pytest.raises(ValueError):
        T.gaussian_blur(_solid(100), 0)
    with pytest.raises(ValueError):
        T.gaussian_blur(_solid(100), -3)


# ---------------------------------------------------------------------------
# gaussian_noise
# ---------------------------------------------------------------------------


def test_gaussian_noise_increases_variance() -> None:
    base = _solid(128)
    noisy = T.gaussian_noise(base, sigma=15.0, seed=1)
    assert base.astype(np.float32).var() == 0.0  # solid image, zero variance to start
    assert noisy.astype(np.float32).var() > 0.0
    assert noisy.shape == base.shape
    assert noisy.dtype == base.dtype


def test_gaussian_noise_is_deterministic_given_same_seed() -> None:
    base = _checkerboard()
    a = T.gaussian_noise(base, sigma=10.0, seed=42)
    b = T.gaussian_noise(base, sigma=10.0, seed=42)
    assert np.array_equal(a, b)


def test_gaussian_noise_differs_across_seeds() -> None:
    base = _checkerboard()
    a = T.gaussian_noise(base, sigma=10.0, seed=1)
    b = T.gaussian_noise(base, sigma=10.0, seed=2)
    assert not np.array_equal(a, b)


def test_gaussian_noise_sigma_zero_is_noop() -> None:
    base = _checkerboard()
    result = T.gaussian_noise(base, sigma=0.0, seed=1)
    assert np.array_equal(result, base)


# ---------------------------------------------------------------------------
# jpeg_recompress
# ---------------------------------------------------------------------------


def test_jpeg_recompress_preserves_shape_and_dtype() -> None:
    base = _checkerboard()
    result = T.jpeg_recompress(base, 80)
    assert result.shape == base.shape
    assert result.dtype == base.dtype


def test_jpeg_recompress_lower_quality_diverges_more_from_source() -> None:
    base = _checkerboard()
    high_q = T.jpeg_recompress(base, 95)
    low_q = T.jpeg_recompress(base, 5)
    high_q_diff = np.abs(high_q.astype(np.int16) - base.astype(np.int16)).mean()
    low_q_diff = np.abs(low_q.astype(np.int16) - base.astype(np.int16)).mean()
    assert low_q_diff > high_q_diff


def test_jpeg_recompress_rejects_out_of_range_quality() -> None:
    with pytest.raises(ValueError):
        T.jpeg_recompress(_solid(100), 0)
    with pytest.raises(ValueError):
        T.jpeg_recompress(_solid(100), 101)


def test_jpeg_recompress_is_deterministic() -> None:
    base = _checkerboard()
    a = T.jpeg_recompress(base, 50)
    b = T.jpeg_recompress(base, 50)
    assert np.array_equal(a, b)
