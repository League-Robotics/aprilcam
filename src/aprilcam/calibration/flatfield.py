"""``FlatFieldCapturer``: build a per-camera flat-field gain map from an
already-captured frame burst and a precomputed field-interior mask
(sprint 037, ticket 001).

Mirrors ``calibration.py``'s ``Calibrator`` shape closely, and its
"camera frame access is handed in, never owned" invariant exactly (see
this module's own ``interfaces.FlatFieldCapturer`` docstring): this
class is pure -- it never opens a camera, never touches a filesystem,
and never reaches into daemon state. It consumes only the frame burst
and mask :meth:`FlatFieldCapturer.build` is called with. Pushing a
temporary exposure override, capturing the burst, and deriving the mask
(from a ``Calibration`` or ArUco detection) are ticket 002's job,
``daemon/service.py``.

**The algorithm, end to end** (``build``, see
``clasi/sprints/037-flat-field-gain-map-capture-as-a-daemon-workflow/
design/calibration-DESIGN.md`` §4 for the full narrative this mirrors):

1. Normalise each frame to the batch mean brightness -- corrects the
   ~25% frame-to-frame light flicker measured at short exposures
   (``docs/knowledge/flat-field-gain-map.md``) without needing a
   flicker model, since the burst's own mean is the reference.
2. Average the normalised frames.
3. Median-blur each channel independently -- kills sheet wrinkles and
   specular highlights without smearing the low-frequency light field.
4. Fill outside the mask with the mask's own interior mean -- no border
   bleed into the estimate.
5. Wide Gaussian blur to keep only the low-frequency light field ``L``.
6. ``gain = target / max(L, eps)`` per channel, where ``target`` is the
   mean of ``L`` inside the mask across channels; clipped to a fixed
   sane range (mirrors ``camera.camera.Camera._apply_flatfield``'s
   multiplicative-correction assumption).
7. Quality gate (:meth:`FlatFieldCapturer._evaluate_quality`): in-field
   clipping (any *raw* source frame saturating inside the mask),
   implausible spread (p98/p2 luma ratio inside the mask exceeding a
   fixed threshold), and frame-to-frame disagreement (each frame's own
   independently-computed low-frequency light field deviating from the
   batch's beyond the flicker-normalisation tolerance already applied in
   step 1 -- this catches the sheet *moving*, which per-frame mean
   normalisation alone cannot). A failing check without ``force=True``
   returns ``succeeded=False`` with the *measured* spread still
   reported -- never a silently-passed gate.
8. On success (or ``force=True``), returns a ``FlatFieldResult`` with
   ``succeeded=True`` carrying the built gain array; the caller
   (``daemon/service.py``) is what persists it.

The constants below (median-blur kernel, Gaussian sigma, gain clip
range, spread/disagreement thresholds) are sprint 037's confirmed
defaults, tuned against this project's own two real cameras -- measured
2.65x/1.32x spread and ~25% flicker
(``docs/knowledge/flat-field-gain-map.md``) -- and the scratch reference
implementation that first validated the approach
(``docs/knowledge/assets/build_flatfield_2026-09-02.py``). Like
``MAX_REPROJECTION_ERROR_CM``, they carry the same "confirmed default,
not yet validated against other hardware" caveat.
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

import cv2 as cv
import numpy as np

from aprilcam.calibration.interfaces import FlatFieldCapturer as _FlatFieldCapturerBase
from aprilcam.calibration.interfaces import FlatFieldResult

__all__ = ["FlatFieldCapturer"]

logger = logging.getLogger(__name__)

#: Median-blur kernel size (odd, pixels) -- kills sheet wrinkles and
#: specular highlights without smearing the low-frequency light field.
MEDIAN_BLUR_KSIZE = 31

#: Gaussian blur sigma (pixels), applied after the median blur, that
#: keeps only the low-frequency light field ``L``.
GAUSSIAN_BLUR_SIGMA = 40.0

#: Clip range for the final gain map -- mirrors
#: ``Camera._apply_flatfield``'s multiplicative-correction assumption
#: (finite, non-negative gains) and keeps a pathologically dark corner
#: from amplifying sensor noise beyond usefulness.
GAIN_CLIP_MIN = 0.3
GAIN_CLIP_MAX = 4.0

#: A raw (pre-normalisation) 8-bit channel value at/above this is
#: considered saturated for the in-field-clipping quality check.
SATURATION_VALUE = 254.0

#: Quality gate: p98/p2 luma spread inside the mask beyond this is
#: implausible for a real capture -- this project's two real cameras
#: measured 2.65x/1.32x (``docs/knowledge/flat-field-gain-map.md``).
MAX_PLAUSIBLE_SPREAD = 10.0

#: Quality gate: how far one frame's own low-frequency light field may
#: deviate (RMS relative, in-mask) from the batch's, after step 1's
#: per-frame mean normalisation has already corrected ~25% flicker. A
#: bigger residual means the sheet itself moved during the burst, not
#: just ordinary flicker.
MAX_FRAME_DISAGREEMENT = 0.15

#: Named quality-gate checks, in the order :meth:`FlatFieldCapturer.
#: _evaluate_quality` evaluates them -- also the vocabulary
#: ``FlatFieldResult.forced_checks`` reports from.
CHECK_CLIPPING = "clipping"
CHECK_SPREAD = "spread"
CHECK_DISAGREEMENT = "disagreement"

_EPS = 1e-6


def _to_channels(frame: np.ndarray) -> np.ndarray:
    """*frame* as ``(H, W, C)`` float32 -- a 2-D (mono) frame gains a
    trailing axis of length 1 so every downstream step can treat mono
    and colour frames identically."""
    arr = np.asarray(frame, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[:, :, np.newaxis]
    return arr


def _luma(channels: np.ndarray) -> np.ndarray:
    """Rec.601 luma of an ``(H, W, C)`` BGR array; a mono (``C == 1``)
    array passes through its single channel unchanged."""
    if channels.shape[2] == 1:
        return channels[:, :, 0]
    b, g, r = channels[:, :, 0], channels[:, :, 1], channels[:, :, 2]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _percentile_spread(channels: np.ndarray, mask: np.ndarray) -> Tuple[float, ...]:
    """Per-channel p98/p2 ratio inside *mask*, one value per channel."""
    result: List[float] = []
    for ch in range(channels.shape[2]):
        band = channels[:, :, ch]
        values = band[mask] if mask.any() else band.reshape(-1)
        p2 = float(np.percentile(values, 2))
        p98 = float(np.percentile(values, 98))
        result.append(p98 / max(p2, _EPS))
    return tuple(result)


class FlatFieldCapturer(_FlatFieldCapturerBase):
    """Concrete ``calibration.interfaces.FlatFieldCapturer``.

    Parameters
    ----------
    median_blur_ksize, gaussian_blur_sigma, gain_clip,
    max_plausible_spread, max_frame_disagreement, saturation_value:
        Overridable tuning constants -- default to this module's own
        confirmed defaults above. Genuinely overridable per instance
        (mirrors ``Calibrator``'s ``max_reprojection_error_cm``
        parameter), since these are domain-tuning calls, not structural
        ones, and a future camera/lighting setup may need different
        numbers.
    """

    def __init__(
        self,
        *,
        median_blur_ksize: int = MEDIAN_BLUR_KSIZE,
        gaussian_blur_sigma: float = GAUSSIAN_BLUR_SIGMA,
        gain_clip: Tuple[float, float] = (GAIN_CLIP_MIN, GAIN_CLIP_MAX),
        max_plausible_spread: float = MAX_PLAUSIBLE_SPREAD,
        max_frame_disagreement: float = MAX_FRAME_DISAGREEMENT,
        saturation_value: float = SATURATION_VALUE,
    ) -> None:
        self._median_blur_ksize = median_blur_ksize
        self._gaussian_blur_sigma = gaussian_blur_sigma
        self._gain_clip = gain_clip
        self._max_plausible_spread = max_plausible_spread
        self._max_frame_disagreement = max_frame_disagreement
        self._saturation_value = saturation_value

    def build(self, frames: Sequence[np.ndarray], mask: np.ndarray, *, force: bool = False) -> FlatFieldResult:
        """See ``calibration.interfaces.FlatFieldCapturer.build``.

        Never opens a camera, never touches a filesystem: *frames* and
        *mask* are the only inputs this method reads.
        """
        if not frames:
            raise ValueError("FlatFieldCapturer.build() requires at least one frame")

        mask_bool = np.asarray(mask, dtype=bool)

        raw = [_to_channels(f) for f in frames]
        means = [float(f.mean()) for f in raw]
        batch_mean = float(np.mean(means))
        normalised = [f * (batch_mean / max(m, _EPS)) for f, m in zip(raw, means)]

        avg = np.mean(normalised, axis=0).astype(np.float32)
        light_field = self._light_field(avg, mask_bool)

        num_channels = light_field.shape[2]
        if mask_bool.any():
            target = float(np.mean([light_field[:, :, c][mask_bool].mean() for c in range(num_channels)]))
        else:
            target = float(np.mean([light_field[:, :, c].mean() for c in range(num_channels)]))
        gain = np.clip(
            target / np.maximum(light_field, _EPS),
            self._gain_clip[0],
            self._gain_clip[1],
        ).astype(np.float32)

        corrected = avg * gain
        spread_before = _percentile_spread(avg, mask_bool)
        spread_after = _percentile_spread(corrected, mask_bool)

        failed_checks, reasons = self._evaluate_quality(raw, normalised, avg, light_field, mask_bool)

        sample_ndim = np.asarray(frames[0]).ndim
        result_gain = gain[:, :, 0] if sample_ndim == 2 else gain

        if not failed_checks:
            return FlatFieldResult(
                succeeded=True,
                spread_before=spread_before,
                spread_after=spread_after,
                reason=None,
                forced_checks=(),
                gain=result_gain,
            )

        reason = "; ".join(reasons)
        if force:
            return FlatFieldResult(
                succeeded=True,
                spread_before=spread_before,
                spread_after=spread_after,
                reason=reason,
                forced_checks=tuple(failed_checks),
                gain=result_gain,
            )

        return FlatFieldResult(
            succeeded=False,
            spread_before=spread_before,
            spread_after=spread_after,
            reason=reason,
            forced_checks=(),
            gain=None,
        )

    # -- internal ---------------------------------------------------------

    def _light_field(self, channels: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """Per-channel low-frequency light field: median-blur (kills
        wrinkles/specular highlights), fill outside *mask* with the
        interior mean (no border bleed), wide Gaussian blur (keep only
        the low-frequency component)."""
        h, w, c = channels.shape
        field = np.empty((h, w, c), dtype=np.float32)
        for ch in range(c):
            band = np.clip(channels[:, :, ch], 0, 255).astype(np.uint8)
            med = cv.medianBlur(band, self._median_blur_ksize).astype(np.float32)
            interior_mean = float(med[mask].mean()) if mask.any() else float(med.mean())
            filled = np.where(mask, med, interior_mean).astype(np.float32)
            field[:, :, ch] = cv.GaussianBlur(filled, (0, 0), self._gaussian_blur_sigma)
        return field

    def _evaluate_quality(
        self,
        raw: List[np.ndarray],
        normalised: List[np.ndarray],
        avg: np.ndarray,
        light_field: np.ndarray,
        mask: np.ndarray,
    ) -> Tuple[List[str], List[str]]:
        """Run the three quality-gate checks. Returns the ordered list
        of failed check names and the matching human-readable reasons
        (same length, same order) -- never raises."""
        failed: List[str] = []
        reasons: List[str] = []

        if mask.any() and self._has_in_field_clipping(raw, mask):
            failed.append(CHECK_CLIPPING)
            reasons.append("in-field clipping detected in at least one source frame")

        luma_spread = self._luma_spread(avg, mask)
        if luma_spread > self._max_plausible_spread:
            failed.append(CHECK_SPREAD)
            reasons.append(
                f"implausible in-field spread {luma_spread:.2f}x exceeds "
                f"{self._max_plausible_spread:.2f}x threshold"
            )

        disagreement = self._max_frame_disagreement_measured(normalised, light_field, mask)
        if disagreement > self._max_frame_disagreement:
            failed.append(CHECK_DISAGREEMENT)
            reasons.append(
                f"frame-to-frame disagreement {disagreement:.2%} exceeds "
                f"{self._max_frame_disagreement:.2%} flicker-normalisation tolerance "
                "-- the sheet may have moved during capture"
            )

        return failed, reasons

    def _has_in_field_clipping(self, raw: List[np.ndarray], mask: np.ndarray) -> bool:
        for frame in raw:
            channel_max = frame.max(axis=2)
            if bool((channel_max[mask] >= self._saturation_value).any()):
                return True
        return False

    def _luma_spread(self, channels: np.ndarray, mask: np.ndarray) -> float:
        luma = _luma(channels)
        values = luma[mask] if mask.any() else luma.reshape(-1)
        p2 = float(np.percentile(values, 2))
        p98 = float(np.percentile(values, 98))
        return p98 / max(p2, _EPS)

    def _max_frame_disagreement_measured(
        self, normalised: List[np.ndarray], batch_field: np.ndarray, mask: np.ndarray
    ) -> float:
        if not mask.any():
            return 0.0
        worst = 0.0
        for frame in normalised:
            frame_field = self._light_field(frame, mask)
            rel = np.abs((frame_field - batch_field) / np.maximum(batch_field, _EPS))
            dev = float(np.sqrt(np.mean(rel[mask] ** 2)))
            worst = max(worst, dev)
        return worst
