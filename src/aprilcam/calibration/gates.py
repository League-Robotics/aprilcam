"""Calibration quality gates: pure decisions over numbers (sprint 045 ticket 004).

**cv2-free by construction** (numpy and the standard library only): every
function here decides from metrics that ``calibration/pose.py`` computed.
That is the point of the split (sprint 045 Decision 7): thresholds are one
edit away, and the whole degradation ladder is testable without OpenCV.

Threshold defaults (sprint 045 Open Question 3), each justified by the
seeded synthetic noise sweep recorded in ticket 004's implementation notes
(camera 144 cm up, tilt 4 deg, 1280x720, fx 1000, k1 -0.25, 46 mm markers;
"clean" = 0.3 px corner noise, worst of 40 seeds):

* ``MAX_RMS_PX = 12.0`` -- a corruption cap, not the accuracy rule (changed
  2026-10-16; it was 1.5 from the synthetic sweep above, which had exact survey
  positions). On a real field the pixel rms is dominated by survey error: at
  120 cm the cameras see about 9 px/cm, so a survey good to 0.5 cm gives 3.5 to
  4.5 px by itself. First live pose solves (46 mm markers, ten hand-surveyed,
  measured lenses): camera 2 3.75 px / 0.50 cm, camera 4 3.50 px / 0.46 cm,
  both otherwise healthy (scale 1.005 / 1.019, spread ~70 deg) and both wrongly
  rejected at 1.5 px. 12 px is about 1.3 cm at 9 px/cm, so the centimetre gate
  below governs accuracy; this one only catches gross corruption (wrong size,
  wrong dictionary, wrong orientation lock), which the scale, rotation-ratio
  and leave-one-out gates mostly catch first.
* ``MAX_RMS_CM = 1.0`` -- the project's existing centre-point threshold;
  clean solves give <= 0.08 cm rms.
* ``SCALE_TOLERANCE = 0.05`` -- clean |median ratio - 1| <= 0.012; markers
  printed 3 % large read 0.037, 10 % large read 0.098.
* ``MIN_ANGULAR_SPREAD_DEG = 10`` -- the four corner markers span ~51 deg;
  one marker spans ~2.5 deg.
* ``MIN_AMBIGUITY_RATIO = 1.3`` -- borrowed from the intrinsics collector.
  Caveat from the sweep: it flags a lone marker only about half the time
  (median 1.5, min 1.0), so the marker-count rule and the layout gate below
  carry the weight; for a spread layout the ratio is a backstop.
* ``MIN_LAYOUT_EXTENT_RATIO = 0.2`` -- smallest/largest singular value of the
  centred marker centres. Found by the sweep, not in the original plan: a
  collinear or near-collinear layout (two diagonal markers, three in a row)
  leaves roll about the baseline unconstrained and the nadir wanders by up to
  30-80 cm at 0.3 px noise without raising the rms or the ambiguity ratio.
  L-shaped 3 markers read 0.48 (nadir within 0.6 cm); near-lines read 0.05-0.11.
* ``MAX_LOO_CM = 2.0`` -- clean worst leave-one-out is <= 0.22 cm; a marker
  displaced d cm reads d cm (1 cm -> 1.06), so 2 cm names real survey errors
  (live reference: 0.75 cm on the corrected main field).
* ``MIN_ROTATION_RATIO = 2.0`` -- the right quarter turn beats the runner-up
  by >= 40x at 0.3 px (>= 12x at 1.0 px); a lone marker reads ~1.

Provisional until confirmed live (ticket 013).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "MAX_RMS_PX", "MAX_RMS_CM", "SCALE_TOLERANCE", "MIN_ANGULAR_SPREAD_DEG",
    "MIN_AMBIGUITY_RATIO", "MIN_LAYOUT_EXTENT_RATIO", "MAX_LOO_CM", "MIN_ROTATION_RATIO",
    "GateMetrics", "angular_spread_deg", "scale_agreement", "scale_ok",
    "ambiguity_ok", "layout_extent_ratio", "per_marker_leave_one_out", "classify",
    "LABEL_FULL", "LABEL_REDUCED", "LABEL_LOW", "LABEL_FAIL",
]

MAX_RMS_PX = 12.0
MAX_RMS_CM = 1.0
SCALE_TOLERANCE = 0.05
MIN_ANGULAR_SPREAD_DEG = 10.0
MIN_AMBIGUITY_RATIO = 1.3
MIN_LAYOUT_EXTENT_RATIO = 0.2
MAX_LOO_CM = 2.0
MIN_ROTATION_RATIO = 2.0

LABEL_FULL = "full"
LABEL_REDUCED = "reduced"
LABEL_LOW = "low"
LABEL_FAIL = "fail"


def angular_spread_deg(rays: np.ndarray) -> float:
    """Largest angle (degrees) between any two of the (N, 3) viewing *rays*.

    Rays need not be unit length. Fewer than two rays -> 0.0.
    """
    r = np.asarray(rays, dtype=np.float64).reshape(-1, 3)
    if len(r) < 2:
        return 0.0
    r = r / np.linalg.norm(r, axis=1, keepdims=True)
    cosines = np.clip(r @ r.T, -1.0, 1.0)
    return float(np.degrees(np.arccos(cosines.min())))


def scale_agreement(
    local_distances: Sequence[float], global_distances: Sequence[float]
) -> Optional[float]:
    """Median of ``local / global`` camera-to-marker distances.

    *local* comes from each marker's own IPPE solve with its printed size;
    *global* from the all-marker pose. ~1.0 means the printed size and the
    surveyed layout agree. ``None`` for fewer than 2 markers.
    """
    local = np.asarray(local_distances, dtype=np.float64)
    glob = np.asarray(global_distances, dtype=np.float64)
    if local.shape != glob.shape:
        raise ValueError("local and global distance lists differ in length")
    if len(local) < 2:
        return None
    return float(np.median(local / glob))


def scale_ok(ratio: Optional[float], tolerance: float = SCALE_TOLERANCE) -> bool:
    """Whether a :func:`scale_agreement` ratio is within *tolerance* of 1 (N/A passes)."""
    return ratio is None or abs(ratio - 1.0) <= tolerance + 1e-9  # float slack at the edge


def ambiguity_ok(ratio: Optional[float], minimum: float = MIN_AMBIGUITY_RATIO) -> bool:
    """Whether the IPPE second-best/best error ratio shows an unambiguous pose."""
    return ratio is None or ratio >= minimum


def layout_extent_ratio(centres_cm: np.ndarray) -> Optional[float]:
    """Smallest / largest singular value of the centred marker *centres*.

    0 = collinear, 1 = isotropic. ``None`` for fewer than 2 markers.
    """
    c = np.asarray(centres_cm, dtype=np.float64).reshape(-1, 2)
    if len(c) < 2:
        return None
    sv = np.linalg.svd(c - c.mean(axis=0), compute_uv=False)
    return float(sv[-1] / sv[0]) if sv[0] > 0 else 0.0


def per_marker_leave_one_out(
    residuals_cm: Mapping[int, float],
) -> Optional[Tuple[int, float]]:
    """``(worst marker id, its cm residual)``; ``None`` below 3 markers.

    *residuals_cm* maps id -> centre error of that marker predicted by the
    pose fit to all the others (``pose.leave_one_out_residuals``).
    """
    if len(residuals_cm) < 3:
        return None
    worst = max(residuals_cm, key=lambda k: residuals_cm[k])
    return int(worst), float(residuals_cm[worst])


@dataclass(frozen=True, kw_only=True)
class GateMetrics:
    """The numbers :func:`classify` decides on; ``None`` means not applicable."""

    rms_px: Optional[float] = None
    rms_cm: Optional[float] = None
    scale_ratio: Optional[float] = None
    angular_spread_deg: Optional[float] = None
    ambiguity_ratio: Optional[float] = None
    layout_extent_ratio: Optional[float] = None
    loo_worst: Optional[Tuple[int, float]] = None
    rotation_ambiguous_ids: Tuple[int, ...] = field(default_factory=tuple)
    """Markers whose quarter-turn winner/runner-up ratio was poor."""


def classify(
    matched: Iterable[int],
    required: Iterable[int],
    helpful: Iterable[int],
    metrics: GateMetrics,
    *,
    has_target: bool = False,
) -> Tuple[str, Tuple[str, ...], Optional[str]]:
    """The degradation ladder: ``(label, notes, failure_reason)``.

    Failure (label ``fail``, reason set): nothing matched; a required marker
    missing; or corrupt data -- rms over threshold (px or cm), scale
    disagreement, or a leave-one-out outlier (the worst marker is named).
    Otherwise: every listed marker matched -> ``full``; two or more ->
    ``reduced`` (with a note naming the absentees); exactly one -> ``low``.
    Weak geometry (angular spread, IPPE ambiguity, collinear layout,
    ambiguous marker rotation) caps the label at ``low`` with a note. All thresholds are
    inclusive on the passing side.

    *has_target*: a calibration target (ChArUco board) contributed corners, so
    zero or one matched markers is not by itself a failure or a ``low`` cap.
    """
    m = sorted(set(int(i) for i in matched))
    req = set(int(i) for i in required)
    hlp = set(int(i) for i in helpful)
    if not m and not has_target:
        return LABEL_FAIL, (), "no calibration markers matched"
    missing_req = sorted(req - set(m))
    if missing_req:
        return LABEL_FAIL, (), f"required marker(s) missing: {missing_req}"

    if metrics.rms_px is not None and metrics.rms_px > MAX_RMS_PX:
        return LABEL_FAIL, (), (
            f"corner reprojection rms {metrics.rms_px:.2f} px exceeds {MAX_RMS_PX} px")
    if metrics.rms_cm is not None and metrics.rms_cm > MAX_RMS_CM:
        return LABEL_FAIL, (), (
            f"corner reprojection rms {metrics.rms_cm:.2f} cm exceeds {MAX_RMS_CM} cm")
    if not scale_ok(metrics.scale_ratio):
        return LABEL_FAIL, (), (
            f"marker size / layout scale disagreement: ratio {metrics.scale_ratio:.3f} "
            f"outside 1 +/- {SCALE_TOLERANCE}")
    if metrics.loo_worst is not None and metrics.loo_worst[1] > MAX_LOO_CM:
        return LABEL_FAIL, (), (
            f"marker {metrics.loo_worst[0]} is {metrics.loo_worst[1]:.2f} cm from where the "
            f"other markers place it (limit {MAX_LOO_CM} cm): mis-surveyed or misplaced")

    notes = []
    listed = req | hlp
    absent = sorted(listed - set(m))
    if len(m) == 1 and not has_target:
        label = LABEL_LOW
        notes.append("pose-only, low confidence: one marker matched")
    elif not absent:
        label = LABEL_FULL
    else:
        label = LABEL_REDUCED
        notes.append(f"reduced: marker(s) not seen: {absent}")

    cap = []
    if metrics.angular_spread_deg is not None and metrics.angular_spread_deg < MIN_ANGULAR_SPREAD_DEG:
        cap.append(f"angular spread {metrics.angular_spread_deg:.1f} deg < {MIN_ANGULAR_SPREAD_DEG}")
    if not ambiguity_ok(metrics.ambiguity_ratio):
        cap.append(f"pose ambiguity ratio {metrics.ambiguity_ratio:.2f} < {MIN_AMBIGUITY_RATIO}")
    if (metrics.layout_extent_ratio is not None
            and metrics.layout_extent_ratio < MIN_LAYOUT_EXTENT_RATIO):
        cap.append(f"markers nearly collinear (extent ratio {metrics.layout_extent_ratio:.2f} "
                   f"< {MIN_LAYOUT_EXTENT_RATIO})")
    if metrics.rotation_ambiguous_ids:
        cap.append(f"marker rotation ambiguous for {sorted(metrics.rotation_ambiguous_ids)}")
    if cap:
        notes.extend(f"confidence capped at low: {c}" for c in cap)
        label = LABEL_LOW
    return label, tuple(notes), None
