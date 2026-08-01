"""Tests for ``aprilcam.vision.pipeline`` (headless, no hardware).

Covers ticket 021-001's acceptance criteria: per-stage shape/dtype and
determinism, illumination flattening's normalizing effect on a lighting
gradient, the downscale stage's coordinate round trip within an epsilon,
and a full configured :func:`run_pipeline` sequence producing a
byte-identical result across repeat runs on the same fixture.

This is a self-contained correctness test against today's
``vision/pipeline.py`` — not a comparison against v1 output, per the
ticket's testing note (v1's ``core/detector.py`` is archived, not
imported). Every image these tests operate on is real fixture pixel
data loaded from ``tests/images/`` (restored by this same ticket); no
camera is opened. Where a test needs a *guaranteed* illumination
gradient or a specific pixel coordinate to check geometry against, it
imposes that on top of real fixture content rather than inventing a
frame from nothing.
"""

from __future__ import annotations

from pathlib import Path

import cv2 as cv
import numpy as np
import pytest

from aprilcam.vision.pipeline import (
    DEFAULT_HIGHPASS_KSIZE,
    PipelineConfig,
    Rescale,
    apply_clahe,
    downscale_with_rescale,
    flatten_illumination,
    run_pipeline,
    sharpen,
    to_grayscale,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = REPO_ROOT / "tests" / "images" / "arducam-ov9782-usb-camera"
BASELINE_JPG = FIXTURE_DIR / "baseline.jpg"


def _load_bgr_fixture() -> np.ndarray:
    """The arducam baseline fixture, as a real BGR frame (no synthesis)."""
    image = cv.imread(str(BASELINE_JPG), cv.IMREAD_COLOR)
    assert image is not None, f"fixture missing or unreadable: {BASELINE_JPG}"
    return image


def _load_gray_fixture() -> np.ndarray:
    """The arducam baseline fixture, decoded straight to grayscale."""
    image = cv.imread(str(BASELINE_JPG), cv.IMREAD_GRAYSCALE)
    assert image is not None, f"fixture missing or unreadable: {BASELINE_JPG}"
    return image


def _block_mean_variance(gray: np.ndarray, blocks: int = 8) -> float:
    """Variance of block-averaged intensity — a coarse illumination-flatness metric.

    Splits *gray* into a ``blocks x blocks`` grid, averages each cell,
    and returns the variance across those cell means. A frame with a
    strong large-scale lighting gradient has widely differing block
    means (high variance); a flattened frame's block means cluster
    closer together (low variance). Exactly the statistical property
    the ticket's testing section names for the flattening stage.
    """
    h, w = gray.shape[:2]
    bh, bw = h // blocks, w // blocks
    means = [
        float(gray[by * bh : (by + 1) * bh, bx * bw : (bx + 1) * bw].mean())
        for by in range(blocks)
        for bx in range(blocks)
    ]
    return float(np.var(means))


def _with_synthetic_gradient(gray: np.ndarray) -> np.ndarray:
    """Real fixture texture with a strong, known left-to-right brightness ramp imposed.

    Real captured lighting is not reliably strong or uniform enough
    across a fixture image to make a flattening assertion robust by
    itself, so this multiplies in a deliberate gradient (0.3x on the
    left to 1.9x on the right) on top of the fixture's own real pixel
    content — the texture is real, the illumination defect being
    corrected is a controlled, known quantity.
    """
    h, w = gray.shape[:2]
    ramp = np.linspace(0.3, 1.9, w, dtype=np.float32)
    gradient = np.tile(ramp, (h, 1))
    graded = np.clip(gray.astype(np.float32) * gradient, 0, 255)
    return graded.astype(np.uint8)


# ---------------------------------------------------------------------------
# to_grayscale
# ---------------------------------------------------------------------------


def test_to_grayscale_shape_and_dtype():
    bgr = _load_bgr_fixture()
    gray = to_grayscale(bgr)
    assert gray.ndim == 2
    assert gray.shape == bgr.shape[:2]
    assert gray.dtype == np.uint8


def test_to_grayscale_deterministic():
    bgr = _load_bgr_fixture()
    first = to_grayscale(bgr)
    second = to_grayscale(bgr)
    assert np.array_equal(first, second)


# ---------------------------------------------------------------------------
# flatten_illumination
# ---------------------------------------------------------------------------


def test_flatten_illumination_normalizes_gradient():
    graded = _with_synthetic_gradient(_load_gray_fixture())
    before = _block_mean_variance(graded)
    after = _block_mean_variance(flatten_illumination(graded))
    # The imposed gradient is large (0.3x-1.9x); a working flatten stage
    # should collapse the block-mean spread by a wide margin, not just
    # nudge it. Guards against a no-op or near-no-op implementation
    # passing on noise alone.
    assert after < before * 0.5, (before, after)


def test_flatten_illumination_shape_dtype_preserved():
    gray = _load_gray_fixture()
    flat = flatten_illumination(gray)
    assert flat.shape == gray.shape
    assert flat.dtype == np.uint8


def test_flatten_illumination_deterministic():
    gray = _load_gray_fixture()
    first = flatten_illumination(gray)
    second = flatten_illumination(gray)
    assert np.array_equal(first, second)


def test_flatten_illumination_even_ksize_is_corrected_not_rejected():
    gray = _load_gray_fixture()
    # An even ksize must not raise -- it is silently incremented to the
    # next odd value, matching v1's own auto-correction (module docstring).
    even = flatten_illumination(gray, ksize=DEFAULT_HIGHPASS_KSIZE + 1)
    odd = flatten_illumination(gray, ksize=DEFAULT_HIGHPASS_KSIZE + 1 + 1)
    assert np.array_equal(even, odd)


# ---------------------------------------------------------------------------
# apply_clahe
# ---------------------------------------------------------------------------


def test_apply_clahe_shape_dtype_preserved():
    gray = _load_gray_fixture()
    out = apply_clahe(gray)
    assert out.shape == gray.shape
    assert out.dtype == np.uint8


def test_apply_clahe_deterministic():
    gray = _load_gray_fixture()
    first = apply_clahe(gray)
    second = apply_clahe(gray)
    assert np.array_equal(first, second)


# ---------------------------------------------------------------------------
# sharpen
# ---------------------------------------------------------------------------


def test_sharpen_shape_dtype_preserved():
    gray = _load_gray_fixture()
    out = sharpen(gray)
    assert out.shape == gray.shape
    assert out.dtype == np.uint8


def test_sharpen_deterministic():
    gray = _load_gray_fixture()
    first = sharpen(gray)
    second = sharpen(gray)
    assert np.array_equal(first, second)


# ---------------------------------------------------------------------------
# downscale_with_rescale — shrinking behaviour
# ---------------------------------------------------------------------------


def test_downscale_with_rescale_shrinks_to_proc_width():
    gray = _load_gray_fixture()
    original_w = gray.shape[1]
    proc_width = original_w // 4
    resized, rescale = downscale_with_rescale(gray, proc_width=proc_width)
    assert resized.shape[1] == proc_width
    assert resized.dtype == np.uint8
    assert rescale.scale == pytest.approx(proc_width / original_w)


def test_downscale_with_rescale_disabled_when_proc_width_zero():
    gray = _load_gray_fixture()
    resized, rescale = downscale_with_rescale(gray, proc_width=0)
    assert resized.shape == gray.shape
    assert np.array_equal(resized, gray)
    assert rescale.scale == 1.0


def test_downscale_with_rescale_never_upscales():
    gray = _load_gray_fixture()
    oversized_proc_width = gray.shape[1] * 4
    resized, rescale = downscale_with_rescale(gray, proc_width=oversized_proc_width)
    assert resized.shape == gray.shape
    assert np.array_equal(resized, gray)
    assert rescale.scale == 1.0


def test_downscale_with_rescale_deterministic():
    gray = _load_gray_fixture()
    proc_width = gray.shape[1] // 3
    first, first_rescale = downscale_with_rescale(gray, proc_width=proc_width)
    second, second_rescale = downscale_with_rescale(gray, proc_width=proc_width)
    assert np.array_equal(first, second)
    assert first_rescale == second_rescale


# ---------------------------------------------------------------------------
# downscale_with_rescale — coordinate round trip (geometry discipline)
# ---------------------------------------------------------------------------


def test_rescale_round_trip_maps_known_point_within_epsilon():
    gray = _load_gray_fixture()
    h, w = gray.shape[:2]
    # A proc_width chosen so the scale is not a tidy fraction, so this
    # exercises real floating-point division rather than an accidental
    # exact ratio.
    proc_width = int(w * 0.37)
    _resized, rescale = downscale_with_rescale(gray, proc_width=proc_width)
    assert rescale.scale < 1.0  # sanity: downscaling actually happened

    original_point = (w * 0.83, h * 0.21)
    # Simulate "detected in the downscaled image": the same forward
    # mapping a detector's corners would have undergone during resize.
    downscaled_point = (original_point[0] * rescale.scale, original_point[1] * rescale.scale)

    recovered = rescale.to_original(*downscaled_point)

    epsilon = 1e-6
    assert recovered[0] == pytest.approx(original_point[0], abs=epsilon)
    assert recovered[1] == pytest.approx(original_point[1], abs=epsilon)


def test_rescale_no_op_round_trip_is_identity():
    identity = Rescale(scale=1.0)
    assert identity.to_original(123.5, 456.75) == (123.5, 456.75)


# ---------------------------------------------------------------------------
# run_pipeline — full configured sequence
# ---------------------------------------------------------------------------


def test_run_pipeline_deterministic_on_fixture():
    bgr = _load_bgr_fixture()
    config = PipelineConfig(
        use_highpass=True, use_clahe=True, use_sharpen=True, proc_width=bgr.shape[1] // 2
    )
    first_image, first_rescale = run_pipeline(bgr, config)
    second_image, second_rescale = run_pipeline(bgr, config)
    assert np.array_equal(first_image, second_image)
    assert first_rescale == second_rescale


def test_run_pipeline_default_config_no_downscale():
    bgr = _load_bgr_fixture()
    image, rescale = run_pipeline(bgr)
    assert image.shape == bgr.shape[:2]
    assert rescale.scale == 1.0


def test_run_pipeline_matches_manual_stage_composition():
    bgr = _load_bgr_fixture()
    config = PipelineConfig(use_highpass=True, use_clahe=False, use_sharpen=False, proc_width=0)
    piped_image, piped_rescale = run_pipeline(bgr, config)

    manual = to_grayscale(bgr)
    manual, manual_rescale = downscale_with_rescale(manual, proc_width=config.proc_width)
    manual = flatten_illumination(manual, ksize=config.highpass_ksize)

    assert np.array_equal(piped_image, manual)
    assert piped_rescale == manual_rescale


def test_run_pipeline_toggles_change_output():
    bgr = _load_bgr_fixture()
    bare, _ = run_pipeline(bgr, PipelineConfig(use_highpass=False))
    highpassed, _ = run_pipeline(bgr, PipelineConfig(use_highpass=True))
    # A no-op preprocessing implementation would make every config
    # produce the same array; guard against that regression.
    assert not np.array_equal(bare, highpassed)
