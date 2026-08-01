"""Frame → detection-ready image: the preprocessing stage sequence.

Every function here is ``image -> image`` (or, for the one stage that
changes the coordinate space, ``image -> (image, Rescale)``): pure,
stateless, no camera access, no threading, no ring buffers, no globals —
the same constraint archive's ``core/detector.py`` stated for itself
(module docstring: "no camera access, no threading, no ring buffers").
Given the same array in, each function always produces the same array
out; nothing here reads the clock, a file, or module-level mutable
state. This is deliberately a set of *named* stages rather than one
monolithic method, per this ticket's contract (sprint 021-001) — a new
stage or a reordered pipeline does not require touching the others, and
each stage is independently testable and independently reusable by
ticket 003's detector.

Mined from v1's ``archive/src/aprilcam/core/detector.py``: the
``TagDetector._preprocess`` static method (highpass/CLAHE/sharpen) and
the ``proc_width`` scale/rescale block inline in ``TagDetector.detect``
(archive lines ~152-243). The *algorithm* is converted faithfully; the
*structure* is not — v1 buried all of this inside one detector class
method, this module gives each concern its own function and a config
object scoped to preprocessing only (detector-tuning fields such as
``quad_decimate``/``corner_refine``/family selection stay out of
:class:`PipelineConfig` and belong to ticket 003's detector config).

Stage order (grayscale → downscale → illumination flattening → CLAHE →
sharpen), reproduced in :func:`run_pipeline`, is v1's actual execution
order, not the order the stages are listed in prose above or in this
sprint's architecture table. v1 downscales *before* running the highpass/
CLAHE/sharpen block, not after — putting the most expensive per-pixel
work (a large Gaussian blur for the highpass, in particular) on the
smaller image is exactly the "efficiency is a requirement, not a hope"
rule the dev plan states for the camera pipeline. Reordering to
downscale last would be a plausible-looking but real behavioural
change, not a refactor, so it is not made here.

How ticket 003's detector should use this module: call
:func:`run_pipeline` with a :class:`PipelineConfig` for the common case
(one call gets a detection-ready grayscale image plus the
:class:`Rescale` needed to map detected corners back to the original
frame's pixel space). Call the individual stage functions directly only
when a caller needs to inspect or reuse an intermediate result — they
are exported for exactly that reason, not merely as private helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2 as cv
import numpy as np

__all__ = [
    "PipelineConfig",
    "Rescale",
    "to_grayscale",
    "flatten_illumination",
    "apply_clahe",
    "sharpen",
    "downscale_with_rescale",
    "run_pipeline",
]

#: v1's default highpass Gaussian kernel size (``DetectorConfig.highpass_ksize``).
DEFAULT_HIGHPASS_KSIZE = 51

#: v1's default CLAHE parameters (hardcoded inline in ``_preprocess``; not
#: previously configurable, made configurable here since nothing else in
#: v1 ever varied them).
DEFAULT_CLAHE_CLIP_LIMIT = 2.0
DEFAULT_CLAHE_TILE_GRID_SIZE = (8, 8)

#: v1's unsharp-mask kernel (``_preprocess``'s ``use_sharpen`` branch) —
#: a fixed 3x3 kernel, never parameterised in v1, so it is not
#: parameterised here either.
_SHARPEN_KERNEL = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)


def to_grayscale(image_bgr: np.ndarray) -> np.ndarray:
    """Convert a BGR frame to single-channel grayscale.

    OpenCV's ``VideoCapture``/``imread`` convention is BGR channel
    order, never RGB — this function assumes it, matching every other
    frame consumer in this codebase (``camera/backends``). Every later
    stage in this module operates on the grayscale result; nothing
    downstream of this function touches colour channels again.
    """
    return cv.cvtColor(image_bgr, cv.COLOR_BGR2GRAY)


def flatten_illumination(gray: np.ndarray, *, ksize: int = DEFAULT_HIGHPASS_KSIZE) -> np.ndarray:
    """Divide out a large-scale illumination gradient (the "highpass" stage).

    Estimates the low-frequency illumination field with a large Gaussian
    blur, then divides the image by that estimate and rescales around a
    mid-grey of 128 — a shadowed tag square and a well-lit one end up at
    comparable intensity, which is the whole point: AprilTag/ArUco
    detection depends on local black/white contrast, not absolute
    brightness, and a real playfield is rarely lit evenly (v1's
    motivation, unchanged here).

    *ksize* must be odd (``cv.GaussianBlur``'s requirement); an even
    value is incremented by one, matching v1's own auto-correction
    rather than raising, since the caller-facing ``PipelineConfig``
    default (51) is already odd and this only guards a hand-rolled
    config.
    """
    k = ksize if ksize % 2 == 1 else ksize + 1
    illum = cv.GaussianBlur(gray, (k, k), 0).astype(np.float32)
    illum = np.maximum(illum, 1.0)  # never divide by (near-)zero
    flat = (gray.astype(np.float32) / illum) * 128.0
    return np.clip(flat, 0, 255).astype(np.uint8)


def apply_clahe(
    gray: np.ndarray,
    *,
    clip_limit: float = DEFAULT_CLAHE_CLIP_LIMIT,
    tile_grid_size: tuple[int, int] = DEFAULT_CLAHE_TILE_GRID_SIZE,
) -> np.ndarray:
    """Contrast-Limited Adaptive Histogram Equalisation.

    Off by default (``PipelineConfig.use_clahe``), same as v1 — an
    optional local-contrast boost for scenes the highpass stage alone
    does not flatten enough, not a stage every frame needs.
    """
    clahe = cv.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)
    result: np.ndarray = clahe.apply(gray)
    return result


def sharpen(gray: np.ndarray) -> np.ndarray:
    """Apply v1's fixed 3x3 unsharp-mask kernel.

    Off by default (``PipelineConfig.use_sharpen``), same as v1 — a
    tool for soft-focus cameras, not a universal step.
    """
    result: np.ndarray = cv.filter2D(gray, -1, _SHARPEN_KERNEL)
    return result


@dataclass(frozen=True, kw_only=True)
class Rescale:
    """The scale factor :func:`downscale_with_rescale` applied.

    ``scale`` is the ratio (downscaled width) / (original width), always
    ``<= 1.0``; ``1.0`` means no resizing happened (the image was
    already at or below the target width, or downscaling was disabled).
    Detected coordinates coming out of a detector run against the
    downscaled image are in *that* image's pixel space — call
    :meth:`to_original` (or divide an ``(N, 2)`` corner array by
    :attr:`scale` directly, the same operation) before returning
    anything to a caller, who thinks entirely in the original frame's
    pixel space (module docstring's "no world/pixel interpretation of
    results" boundary stops at *this* frame, not beyond it — rescaling
    detected pixels back to the frame actually captured is still this
    module's job).
    """

    scale: float

    def to_original(self, x: float, y: float) -> tuple[float, float]:
        """Map one point from the downscaled image back to the original."""
        return (x / self.scale, y / self.scale)


def downscale_with_rescale(gray: np.ndarray, *, proc_width: int) -> tuple[np.ndarray, Rescale]:
    """Shrink *gray* to at most *proc_width* wide, for cheaper detection.

    Mirrors v1's inline ``proc_width`` block in ``TagDetector.detect``:
    ``scale = min(1.0, proc_width / original_width)`` — this never
    upscales — and resizing uses ``cv.INTER_AREA``, the interpolation
    OpenCV recommends for shrinking. ``proc_width <= 0`` disables
    downscaling outright and returns *gray* unchanged with
    ``Rescale(scale=1.0)``, matching ``DetectorConfig.proc_width``'s "0
    disables" convention.

    The returned :class:`Rescale` carries the *analytic* scale factor
    (``proc_width / original_width``, before the integer rounding that
    picks actual output pixel dimensions) — the same value used to
    resize is the same value used to map back, so the round trip is
    exact to floating-point precision rather than compounding a second
    rounding error on the way back.
    """
    h, w = gray.shape[:2]
    scale = 1.0
    if proc_width > 0 and w > 0:
        scale = min(1.0, float(proc_width) / float(w))
    if scale >= 1.0:
        return gray, Rescale(scale=1.0)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = cv.resize(gray, (new_w, new_h), interpolation=cv.INTER_AREA)
    return resized, Rescale(scale=scale)


@dataclass(frozen=True, kw_only=True)
class PipelineConfig:
    """Which preprocessing stages :func:`run_pipeline` runs, and their parameters.

    Scoped to preprocessing only — mirrors the preprocessing-relevant
    subset of archive's ``DetectorConfig`` (``use_highpass``,
    ``highpass_ksize``, ``use_clahe``, ``use_sharpen``, ``proc_width``).
    Detector-tuning fields (``quad_decimate``, ``corner_refine``, family
    selection, AprilTag quad-detector thresholds, ...) are ticket 003's
    concern, not this module's — a new detector parameter must never
    require a change here (sprint.md Step 2's "changes independently"
    module boundary).
    """

    use_highpass: bool = True
    highpass_ksize: int = DEFAULT_HIGHPASS_KSIZE
    use_clahe: bool = False
    clahe_clip_limit: float = DEFAULT_CLAHE_CLIP_LIMIT
    clahe_tile_grid_size: tuple[int, int] = DEFAULT_CLAHE_TILE_GRID_SIZE
    use_sharpen: bool = False
    proc_width: int = 0


def run_pipeline(
    image_bgr: np.ndarray, config: Optional[PipelineConfig] = None
) -> tuple[np.ndarray, Rescale]:
    """Run the full, configured stage sequence over one BGR frame.

    Fixed order: grayscale → downscale (if ``proc_width > 0``) →
    illumination flattening (if ``use_highpass``) → CLAHE (if
    ``use_clahe``) → sharpen (if ``use_sharpen``) — v1's own order, see
    the module docstring for why downscale runs before the rest rather
    than after. Deterministic by construction: every stage this calls is
    itself pure, so the same *image_bgr* and *config* always produce a
    byte-identical result, with no globals or hidden state anywhere in
    the chain (this ticket's acceptance criterion).

    Returns the preprocessed grayscale image plus the :class:`Rescale`
    needed to map anything detected in it back to *image_bgr*'s own
    pixel space — ``Rescale(scale=1.0)`` (a no-op mapping) when
    downscaling did not run.
    """
    cfg = config if config is not None else PipelineConfig()

    gray = to_grayscale(image_bgr)
    gray, rescale = downscale_with_rescale(gray, proc_width=cfg.proc_width)

    if cfg.use_highpass:
        gray = flatten_illumination(gray, ksize=cfg.highpass_ksize)
    if cfg.use_clahe:
        gray = apply_clahe(
            gray, clip_limit=cfg.clahe_clip_limit, tile_grid_size=cfg.clahe_tile_grid_size
        )
    if cfg.use_sharpen:
        gray = sharpen(gray)

    return gray, rescale
