"""Synthetic degradation transforms (sprint 035 ticket 007).

Seven pure ``image: np.ndarray -> np.ndarray`` functions --
:func:`exposure_scale`, :func:`gamma`, :func:`contrast`,
:func:`brightness`, :func:`gaussian_blur`, :func:`gaussian_noise`,
:func:`jpeg_recompress` -- that each take one BGR (or grayscale)
``uint8`` array and return a same-shape, same-dtype array with one
named kind of degradation applied. Nothing here does file I/O, opens a
camera, or calls a detector; every function is deterministic given its
arguments (:func:`gaussian_noise` included -- see its own docstring) so
this module composes safely with ``test_synthetic_degradation.py``'s
fixture-driven grid without becoming a second source of flakiness on
top of the physical capture matrix ticket 005/006 already built.

**Why synthetic, alongside ticket 005/006's captured sweeps.** The
physical capture matrix (``tests/images/fs-20260823/``) varies real
camera controls -- exposure, gain, white balance -- which is the only
way to learn a *real* washout/dark-floor cliff. It does not, and
cannot easily, vary lens blur or sensor noise independently of those
controls, and it can never be re-run without a human at the rig. These
transforms let ``test_synthetic_degradation.py`` stress the same
already-captured baseline images along axes (blur, noise, JPEG
artifacting) the physical rig does not directly sweep, and to do so
offline, deterministically, on every future test run.

All seven functions clip to the input dtype's valid range (``[0, 255]``
for ``uint8``) before casting back, matching how a real sensor or
codec would saturate rather than wrap or produce out-of-range values.
"""

from __future__ import annotations

import numpy as np
import cv2 as cv

__all__ = [
    "exposure_scale",
    "gamma",
    "contrast",
    "brightness",
    "gaussian_blur",
    "gaussian_noise",
    "jpeg_recompress",
]


def exposure_scale(image: np.ndarray, factor: float) -> np.ndarray:
    """Multiply every pixel by *factor* -- a linear stand-in for a
    sensor exposure/gain change (brighter for ``factor > 1``, darker
    for ``factor < 1``), clipped to the input dtype's range.
    """
    scaled = image.astype(np.float32) * float(factor)
    return np.clip(scaled, 0, 255).astype(image.dtype)


def gamma(image: np.ndarray, gamma_value: float) -> np.ndarray:
    """Apply gamma correction: ``output = 255 * (input / 255) ** gamma_value``.

    ``gamma_value < 1`` lifts shadows/midtones (brightens); ``gamma_value
    > 1`` crushes them (darkens). ``gamma_value`` must be positive.
    """
    if gamma_value <= 0:
        raise ValueError(f"gamma_value must be positive, got {gamma_value}")
    normalized = image.astype(np.float32) / 255.0
    corrected = np.power(normalized, float(gamma_value)) * 255.0
    return np.clip(corrected, 0, 255).astype(image.dtype)


def contrast(image: np.ndarray, factor: float) -> np.ndarray:
    """Scale pixel values around mid-gray (127.5) by *factor* --
    ``factor > 1`` increases contrast, ``factor < 1`` flattens it
    (``factor == 0`` collapses the image to flat mid-gray).
    """
    adjusted = (image.astype(np.float32) - 127.5) * float(factor) + 127.5
    return np.clip(adjusted, 0, 255).astype(image.dtype)


def brightness(image: np.ndarray, delta: float) -> np.ndarray:
    """Add a constant *delta* to every pixel, clipped to the input
    dtype's range -- the additive counterpart to :func:`exposure_scale`'s
    multiplicative brighten/darken.
    """
    adjusted = image.astype(np.float32) + float(delta)
    return np.clip(adjusted, 0, 255).astype(image.dtype)


def gaussian_blur(image: np.ndarray, ksize: int) -> np.ndarray:
    """Gaussian-blur *image* with a square ``ksize x ksize`` kernel
    (sigma derived from ``ksize`` by OpenCV, i.e. ``sigma=0``).

    *ksize* must be a positive odd integer (OpenCV's own
    ``GaussianBlur`` requirement). ``ksize == 1`` is a no-op (returns a
    copy, since a 1x1 kernel changes nothing) rather than raising, so
    callers can include "no blur" in a grid without special-casing it.
    """
    if ksize < 1 or ksize % 2 == 0:
        raise ValueError(f"ksize must be a positive odd integer, got {ksize}")
    if ksize == 1:
        return image.copy()
    return cv.GaussianBlur(image, (ksize, ksize), 0)


def gaussian_noise(image: np.ndarray, sigma: float, seed: int) -> np.ndarray:
    """Add zero-mean Gaussian noise (standard deviation *sigma*) to
    *image*, clipped to the input dtype's range.

    Deterministic: draws from ``numpy.random.default_rng(seed)``, a
    fresh, locally-scoped generator seeded per call rather than the
    module/global RNG state, so the same ``(image, sigma, seed)``
    always produces the exact same output array and two calls never
    interfere with each other's draws regardless of call order --
    exactly the ``Math.random``-style nondeterminism this ticket's
    instructions warn corrupts a test corpus.
    """
    rng = np.random.default_rng(seed)
    noise = rng.normal(loc=0.0, scale=float(sigma), size=image.shape)
    noisy = image.astype(np.float32) + noise
    return np.clip(noisy, 0, 255).astype(image.dtype)


def jpeg_recompress(image: np.ndarray, quality: int) -> np.ndarray:
    """Round-trip *image* through JPEG encoding at *quality* (1-100,
    OpenCV/libjpeg convention -- higher is less lossy) and back,
    returning the decoded array -- simulates the compression artifacts
    a real capture/transport pipeline can introduce.
    """
    if not 1 <= quality <= 100:
        raise ValueError(f"quality must be in [1, 100], got {quality}")
    ok, encoded = cv.imencode(".jpg", image, [int(cv.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("jpeg_recompress: cv2.imencode failed")
    decoded = cv.imdecode(encoded, cv.IMREAD_UNCHANGED)
    if decoded is None:
        raise RuntimeError("jpeg_recompress: cv2.imdecode failed")
    return decoded
