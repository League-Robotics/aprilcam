"""``Calibrator``/``Calibration``: establish and hold one camera's
pixel <-> world mapping (ticket 022-003).

Implements ``calibration.interfaces.Calibrator.calibrate`` (UC-04,
UC-15): multi-frame averaged ArUco detection (via ``vision/detector.py``
from sprint 021, reused rather than reimplemented), id matching against
the linked ``config.loader.PlayfieldDefinition``'s surveyed positions,
all-or-nothing acceptance against ``PlayfieldDefinition.
required_aruco_ids()``, homography solve, degenerate-geometry rejection,
and ``CalibrationResult`` construction. The concrete ``Calibration``
dataclass lives here too (next to what constructs it); its
``to_world``/``to_pixel`` bodies delegate to ``calibration/geometry.py``'s
pure math, per this ticket's split between "engine" (this module) and
"point-conversion math" (``geometry.py``).

**Camera frame access is handed in, never owned here** (sprint
architecture's module boundary table: "Outside: camera frame access
(frames are handed in)"). :class:`Calibrator` is constructed with a
``capture_frames`` callable rather than opening cameras itself -- the
daemon (``daemon/service.py``) is the one thing that resolves a camera
and reads from it; this module only ever consumes whatever frames it is
handed. This is also what makes
:class:`~aprilcam.errors.NoPlayfieldLinked`'s "raised before any frame
is captured" contract mechanically true rather than merely documented:
:meth:`Calibrator.calibrate` checks the camera's playfield link via
*its* injected ``get_camera_config`` callable and raises before
``capture_frames`` is ever called, so a camera with no link causes
*zero* calls into whatever backend ``capture_frames`` wraps
(UC-15.S-2's acceptance criterion).

**Archive pointers** (mined for approach, not ported wholesale --
``specification.md`` §6 condemns v1's ``homocal`` CLI and its
upper-left-origin corner table as a foot-gun, and neither is carried
forward here): ``archive/src/aprilcam/calibration/calibration.py``'s
``calibrate_from_playfield_def`` for the overall id-matching ->
homography-solve shape (this module's homography solve goes directly
from detected pixel positions to a playfield definition's *real*
surveyed world coordinates -- never through an intermediate
corner-relative table, which is exactly what keeps the origin-shift
hazard structurally absent here, see ``geometry.py``'s docstring) and
``_reprojection_rms`` for the reprojection-error formula;
``archive/src/aprilcam/calibration/homography.py``'s
``detect_all_tags``/``compute_homography`` for the multi-frame-average
and DLT-solve approach. Not mined: ``_assign_corners_by_position``'s
canonical upper-left-origin ID tables -- superseded entirely by
matching detected ids directly against
``PlayfieldDefinition.aruco_position``.

**Lens distortion (sprint 030 ticket 001; moved to ``distortion.py`` in
sprint 036 ticket 005).** ``Calibration`` gains optional
``camera_matrix``/``dist_coeffs``; ``to_world``/``to_pixel``
undistort/redistort via ``distortion.undistort_point``/``distort_point``
when they are set. ``Calibrator.calibrate`` carries forward whatever
lens model this camera's prior successful calibration already held
(Design Rationale Decision 3 -- intrinsics survive a field move without
being re-estimated) and, when one is known, undistorts every detected
marker pixel *before* the homography solve and the reprojection-error
measurement, both fed the identical undistorted point set -- v1's own
earned lesson (``archive/.../calibration.py:1342-1346``) is that
solving the homography from undistorted points while measuring RMS
against a different (still distorted) set silently reports a healthy
error for a solve that is not. This ticket lands the storage and
solve-order mechanics only; *producing* intrinsics in the first place
(``estimate_intrinsics``) is ticket 002.

**Intrinsics estimation (sprint 030 ticket 002).** ``Calibrator.calibrate``
gains ``estimate_intrinsics: bool = False``. When ``True``, this reuses
the exact detection burst ``calibrate`` already performs, over whatever
ground truth the linked playfield definition surveys (ArUco *and*
AprilTag entries -- :meth:`_detect_averaged` now keeps both families,
not ArUco alone), and runs ``cv2.calibrateCamera`` once, over that
single view's points, via :func:`_estimate_intrinsics`.

*The homography/intrinsics split.* An elevated feature
(``Feature.height_cm > 0``) does not lie on the field plane, and the
homography maps the field plane to pixels -- feeding an elevated point
into the homography solve corrupts it exactly the way unmodelled
parallax does (the error class sprint 029 exists to correct,
reintroduced at calibration time). So: the homography solve and
``reprojection_error_cm`` use **only** the coplanar (``height_cm == 0``)
*ArUco* subset of ``matched_ids`` (never AprilTags -- those were never
part of the homography before this ticket either); intrinsics
estimation uses **every** surveyed-and-detected point, ArUco and
AprilTag alike, with real 3D object points ``(x, y, height_cm)``.

*Two degeneracies, not one.* v1's own archived
``calibration/DESIGN.md:191`` names the first: a single coplanar view
underconstrains a *free* ``cv2.calibrateCamera`` fit (fx, fy, cx, cy,
k1, k2, k3, p1, p2 all free) -- v1's stored ``dist_coeffs [0.42, -2.43,
...]`` is the concrete artefact. This ticket's own empirical
investigation (documented at :func:`_sanity_check_intrinsics`) found a
**second**, independent one specific to a near-exactly-nadir mount even
under the *constrained* flag set below: for a camera looking straight
down at a flat target, a surveyed point's image position depends on
focal length (``fx``) and camera-to-plane distance (the solved
extrinsic ``tvec[2]``) only through their *ratio* -- ``x_pixel =
fx * X / Z + cx`` for fixed, known ``X`` -- so ``fx`` and ``Z`` trade off
along an exactly flat ridge with **no** distortion coefficient
involved at all. Synthetic sweeps (mined for this ticket, not shipped as
a script) showed ``fx`` landing anywhere from ~20x to ~700x the
camera's true focal length at 0 deg of mount tilt, converging to within
a few percent of truth once genuine tilt (>= ~5 deg) or genuine
elevated-feature z-spread (>= ~5 cm, even at *exactly* 0 deg tilt) gives
the solve real 3D structure to anchor ``fx``/``Z`` against. Neither
degeneracy is fixable by choosing better flags -- both are structural
properties of what the single view actually contains -- so both are
caught by :func:`_sanity_check_intrinsics` rejecting the *fitted
coefficients themselves* (never a value judgement made before the fit
runs) rather than assumed away.

*Mode selection is graded, decided from the linked playfield's own
surveyed features, never a per-call argument*: see
:data:`MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE`/
:data:`MIN_ELEVATED_Z_SPREAD_CM` for the concrete threshold and the
evidence behind it (Open Question 6). ``k2`` stays fixed in the default
coplanar-constrained model (Open Question 5) -- held-out validation
(:func:`_held_out_split`) run during this ticket's own investigation
showed freeing it made held-out residual *worse* on average (roughly
4-6x, across 200 synthetic noise trials at a well-conditioned 8 deg
mount tilt) and was only measurably better in ~1 trial in 5: with a
single view and this few points, ``k2`` chases noise rather than
capturing real signal.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2 as cv
import numpy as np

from aprilcam.calibration import distortion, geometry
from aprilcam.calibration.interfaces import Calibration as _CalibrationBase
from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.calibration.interfaces import Calibrator as _CalibratorBase
from aprilcam.calibration.interfaces import MarkerObservation
from aprilcam.calibration.lens import (
    LENS_SOURCE_ESTIMATED,
    LENS_SOURCE_LEGACY,
    LENS_SOURCE_MEASURED,
    LENS_SOURCE_NONE,
    LensModel,
)
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.interfaces import PlayfieldDefinition as PlayfieldDefinitionIface
from aprilcam.config.loader import PlayfieldDefinition as _ConcretePlayfieldDefinition
from aprilcam.errors import NoPlayfieldLinked
from aprilcam.types import PixelPoint, TagFamily, WorldPoint
from aprilcam.vision.detector import DetectorConfig, TagDetector
from aprilcam.vision.pipeline import PipelineConfig, run_pipeline

__all__ = ["Calibration", "Calibrator"]

logger = logging.getLogger(__name__)

#: A fixed-length 3-tuple of floats -- ``Calibration.homography``'s row
#: type. Named so it is declared once rather than repeated at every
#: call site that builds one.
_Row3 = Tuple[float, float, float]


def _row3(values: Sequence[Any]) -> _Row3:
    """*values* (any 3-element sequence) as an explicit ``_Row3`` --
    lets mypy see a fixed-length tuple rather than the
    variable-length ``tuple[float, ...]`` a generic
    ``tuple(float(v) for v in values)`` produces, without an
    ``# type: ignore``."""
    a, b, c = values
    return (float(a), float(b), float(c))

#: v1's ``calibrate_from_playfield_def(num_frames=30)`` default -- an
#: earned constant (real-camera averaging stability), mined as a
#: starting point rather than re-derived. Callers that want a different
#: count control it entirely through what their ``capture_frames``
#: callable returns; this class never dictates a count itself (see the
#: module/class docstrings).
DEFAULT_CALIBRATION_FRAMES = 30

#: Reprojection-error acceptance ceiling, in cm (ticket 023-006, UC-04's
#: open question -- "completeness is necessary but may not be
#: sufficient" -- now resolved). A solve where every required ArUco id
#: was found (completeness holds) but whose RMS reprojection error
#: exceeds this is still rejected: complete is not the same as good.
#: **Confirmed 1.0 cm by the stakeholder** (sprint.md Decisions, Design
#: Rationale Decision 4): ~5x headroom over the observed-good 0.179 cm
#: live sprint-022 solve against the real ``main-playfield``, and 0.7%
#: of that field's 134.3 cm width -- tight enough to catch a genuinely
#: degenerate solve, loose enough not to fail an ordinary one. Named and
#: overridable per :class:`Calibrator` instance
#: (``max_reprojection_error_cm``) rather than hardcoded, since the
#: exact number is a domain-tuning call, not a structural one -- trivial
#: to retune once more field data exists.
MAX_REPROJECTION_ERROR_CM = 1.0

#: Staleness drift tolerance, in cm (ticket 023-007, sprint.md Decisions
#: #2). **Confirmed 2.0 cm by the stakeholder**: ~11x the observed
#: ~0.18 cm noise floor from the real sprint-022 solve, so ordinary
#: detection jitter should not false-positive a still camera into
#: reporting stale. Named and overridable
#: (``daemon.detection.DetectionEngine.__init__``'s
#: ``drift_tolerance_cm``), mirroring :data:`MAX_REPROJECTION_ERROR_CM`'s
#: "domain-tuning call, not a structural one" framing above.
DRIFT_TOLERANCE_CM = 2.0

#: How often the opportunistic drift check may run per camera, in
#: seconds (ticket 023-007, sprint.md Decision 5). **Confirmed 10 s.**
#: This is a rate limit on repeated checks *while a camera is under
#: active use* -- an unqueried camera already does zero extra work
#: regardless of this number, since nothing calls
#: ``daemon.detection.DetectionEngine.run_burst`` for it at all (no
#: independent poll loop exists; see that module's docstring).
DRIFT_CHECK_INTERVAL_S = 10.0

#: Fewer than this many of a calibration's own matched static ArUco
#: markers newly detected means "cannot verify," never "confirmed no
#: drift" -- see :func:`compute_drift_cm`. Three is the same floor
#: :func:`_is_collinear` uses for "can this even describe a 2D spread"
#: (fewer points can trivially agree by chance), applied here to "is
#: this reading trustworthy enough to accuse the calibration of having
#: moved," not to a homography solve.
MIN_DRIFT_CHECK_MARKERS = 3

#: Below this ratio of the second-to-largest singular value of a
#: (centred) correspondence point set, the points are treated as
#: collinear -- see :func:`_is_collinear`.
_COLLINEARITY_RATIO = 1e-3

#: A homography whose determinant magnitude falls below this is treated
#: as singular/degenerate rather than trusted numerically.
_MIN_HOMOGRAPHY_DETERMINANT = 1e-12

# ---------------------------------------------------------------------------
# Intrinsics estimation (sprint 030 ticket 002) -- see the module
# docstring's "Intrinsics estimation" section for the two degeneracies
# these constants exist to police, and for the empirical evidence behind
# each specific number.
# ---------------------------------------------------------------------------

#: The ``n_pts >= 6`` floor (sprint.md Design Rationale, Decision 2):
#: fewer than this many ArUco+AprilTag points *surveyed by the playfield
#: definition itself* (never points merely detected in-frame that the
#: definition doesn't survey -- archive ``DESIGN.md``'s own warning)
#: means ``estimate_intrinsics=True`` fails explicitly rather than
#: attempting an even-more-underdetermined fit. v1's own earned floor
#: (``calibrate_single``), mined as a starting point.
MIN_SURVEYED_INTRINSICS_POINTS = 6

#: Open Question 6's answer: the minimum count of surveyed-and-detected
#: *elevated* (``height_cm > 0``) points below which the coplanar-
#: constrained model is kept even when some elevation is present, never
#: freed on the strength of a single elevated tag with a token height.
#: Three, not one or two: the same "enough points to trust a spread
#: claim" floor :data:`MIN_DRIFT_CHECK_MARKERS`/:func:`_is_collinear`
#: already use elsewhere in this module for a 2D spread, extended here
#: to a 3D one -- a single or a pair of elevated points cannot even be
#: checked for mutual consistency.
MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE = 3

#: Open Question 6's other half: the minimum z-spread (max height_cm -
#: min height_cm, across every point -- coplanar and elevated alike --
#: fed to intrinsics estimation) below which the coplanar-constrained
#: model is kept regardless of how many elevated points exist. Chosen
#: with real margin above this ticket's own empirical finding: synthetic
#: trials mixing 3 elevated points into main-playfield's real 11-point
#: coplanar layout, at a mathematically exact 0 deg mount tilt (the
#: single hardest case -- see the module docstring), converged to
#: within a few percent of the true focal length/height at z-spreads as
#: low as 3 cm; 5 cm keeps a full extra centimetre of headroom above
#: that observed-good boundary, comfortably above any realistic
#: surveying/detection noise floor on this project (sub-cm).
MIN_ELEVATED_Z_SPREAD_CM = 5.0

#: :func:`_estimate_intrinsics`'s fixed decision for Open Question 5:
#: **not** freed by default. Held-out validation (this ticket's own
#: investigation, not re-run at import time) fit the coplanar-
#: constrained model at a well-conditioned 8 deg mount tilt across 200
#: synthetic noise trials (4 noise levels x 50 seeds): freeing k2 raised
#: mean held-out reprojection RMS by roughly 4-6x and was only
#: measurably *better* than k1-only in ~18-28% of trials (worse in the
#: rest) -- with a single view and this few points, k2 chases noise
#: rather than capturing real signal. Not a per-instance/per-camera
#: setting: the evidence is about this project's own point-count regime
#: (main-playfield's 11 surveyed points, an 8/3ish fit/held-out split),
#: not a property any one camera could override its way out of.
_FREE_K2_IN_COPLANAR_DEFAULT = False

#: :func:`_sanity_check_intrinsics`'s bound on ``|k1|`` (and, on the rare
#: elevated-mode call where k2 is genuinely free, ``|k2|`` too).
#: Generous relative to a real consumer/webcam lens (typically
#: ``|k1| < 0.5`` -- this project's own falsifiable-check evidence,
#: sprint.md's Problem section, describes a "roughly 2%" measured
#: distortion effect, i.e. a modest lens, not a fisheye), while sitting
#: comfortably below the wildly-implausible values this ticket's
#: synthetic degenerate trials produced (k1 magnitudes from single to
#: multiple thousands) and below v1's own stored k1 of 0.42 doubled
#: over for margin.
_MAX_ABS_DISTORTION_COEFF = 1.5

#: :func:`_sanity_check_intrinsics`'s bound on ``|k2/k1|`` when k2 is
#: free. v1's stored ``dist_coeffs [0.42, -2.43, ...]`` -- this ticket's
#: own concrete "what the bound exists to reject" example -- has a ratio
#: of ``abs(-2.43 / 0.42) ~= 5.79``; 3.0 rejects that with real margin
#: while still tolerating a secondary radial term genuinely up to 3x the
#: primary one.
_MAX_K2_OVER_K1_RATIO = 3.0

#: :func:`_sanity_check_intrinsics`'s bound on the fitted focal length
#: (``camera_matrix[0][0]``) relative to the captured frame's width --
#: the direct, most robust catch for this ticket's own newly-documented
#: fx/depth degeneracy (module docstring): ordinary consumer/webcam
#: lenses span roughly 0.3x (~100 deg+ wide) to 3.0x (narrow/telephoto)
#: image width; a fit landing outside that band is not a plausible lens,
#: it is the degenerate ridge. This ticket's synthetic near-nadir trials
#: produced ratios from ~3x up to several hundred x -- comfortably
#: outside this band -- while every well-conditioned trial (>= ~5 deg
#: tilt, or elevated-mode z-spread present) landed inside it.
_MIN_FX_TO_WIDTH_RATIO = 0.3
_MAX_FX_TO_WIDTH_RATIO = 3.0

CameraConfigLookup = Callable[[str], Optional[CameraConfig]]
PlayfieldLookup = Callable[[str], Optional[PlayfieldDefinitionIface]]
FrameSource = Callable[[str], Sequence[np.ndarray]]
DetectorFactory = Callable[[], TagDetector]
CalibrationLookup = Callable[[str], Optional["Calibration"]]

#: ``(camera_slug, (width, height)) -> LensModel | None`` -- the per-camera
#: lens store's size-matched lookup (sprint 044); see ``calibration.lens``.
LensLookup = Callable[[str, Tuple[int, int]], Optional[LensModel]]

#: ``(camera_slug, (width, height)) -> str | None`` -- why the lens store
#: would ignore its model at that frame size (resolution mismatch), or
#: ``None``; surfaces as ``CalibrationResult.lens_warning`` (sprint 044).
LensWarningLookup = Callable[[str, Tuple[int, int]], Optional[str]]


def _default_detector_factory() -> TagDetector:
    # Subpixel corner refinement is pinned explicitly (it is also the
    # DetectorConfig default): the pose solve (sprint 045) consumes marker
    # corners, whose accuracy is what refinement buys, so a future change to
    # the detector's default must not silently degrade calibration bursts.
    #
    # Inverted-marker detection is turned OFF for the same reason: with it
    # on, OpenCV reports corners ~0.4 cell inward of the true marker corners
    # and skips refinement (see DetectorConfig.detect_inverted). Calibration
    # markers are ordinary dark-on-light prints.
    return TagDetector(DetectorConfig(corner_refine="subpix", detect_inverted=False))


#: Sprint 045 persisted pose-provenance fields -> their JSON converters.
_POSE_PERSISTED_FIELDS: Dict[str, Callable[[Any], Any]] = {
    "method": str,
    "confidence": str,
    "camera_height_cm": float,
    "camera_nadir_x_cm": float,
    "camera_nadir_y_cm": float,
    "camera_tilt_deg": float,
    "reprojection_error_px": float,
    "corner_count": int,
    "scale_agreement": float,
}


@dataclass(frozen=True, kw_only=True)
class Calibration(_CalibrationBase):
    """Concrete ``calibration.interfaces.Calibration``.

    ``homography`` is the solved pixel->world 3x3 matrix, stored as a
    plain nested tuple of floats (row-major) rather than a
    ``numpy.ndarray``: a numpy field would make this frozen dataclass's
    auto-generated ``__eq__`` raise (numpy's ``==`` returns an
    elementwise array, not a bool -- the same reason
    ``vision.detector.Detection`` suppresses ``eq`` for its own numpy
    field), and a tuple round-trips through JSON (:meth:`to_dict`/
    :meth:`from_dict`) with no precision loss and no extra conversion
    step. :meth:`to_world`/:meth:`to_pixel` convert back to an
    ``ndarray`` internally, once per call, before delegating to
    ``calibration/geometry.py``'s pure math.

    ``camera_matrix``/``dist_coeffs`` (sprint 030 ticket 001, Design
    Rationale Decision 3) are this camera's optional lens model --
    intrinsic matrix and distortion coefficients, the same shapes
    ``cv2.calibrateCamera`` produces. Stored as plain tuples for the
    identical reason ``homography`` is: JSON round-trip with no numpy
    ``__eq__`` hazard. Both default to ``None`` -- "no lens model
    known," today's exact behaviour and every ``calibration.json``
    written before this ticket -- and are carried forward unchanged
    across an ordinary recalibration rather than re-estimated
    (:meth:`Calibrator.calibrate`); ticket 002 is what lets an operator
    populate them in the first place via ``estimate_intrinsics``. They
    live here, not on ``config.interfaces.CameraConfig``, precisely so
    :meth:`to_world`/:meth:`to_pixel` stay pure methods over this
    dataclass's own fields -- see sprint.md's Design Rationale Decision
    3 for the alternative considered and rejected.
    """

    homography: Tuple[_Row3, _Row3, _Row3]
    matched_ids: Tuple[int, ...]
    """ArUco ids the homography was solved from -- provenance, echoing
    the ``CalibrationResult.matched_ids`` this calibration succeeded
    with."""
    reprojection_error_cm: Optional[float] = None
    """RMS distance (cm) between each matched marker's surveyed world
    position and where the solved homography reprojects its detected
    pixel position. A stored :class:`Calibration` always has this at or
    below whatever :class:`Calibrator` instance produced it was
    configured with (``MAX_REPROJECTION_ERROR_CM`` by default, ticket
    023-006) -- a solve above the threshold never reaches this
    dataclass at all; see :meth:`Calibrator.calibrate`."""
    camera_matrix: Optional[Tuple[_Row3, _Row3, _Row3]] = None
    """This camera's 3x3 intrinsic matrix (``fx``, ``fy``, ``cx``,
    ``cy``), if known. ``None`` means "no lens model" -- see the class
    docstring."""
    dist_coeffs: Optional[Tuple[float, ...]] = None
    """This camera's distortion coefficients, if known -- ``(k1, k2,
    p1, p2, k3)`` for the standard 5-coefficient model
    ``cv2.calibrateCamera`` fits by default (ticket 002), though any
    length OpenCV's distortion model accepts round-trips here
    unchanged. ``None`` means "no lens model" -- see the class
    docstring."""
    lens_source: Optional[str] = None
    """Provenance of the embedded lens: ``'measured'`` (from the
    per-camera lens store), ``'estimated'`` (``estimate_intrinsics``) or
    ``'legacy'`` (carried from a pre-sprint-044 file). ``None`` when no
    lens is embedded. :meth:`from_dict` infers ``'legacy'`` for an old
    file with a matrix and no ``lens_source``."""

    def __post_init__(self) -> None:
        # A lens with no recorded provenance can only be pre-sprint-044
        # data: normalize it to 'legacy' so construction, to_dict and
        # from_dict all agree (and round-trip equal).
        if self.lens_source is None and self.camera_matrix is not None:
            object.__setattr__(self, "lens_source", LENS_SOURCE_LEGACY)

    def to_world(self, pixel: PixelPoint) -> WorldPoint:
        if self._has_intrinsics():
            pixel = distortion.undistort_point(
                pixel, self._camera_matrix_array(), self._dist_coeffs_array()
            )
        return geometry.pixel_to_world(self._matrix(), pixel)

    def to_pixel(self, world: WorldPoint) -> PixelPoint:
        pixel = geometry.world_to_pixel(self._matrix(), world)
        if self._has_intrinsics():
            pixel = distortion.distort_point(
                pixel, self._camera_matrix_array(), self._dist_coeffs_array()
            )
        return pixel

    def _matrix(self) -> np.ndarray:
        return np.array(self.homography, dtype=np.float64)

    def _has_intrinsics(self) -> bool:
        """Whether both halves of this camera's lens model are known --
        ``camera_matrix``/``dist_coeffs`` are set (or cleared) together
        by every producer in this codebase (:meth:`Calibrator.calibrate`,
        :meth:`from_dict`), but :meth:`to_world`/:meth:`to_pixel` check
        both rather than assume that invariant, so a hand-constructed
        :class:`Calibration` with only one set behaves as "no lens
        model" (today's behaviour) instead of raising."""
        return self.camera_matrix is not None and self.dist_coeffs is not None

    def _camera_matrix_array(self) -> np.ndarray:
        assert self.camera_matrix is not None  # guarded by _has_intrinsics
        return np.array(self.camera_matrix, dtype=np.float64)

    def _dist_coeffs_array(self) -> np.ndarray:
        assert self.dist_coeffs is not None  # guarded by _has_intrinsics
        return np.array(self.dist_coeffs, dtype=np.float64)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for ``calibration.json`` (daemon/service.py owns
        the atomic file write; this is just the shape).

        ``camera_matrix``/``dist_coeffs`` are omitted entirely when
        ``None`` -- never written as a JSON ``null`` placeholder
        (mirrors ``config.interfaces.CameraConfig.to_dict``'s identical
        treatment of its own optional ``camera_position``) -- so a
        ``calibration.json`` written by a camera with no lens model is
        byte-for-byte identical to what this method produced before
        this ticket."""
        data: Dict[str, Any] = {
            "stale": self.stale,
            "calibrated_at": self.calibrated_at,
            "playfield": self.playfield,
            "homography": [list(row) for row in self.homography],
            "matched_ids": list(self.matched_ids),
            "reprojection_error_cm": self.reprojection_error_cm,
        }
        if self.camera_matrix is not None:
            data["camera_matrix"] = [list(row) for row in self.camera_matrix]
        if self.dist_coeffs is not None:
            data["dist_coeffs"] = list(self.dist_coeffs)
        if self.lens_source is not None:
            data["lens_source"] = self.lens_source
        # Sprint 045 pose provenance: omitted when absent so a pre-045 file
        # re-serialises byte-identically.
        for name in _POSE_PERSISTED_FIELDS:
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Calibration":
        """The inverse of :meth:`to_dict` -- ``daemon/service.py``'s
        startup reload path. Raises ``KeyError``/``ValueError``/
        ``TypeError`` on a malformed record; the daemon treats a corrupt
        ``calibration.json`` as "no calibration held" rather than
        failing to start (mirrors ``camera.registry.CameraRegistry``'s
        "a corrupt file must never crash the daemon" stance).

        ``camera_matrix``/``dist_coeffs`` default to ``None`` when
        absent -- true for every ``calibration.json`` written before
        this ticket, which loads here unchanged (backward compatibility,
        sprint.md's Migration Concerns: "no forced conversion, no schema
        version bump")."""
        homography_raw = data["homography"]
        camera_matrix_raw = data.get("camera_matrix")
        dist_coeffs_raw = data.get("dist_coeffs")
        lens_source_raw = data.get("lens_source")
        if lens_source_raw is None and camera_matrix_raw is not None:
            lens_source_raw = LENS_SOURCE_LEGACY
        pose_kwargs: Dict[str, Any] = {}
        for name, conv in _POSE_PERSISTED_FIELDS.items():
            if data.get(name) is not None:
                pose_kwargs[name] = conv(data[name])
        return cls(
            **pose_kwargs,
            lens_source=str(lens_source_raw) if lens_source_raw is not None else None,
            stale=bool(data.get("stale", False)),
            calibrated_at=float(data["calibrated_at"]),
            playfield=str(data["playfield"]),
            homography=(
                _row3(homography_raw[0]),
                _row3(homography_raw[1]),
                _row3(homography_raw[2]),
            ),
            matched_ids=tuple(int(i) for i in data.get("matched_ids", ())),
            reprojection_error_cm=(
                float(data["reprojection_error_cm"])
                if data.get("reprojection_error_cm") is not None
                else None
            ),
            camera_matrix=(
                (
                    _row3(camera_matrix_raw[0]),
                    _row3(camera_matrix_raw[1]),
                    _row3(camera_matrix_raw[2]),
                )
                if camera_matrix_raw is not None
                else None
            ),
            dist_coeffs=(
                tuple(float(c) for c in dist_coeffs_raw) if dist_coeffs_raw is not None else None
            ),
        )


def _is_collinear(points: np.ndarray, *, ratio_threshold: float = _COLLINEARITY_RATIO) -> bool:
    """True if every row of *points* (an ``(N, 2)`` array) lies
    approximately on one line -- independent of which two axes that line
    runs along.

    Uses the SVD of the *centred* point matrix: two points, or points
    exactly on a line, produce a zero (or near-zero) second singular
    value, since the point cloud has no meaningful extent in the
    direction perpendicular to the line. A proper 2D spread of points
    (the real ``main-playfield``'s 10 surveyed ArUco positions, for
    instance) has two comparably-sized singular values.
    """
    if len(points) < 3:
        return True  # fewer than 3 points can never span a plane
    centered = points - points.mean(axis=0)
    singular_values = np.linalg.svd(centered, compute_uv=False)
    if singular_values[0] == 0.0:
        return True
    return bool(singular_values[1] / singular_values[0] < ratio_threshold)


def _degeneracy_reason(pixel_pts: np.ndarray, world_pts: np.ndarray) -> Optional[str]:
    """``None`` if *pixel_pts*/*world_pts* can support a well-posed
    homography solve; otherwise a human-readable reason naming which
    side is degenerate, for ``CalibrationResult.reason`` (UC-04's "a
    complete-but-degenerate solve ... must name its problem").

    Checked **before** calling into OpenCV, deliberately:
    ``cv2.findHomography`` does not reliably refuse a collinear
    correspondence set -- for points confined to (or extremely close to)
    one line, the perspective mapping is unconstrained in the direction
    perpendicular to that line, so OpenCV can return a numerically
    well-formed matrix that is nonetheless not a trustworthy 2D
    homography. That is exactly "a valid-but-bad homography," the
    failure mode UC-04's own text names -- silently accepting it would
    be worse than refusing up front.
    """
    if len(pixel_pts) < 4:
        return f"only {len(pixel_pts)} correspondence point(s); a homography needs at least 4"
    if _is_collinear(world_pts):
        return "surveyed marker positions are collinear; cannot solve a homography from them"
    if _is_collinear(pixel_pts):
        return "detected marker pixel positions are collinear; cannot solve a homography from them"
    return None


def _solve_homography(pixel_pts: np.ndarray, world_pts: np.ndarray) -> Optional[np.ndarray]:
    """Pixel->world homography via OpenCV's DLT solver.

    Regular least squares (``method=0``), not RANSAC: every
    correspondence here comes from an id-matched detection against
    surveyed ground truth (``Calibrator._detect_averaged`` +
    ``PlayfieldDefinition.aruco_position``), not error-prone point
    matching -- there are no outliers to robustly reject, only a
    well- or ill-conditioned system to solve exactly.

    Returns ``None`` (never raises) for a failed solve or a
    post-hoc-detected singular result, so callers can turn it into a
    named ``CalibrationResult.reason`` rather than an uncaught
    exception.
    """
    homography, _mask = cv.findHomography(
        pixel_pts.astype(np.float32), world_pts.astype(np.float32), method=0
    )
    if homography is None:
        return None
    if not np.all(np.isfinite(homography)):
        return None
    if abs(np.linalg.det(homography)) < _MIN_HOMOGRAPHY_DETERMINANT:
        return None
    return homography


def _reprojection_error_cm(
    homography: np.ndarray, pixel_pts: np.ndarray, world_pts: np.ndarray
) -> float:
    """RMS distance (cm) between each surveyed world position and where
    *homography* reprojects the matching detected pixel position.
    Mirrors ``archive/src/aprilcam/calibration/calibration.py``'s
    ``_reprojection_rms`` formula, via ``geometry.apply_homography``
    rather than duplicating the homogeneous-divide arithmetic."""
    errors = []
    for (px, py), (wx, wy) in zip(pixel_pts, world_pts):
        x, y = geometry.apply_homography(homography, float(px), float(py))
        errors.append(math.hypot(x - wx, y - wy))
    return float(np.sqrt(np.mean(np.square(errors))))


def _matrix_to_tuple(matrix: np.ndarray) -> Tuple[_Row3, _Row3, _Row3]:
    return (_row3(matrix[0]), _row3(matrix[1]), _row3(matrix[2]))


def compute_drift_cm(
    calibration: Calibration,
    definition: _ConcretePlayfieldDefinition,
    detected: Dict[int, PixelPoint],
) -> Optional[float]:
    """How far *calibration*'s own matched static markers appear to have
    moved, in cm (ticket 023-007's opportunistic drift check, sprint.md
    Decision 5).

    Reuses exactly the reprojection-error idea :meth:`Calibrator.calibrate`
    already applies to *accept* a fresh solve
    (:func:`_reprojection_error_cm`), pointed instead at a *later*
    frame's detections: if the camera has not moved, re-detecting each of
    *calibration*'s ``matched_ids`` and mapping the detected pixel through
    the still-valid ``calibration.homography`` (:meth:`Calibration.to_world`)
    must reproduce *definition*'s surveyed ground truth
    (``aruco_position``), up to ordinary detection noise. A real bump
    throws every matched marker's reprojection off by roughly how far the
    camera moved -- this is "compare to their stored positions" (the
    ticket's own phrasing) with ``PlayfieldDefinition.aruco_position`` as
    the stored positions, so :class:`Calibration` itself needs no new
    field to carry them.

    *detected* is one frame's ArUco hits, keyed by id -- callers pass in
    whatever a detection pass already produced (``daemon.detection.
    DetectionEngine`` reuses the same ``detector.detect()`` call
    ``run_burst`` makes for tag tracking; no independent detection pass
    exists, preserving the idle-efficiency budget by construction).

    Returns ``None`` -- never ``0.0`` -- when fewer than
    :data:`MIN_DRIFT_CHECK_MARKERS` of *calibration*'s matched markers
    are present in *detected*: darkness, an occluded lens, or a covered
    field (all of which this project's stakeholder hits in practice) must
    read as "cannot verify," never as either a spurious stale flip
    (already true here, since nothing computes without real detections)
    or, just as important, a *false confirmation* that nothing has moved
    -- both would be wrong for the same reason UC-11.S-3 exists: an
    advisory a caller cannot trust is worse than no advisory. Any id
    *detected* carries that :meth:`PlayfieldDefinition.aruco_position`
    no longer recognises (the playfield definition changed identity
    between calibrating and now) is silently excluded from
    *visible* rather than raising -- the same "cannot verify" stance
    applies if that drops the count below the minimum.

    The **maximum** single-marker error, not an RMS across every visible
    marker, decides the outcome: a genuine camera bump throws off every
    matched marker roughly uniformly, so either statistic would catch
    it, but a max is the more conservative (never-diluted-by-averaging)
    choice, and does not require deciding how many markers a particular
    burst happened to catch in frame.
    """
    visible = [tag_id for tag_id in calibration.matched_ids if tag_id in detected]
    errors: List[float] = []
    for tag_id in visible:
        try:
            surveyed = definition.aruco_position(tag_id)
        except KeyError:
            continue
        world = calibration.to_world(detected[tag_id])
        errors.append(math.hypot(world.x - surveyed.x, world.y - surveyed.y))
    if len(errors) < MIN_DRIFT_CHECK_MARKERS:
        return None
    return max(errors)


# ---------------------------------------------------------------------------
# Intrinsics estimation (sprint 030 ticket 002) -- see the module
# docstring's "Intrinsics estimation" section for the design rationale and
# the empirical evidence behind every threshold/bound below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IntrinsicsFit:
    """The outcome of one :func:`_estimate_intrinsics` call -- explicitly
    accepted or rejected, mirroring :class:`CalibrationResult`'s own
    "no partial success" shape at a smaller scale. ``camera_matrix``/
    ``dist_coeffs`` are ``None`` on a rejection -- never a value
    :meth:`Calibrator.calibrate` could mistake for something to store.
    """

    accepted: bool
    reason: Optional[str]
    camera_matrix: Optional[Tuple[_Row3, _Row3, _Row3]]
    dist_coeffs: Optional[Tuple[float, ...]]
    mode: str
    """``"coplanar-constrained"`` or ``"elevated"`` -- which model this
    call ran, regardless of whether the result was ultimately accepted."""
    elevated_feature_count: int
    """How many of the points handed to this call had ``height_cm > 0``
    -- reported even on rejection, so a caller can still say how many
    elevated features *contributed to the attempt*."""
    held_out_rms_px: Optional[float]
    """The held-out reprojection residual, in pixels (this ticket's own
    "held-out validation... reported, not silently discarded"
    acceptance criterion) -- informational, not itself a pass/fail gate
    (:func:`_sanity_check_intrinsics` is the gate); ``None`` only if the
    point set was too small to hold anything out at all."""


def _held_out_split(n: int) -> Tuple[List[int], List[int]]:
    """Split *n* points' indices into a fit subset and a held-out
    subset, deterministically (no shuffling -- reproducible tests) and
    **evenly spaced**, not a contiguous prefix/suffix block.

    A block split was this function's first draft, on the assumption
    that this project's playfield entries have no ordering that would
    bias it -- wrong, caught by this ticket's own test suite:
    :meth:`Calibrator.calibrate` builds its point list ArUco ids first,
    then AprilTag ids, and an elevated-mode caller's *elevated* features
    are conventionally the ones appended last (a playfield's original
    coplanar corners surveyed first, elevated ground truth added later).
    A trailing-block holdout could then hold out *every* elevated point
    at once, leaving the fit subset with none of the very 3D structure
    elevated mode exists to use. Evenly spaced indices
    (``numpy.linspace``) make that systematic exclusion structurally
    impossible regardless of input order, at the cost of no longer being
    a legible "first N / last M" split -- an acceptable trade since nothing
    about this split needs to be human-readable, only reproducible.

    Roughly a 70/30 split, floored so the fit subset never drops below 4
    (the minimum a homography-shaped correspondence problem needs, and
    in practice always well above what ``cv2.calibrateCamera`` needs
    once ``CALIB_FIX_PRINCIPAL_POINT``/``CALIB_FIX_ASPECT_RATIO`` cut the
    coplanar-constrained model down to effectively one or two free
    intrinsic parameters) and the held-out subset never drops below 2.
    At the ``MIN_SURVEYED_INTRINSICS_POINTS`` floor (n=6) this yields
    exactly 4 fit / 2 held-out; at ``main-playfield``'s real 11 points,
    8 fit / 3 held-out -- both exercised directly by this ticket's own
    empirical investigation (module docstring).
    """
    holdout_n = max(2, round(n * 0.3))
    holdout_n = min(holdout_n, max(0, n - 4))
    if holdout_n <= 0:
        return list(range(n)), []
    holdout_idx = sorted({int(round(p)) for p in np.linspace(0, n - 1, num=holdout_n)})
    # Rounding can collide for a small n/holdout_n ratio, leaving fewer
    # than holdout_n distinct indices -- backfill from whatever is left,
    # preserving determinism (always the same order: index 0 upward).
    remaining = [i for i in range(n) if i not in holdout_idx]
    while len(holdout_idx) < holdout_n and remaining:
        holdout_idx.append(remaining.pop(0))
    holdout_idx = sorted(holdout_idx)
    fit_idx = [i for i in range(n) if i not in holdout_idx]
    return fit_idx, holdout_idx


def _initial_camera_matrix_guess(image_size: Tuple[int, int]) -> np.ndarray:
    """A generic starting intrinsic matrix for the elevated-mode fit.

    ``cv2.calibrateCamera`` refuses a non-planar point set with no
    initial ``cameraMatrix`` at all ("For non-planar calibration rigs
    the initial intrinsic matrix must be specified") -- confirmed
    directly against this project's own pinned OpenCV version during
    this ticket's investigation, unlike the coplanar-constrained path
    below, which OpenCV happily self-initializes (auto-centring the
    principal point and deriving a starting focal length via its own
    internal homography-based heuristic) when ``cameraMatrix=None``.
    This guess is only ever a Levenberg-Marquardt *starting point* --
    every entry remains free to move (``CALIB_USE_INTRINSIC_GUESS`` seeds
    the optimizer here, it does not fix anything, unlike the coplanar
    path's ``CALIB_FIX_*`` flags) -- so its exact values are not
    load-bearing precision, just a plausible-order-of-magnitude seed:
    focal length at the larger image dimension (a generic normal-FOV
    guess), principal point at the image centre.
    """
    width, height = image_size
    focal_guess = float(max(width, height))
    return np.array(
        [[focal_guess, 0.0, width / 2.0], [0.0, focal_guess, height / 2.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _sanity_check_intrinsics(
    camera_matrix: np.ndarray, dist_coeffs: np.ndarray, image_size: Tuple[int, int], *, k2_free: bool
) -> Optional[str]:
    """``None`` if *camera_matrix*/*dist_coeffs* pass this ticket's
    sanity bound; otherwise a human-readable reason naming which check
    failed, for :class:`CalibrationResult.reason`.

    Two independent degeneracies, two independent checks (module
    docstring's "Two degeneracies, not one" section) -- **either** alone
    is grounds for rejection:

    1. **Implausible focal length** (``fx`` far outside an ordinary
       consumer/webcam lens's range relative to the image width) -- the
       direct signature of this ticket's own newly-documented fx/depth
       ambiguity for a near-exactly-nadir single coplanar view. This is
       checked *first* and independently of the distortion coefficients,
       since that degeneracy can occur with an otherwise-small ``k1``
       (module docstring's empirical evidence).
    2. **Implausible distortion coefficients** -- v1's own concrete
       failure mode (stored ``dist_coeffs [0.42, -2.43, ...]``): an
       absolute bound on ``k1``/``k2`` individually, plus (only when
       ``k2`` was actually freed) a bound on their ratio, matching v1's
       own ``k2`` being ~5.79x its ``k1`` in magnitude.
    """
    fx = float(camera_matrix[0][0])
    width = image_size[0]
    ratio = fx / width if width else float("inf")
    if not (_MIN_FX_TO_WIDTH_RATIO <= ratio <= _MAX_FX_TO_WIDTH_RATIO):
        return (
            f"implausible focal length: fx={fx:.1f}px is {ratio:.2f}x the {width}px image "
            f"width (expected roughly {_MIN_FX_TO_WIDTH_RATIO}x-{_MAX_FX_TO_WIDTH_RATIO}x for "
            "an ordinary lens) -- likely an under-constrained single coplanar view "
            "(see calibration.calibration's module docstring's fx/depth degeneracy note)"
        )

    k1 = float(dist_coeffs[0])
    if abs(k1) > _MAX_ABS_DISTORTION_COEFF:
        return (
            f"implausible k1={k1:.4f} (|k1| > {_MAX_ABS_DISTORTION_COEFF}) -- see v1's stored "
            "dist_coeffs [0.42, -2.43, ...] as the concrete example this bound exists to reject"
        )
    if k2_free:
        k2 = float(dist_coeffs[1])
        if abs(k2) > _MAX_ABS_DISTORTION_COEFF:
            return (
                f"implausible k2={k2:.4f} (|k2| > {_MAX_ABS_DISTORTION_COEFF}) -- see v1's "
                "stored dist_coeffs [0.42, -2.43, ...] as the concrete example this bound "
                "exists to reject"
            )
        if abs(k1) > 1e-9 and abs(k2 / k1) > _MAX_K2_OVER_K1_RATIO:
            return (
                f"implausible k2/k1 ratio {k2 / k1:.2f} (> {_MAX_K2_OVER_K1_RATIO}) -- v1's "
                f"stored dist_coeffs [0.42, -2.43, ...] had a ratio of "
                f"{-2.43 / 0.42:.2f}, exactly the failure mode this bound rejects"
            )
    return None


def _estimate_intrinsics(
    object_points: np.ndarray,
    image_points: np.ndarray,
    image_size: Tuple[int, int],
    *,
    elevated_feature_count: int,
    z_spread_cm: float,
) -> _IntrinsicsFit:
    """:func:`_fit_intrinsics` with OpenCV's own refusals turned into a
    rejected :class:`_IntrinsicsFit` (sprint 044 ticket 010).

    ``cv2.calibrateCamera`` raises ``cv2.error`` when the fit subset has
    no more residuals than free parameters -- exactly what the held-out
    split produces at the six-point floor (4 fit points = 8 residuals vs
    8 variables in the elevated model). That is an under-determined
    input, not a bug: it must surface as a ``succeeded=False`` result
    with a reason, never a traceback through the RPC.
    """
    try:
        return _fit_intrinsics(
            object_points,
            image_points,
            image_size,
            elevated_feature_count=elevated_feature_count,
            z_spread_cm=z_spread_cm,
        )
    except cv.error as exc:
        n = len(object_points)
        elevated_mode = (
            elevated_feature_count >= MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE
            and z_spread_cm >= MIN_ELEVATED_Z_SPREAD_CM
        )
        return _IntrinsicsFit(
            accepted=False,
            reason=(
                f"too few ground-truth points to fit a lens model ({n} detected; "
                f"part of them are held out for validation) -- survey and detect "
                f"more ArUco/AprilTag points on the playfield (nine or more is "
                f"comfortable). OpenCV said: {str(exc).splitlines()[0][:200]}"
            ),
            camera_matrix=None,
            dist_coeffs=None,
            mode="elevated" if elevated_mode else "coplanar-constrained",
            elevated_feature_count=elevated_feature_count,
            held_out_rms_px=None,
        )


def _fit_intrinsics(
    object_points: np.ndarray,
    image_points: np.ndarray,
    image_size: Tuple[int, int],
    *,
    elevated_feature_count: int,
    z_spread_cm: float,
) -> _IntrinsicsFit:
    """Fit one camera's intrinsics/distortion from a single view's
    surveyed-and-detected points (module docstring). *object_points* is
    ``(N, 3)`` -- ``(x, y, height_cm)`` in cm -- and *image_points* is
    ``(N, 2)`` -- the matching **raw** (not undistorted -- that would be
    circular) detected pixel positions, both already aligned by index
    with *elevated_feature_count*/*z_spread_cm* describing the same set.

    Mode selection (Open Question 6, :data:`MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE`/
    :data:`MIN_ELEVATED_Z_SPREAD_CM`) is graded and decided here, from
    what was actually surveyed and detected this burst -- never a
    per-call argument (sprint.md Design Rationale Decision 2).
    """
    n = len(object_points)
    elevated_mode = (
        elevated_feature_count >= MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE
        and z_spread_cm >= MIN_ELEVATED_Z_SPREAD_CM
    )
    mode = "elevated" if elevated_mode else "coplanar-constrained"

    fit_idx, holdout_idx = _held_out_split(n)
    obj_fit = object_points[fit_idx].astype(np.float32)
    img_fit = image_points[fit_idx].astype(np.float32)

    if elevated_mode:
        # "Freed further" (sprint.md Design Rationale Decision 2): no
        # CALIB_FIX_* constraint at all -- principal point, aspect
        # ratio, tangential terms, and k3 are all estimated, since
        # genuine z-spread resolves the fx/depth degeneracy the
        # coplanar-constrained flags below exist to defend against
        # (module docstring's empirical evidence). CALIB_USE_INTRINSIC_GUESS
        # is required here purely because OpenCV refuses to
        # self-initialize a non-planar rig's starting camera matrix --
        # it does not fix anything (see _initial_camera_matrix_guess's
        # own docstring).
        flags = cv.CALIB_USE_INTRINSIC_GUESS
        initial_guess = _initial_camera_matrix_guess(image_size)
        # mypy/cv2-stubs note: the stubs declare cameraMatrix/distCoeffs as
        # required, non-Optional arrays; distCoeffs=None here is correct OpenCV
        # usage (self-initializes the distortion coefficients even with an
        # explicit cameraMatrix guess) -- the stubs are simply narrower than
        # the real C++ API.
        rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv.calibrateCamera(  # type: ignore[call-overload]
            [obj_fit], [img_fit], image_size, initial_guess, None, flags=flags
        )
    else:
        # The coplanar-constrained default (sprint.md Design Rationale
        # Decision 2, this ticket's headline acceptance criterion):
        # fixed principal point, fixed aspect ratio, zero tangential
        # distortion, fixed k3 -- k1-only unless
        # _FREE_K2_IN_COPLANAR_DEFAULT says otherwise (Open Question 5,
        # module docstring: measurably worse on held-out residual, so
        # it stays False).
        flags = (
            cv.CALIB_FIX_PRINCIPAL_POINT
            | cv.CALIB_FIX_ASPECT_RATIO
            | cv.CALIB_ZERO_TANGENT_DIST
            | cv.CALIB_FIX_K3
        )
        if not _FREE_K2_IN_COPLANAR_DEFAULT:
            flags |= cv.CALIB_FIX_K2

        if np.all(obj_fit[:, 2] == 0.0):
            # No initial cameraMatrix -- OpenCV self-initializes a
            # planar rig's starting focal length/principal point from
            # the correspondences themselves via its own internal
            # homography-based heuristic. Load-bearing, not just an
            # implementation-detail choice: this is what makes this
            # ticket's own fx/depth-degeneracy sanity check
            # (:func:`_sanity_check_intrinsics`) actually fire on a
            # near-exact-nadir single coplanar view. A run with this
            # ticket's own investigation found that seeding the
            # optimizer with an explicit initial guess instead lets a
            # degenerate fit converge to (or barely move from)
            # whatever the guess said -- landing inside a "plausible"
            # focal-length band purely because the guess did, not
            # because the fit resolved anything -- which would silently
            # defeat the very check this branch exists to trigger.
            # Auto-init has no such anchor: it reliably produces wildly
            # implausible focal lengths (tens to hundreds of times the
            # true value, confirmed directly) precisely when the
            # underlying fx/depth ridge is genuinely flat, which is
            # exactly the signal :func:`_sanity_check_intrinsics` needs.
            # mypy/cv2-stubs note: the stubs declare cameraMatrix/distCoeffs as
            # required, non-Optional arrays; passing None for both here is correct
            # OpenCV usage requesting full self-initialization for a planar rig
            # (see the comment above) -- the stubs are simply narrower than the
            # real C++ API.
            rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv.calibrateCamera(  # type: ignore[call-overload]
                [obj_fit], [img_fit], image_size, None, None, flags=flags
            )
        else:
            # The graded-threshold edge case (Open Question 6): mode
            # selection decided elevated_mode=False (not enough elevated
            # count/z-spread to free the model), but fit_idx still
            # contains at least one nonzero-height point -- e.g. a
            # below-threshold elevated feature that this ticket's own
            # even-spread _held_out_split (see that function's own
            # docstring) placed in the fit subset rather than the
            # held-out one. OpenCV's planar-rig auto-initialization
            # above only fires when every fit_idx point is genuinely
            # z=0, so this subset needs an explicit initial guess purely
            # to avoid "For non-planar calibration rigs the initial
            # intrinsic matrix must be specified" -- confirmed directly
            # by this ticket's own test suite. Rare in practice (it
            # requires elevation to exist below the graded threshold at
            # all), and the sanity-bound weakening the all-coplanar
            # branch above avoids does not apply here either way: this
            # branch already has genuine (if sub-threshold) 3D
            # structure to anchor the fit, unlike the purely-planar case.
            flags |= cv.CALIB_USE_INTRINSIC_GUESS
            initial_guess = _initial_camera_matrix_guess(image_size)
            # mypy/cv2-stubs note: the stubs declare cameraMatrix/distCoeffs as
            # required, non-Optional arrays; distCoeffs=None here is correct OpenCV
            # usage (self-initializes the distortion coefficients even with an
            # explicit cameraMatrix guess, same as the elevated branch above) --
            # the stubs are simply narrower than the real C++ API.
            rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv.calibrateCamera(  # type: ignore[call-overload]
                [obj_fit], [img_fit], image_size, initial_guess, None, flags=flags
            )

    held_out_rms_px: Optional[float] = None
    if holdout_idx:
        # Reuses the SAME extrinsic pose calibrateCamera just solved for
        # the fit subset -- valid because every point (fit and held-out
        # alike) is on the same rigid field, observed by the same
        # camera in the same single frame, so there is exactly one true
        # pose. No separate solvePnP call: with as few as 2 held-out
        # points that would not even be possible (solvePnP itself
        # requires >= 4), confirmed directly during this ticket's own
        # investigation.
        obj_hold = object_points[holdout_idx].astype(np.float64)
        img_hold = image_points[holdout_idx]
        projected, _ = cv.projectPoints(obj_hold, rvecs[0], tvecs[0], camera_matrix, dist_coeffs)
        projected = projected.reshape(-1, 2)
        held_out_rms_px = float(
            np.sqrt(np.mean(np.sum((projected - img_hold) ** 2, axis=1)))
        )

    dist_coeffs_flat = dist_coeffs.reshape(-1)
    reason = _sanity_check_intrinsics(
        camera_matrix,
        dist_coeffs_flat,
        image_size,
        k2_free=elevated_mode or _FREE_K2_IN_COPLANAR_DEFAULT,
    )
    if reason is not None:
        return _IntrinsicsFit(
            accepted=False,
            reason=reason,
            camera_matrix=None,
            dist_coeffs=None,
            mode=mode,
            elevated_feature_count=elevated_feature_count,
            held_out_rms_px=held_out_rms_px,
        )

    return _IntrinsicsFit(
        accepted=True,
        reason=None,
        camera_matrix=_matrix_to_tuple(camera_matrix),
        dist_coeffs=tuple(float(c) for c in dist_coeffs_flat),
        mode=mode,
        elevated_feature_count=elevated_feature_count,
        held_out_rms_px=held_out_rms_px,
    )


class Calibrator(_CalibratorBase):
    """Concrete ``calibration.interfaces.Calibrator``.

    Parameters
    ----------
    get_camera_config:
        Resolves a camera slug -> its currently-held ``CameraConfig``
        (``None`` if the camera has never been configured). Daemon-owned
        state (``daemon.service.DaemonService._camera_configs``),
        injected rather than imported so this module never depends on
        the daemon -- the camera's ``.playfield`` link is checked
        **before** *capture_frames* is ever called (see the module
        docstring's ``NoPlayfieldLinked`` section).
    get_playfield:
        Resolves a playfield *name* -> its ``PlayfieldDefinition``
        (``None`` if the daemon does not currently hold one by that
        name). A camera config naming a playfield the daemon does not
        hold is treated the same as no link at all -- both mean "no
        coordinate system exists to map to right now."
    capture_frames:
        Camera frame access, handed in (module docstring). Returns
        however many raw BGR frames it was able to capture for a camera
        slug -- zero or more; this class multi-frame-averages whatever
        it is given rather than dictating a count itself.
    detector_factory:
        Builds the one ``vision.detector.TagDetector`` this instance
        reuses across every :meth:`calibrate` call, for every camera --
        mirrors ``daemon.detection.DetectionEngine``'s "building an
        ``ArucoDetector`` is not free, reuse it" reasoning. Overridable
        for tests.
    pipeline_config:
        Passed straight through to ``vision.pipeline.run_pipeline`` for
        every captured frame. ``None`` uses that function's own default.
    max_reprojection_error_cm:
        The reprojection-error acceptance ceiling (ticket 023-006) --
        defaults to the module-level :data:`MAX_REPROJECTION_ERROR_CM`
        (1.0 cm) but is genuinely overridable per instance, since the
        confirmed default is a domain-tuning call (sprint.md Decisions),
        not a structural one.
    get_prior_calibration:
        Resolves a camera slug -> the ``Calibration`` its *most recent
        successful* calibration produced, if any (sprint 030 ticket
        002) -- what an ordinary (``estimate_intrinsics=False``)
        :meth:`calibrate` call reads to carry a lens model forward.
        Defaults to :meth:`get_calibration` (this instance's own
        write-only-until-now cache), reproducing ticket 001's exact
        same-process behaviour when a caller does not override it.

        **Why this parameter exists at all -- a confirmed bug ticket 001
        left for this ticket to close.** ``daemon.service.DaemonService``
        holds *two* separate calibration caches: its own
        ``self._calibrations`` (populated at construction from disk,
        ``_load_calibrations``, and by every successful
        ``DaemonService.calibrate`` call), and this class's
        ``self._calibrations`` (populated *only* by a successful
        :meth:`calibrate` call *in this process*). Before this
        parameter existed, ticket 001's carry-forward read the latter --
        correct for a same-process recalibration, but empty on a freshly
        started daemon that has not yet run a single in-process
        ``calibrate`` call, even though real intrinsics sit right there
        in a loaded ``calibration.json``. The result: any recalibration
        immediately after a daemon restart silently wiped a
        previously-estimated lens model back to ``None``, discarding
        real operator effort with no error and no warning. Injecting
        ``DaemonService.get_calibration`` here (a bound-method reference,
        resolved at call time -- safe to pass before that service's own
        stores exist, the same pattern ``DetectionEngine``'s
        ``calibration_lookup=self.get_calibration`` already uses in
        ``daemon/service.py.__init__``) makes the carry-forward read the
        daemon's disk-backed store instead, closing the gap. Test
        coverage: ``tests/aprilcam/daemon/test_service.py``'s
        restart-carries-forward-intrinsics test constructs a *fresh*
        ``DaemonService`` against a ``calibration.json`` already on disk
        (never touched by this process before) and recalibrates,
        asserting the lens model survives -- a same-process-only test
        would pass with the bug still present.
    """

    def __init__(
        self,
        *,
        get_camera_config: CameraConfigLookup,
        get_playfield: PlayfieldLookup,
        capture_frames: FrameSource,
        detector_factory: DetectorFactory = _default_detector_factory,
        pipeline_config: Optional[PipelineConfig] = None,
        max_reprojection_error_cm: float = MAX_REPROJECTION_ERROR_CM,
        get_prior_calibration: Optional[CalibrationLookup] = None,
        get_lens_model: Optional[LensLookup] = None,
        get_lens_warning: Optional[LensWarningLookup] = None,
    ) -> None:
        self._get_lens_model = get_lens_model
        self._get_lens_warning = get_lens_warning
        self._get_camera_config = get_camera_config
        self._get_playfield = get_playfield
        self._capture_frames = capture_frames
        self._pipeline_config = pipeline_config
        self._detector = detector_factory()
        self._max_reprojection_error_cm = max_reprojection_error_cm
        self._calibrations: Dict[str, Calibration] = {}
        self._lock = threading.Lock()
        # Bound to self.get_calibration -- not resolved until first
        # called, so this is safe even though it references a method
        # that reads self._calibrations, set just above. See this
        # class's own docstring ("get_prior_calibration") for why a
        # caller (DaemonService) overrides this to fix a real carry-
        # forward-after-restart bug.
        self._get_prior_calibration: CalibrationLookup = (
            get_prior_calibration if get_prior_calibration is not None else self.get_calibration
        )

    def calibrate(
        self, camera_slug: str, *, estimate_intrinsics: bool = False
    ) -> CalibrationResult:
        """See ``calibration.interfaces.Calibrator.calibrate``.

        On success, the resulting :class:`Calibration` is stored
        internally and retrievable via :meth:`get_calibration` -- an
        addition beyond the declared ``Calibrator`` base class (the same
        "additive helper on the concrete class" pattern
        ``config.loader.PlayfieldDefinition.aruco_position`` uses), since
        the base class's declared return type is only
        ``CalibrationResult``. ``daemon.service.DaemonService.calibrate``
        reads it immediately after a successful call to get the object
        it persists to ``calibration.json`` -- this class never touches
        a filesystem itself (persistence is the daemon's job, sprint
        architecture's module boundary table).

        *estimate_intrinsics* (sprint 030 ticket 002) is the module
        docstring's "Intrinsics estimation" section, end to end: when
        ``True``, runs :func:`_estimate_intrinsics` over this burst's
        surveyed-and-detected ArUco+AprilTag points (all of them,
        coplanar and elevated alike) and stores the result if -- and
        only if -- it passes :func:`_sanity_check_intrinsics`; when
        ``False`` (the default), carries forward whatever lens model
        *camera_slug*'s prior successful calibration already held, via
        :attr:`_get_prior_calibration` (**not** :meth:`get_calibration`
        directly -- see that attribute's own docstring for the
        carry-forward-after-restart bug this distinction fixes).
        """
        config = self._get_camera_config(camera_slug)
        if config is None or config.playfield is None:
            raise NoPlayfieldLinked(camera_slug)

        definition = self._get_playfield(config.playfield)
        if definition is None:
            # The camera names a playfield the daemon does not currently
            # hold (e.g. pushed once, then the daemon restarted and only
            # the camera config -- not the playfield -- was re-pushed).
            # From this method's point of view there is, right now, no
            # coordinate system to map to either way -- the same
            # precondition class UC-15.S-2 names, so it gets the same
            # error rather than a second type for a scenario with no
            # dedicated use-case text.
            raise NoPlayfieldLinked(camera_slug)
        if not isinstance(definition, _ConcretePlayfieldDefinition):
            # Every PlayfieldDefinition this daemon ever holds was built
            # either by ConfigLoader.load_playfield or by
            # daemon/rpc.py's _playfield_definition_from_proto -- both
            # always produce this concrete type, so this is not reached
            # through normal use. Guards against a future
            # PlayfieldDefinition subclass this module would not know
            # how to read ``aruco_position`` from -- the same defensive
            # check ``daemon/rpc.py``'s ``_playfield_entry_to_proto`` and
            # ``client/daemon_client.py``'s ``set_playfield`` make for
            # the identical reason.
            raise TypeError(
                f"cannot calibrate against playfield {definition.name!r}: definition is "
                f"{type(definition).__name__}, not aprilcam.config.loader.PlayfieldDefinition"
            )

        if estimate_intrinsics:
            # The n_pts >= 6 floor (module docstring, sprint.md Design
            # Rationale Decision 2) counts what the playfield definition
            # itself surveys -- never what a burst happens to detect --
            # and is checked here, before any frame is captured, exactly
            # like the NoPlayfieldLinked precondition above: cheap, and
            # there is nothing a frame could tell us that would change
            # this answer.
            surveyed_count = len(definition.aruco_tags) + len(definition.april_tags)
            if surveyed_count < MIN_SURVEYED_INTRINSICS_POINTS:
                return CalibrationResult(
                    succeeded=False,
                    matched_ids=(),
                    missing_ids=(),
                    intrinsics_estimated=False,
                    reason=(
                        f"estimate_intrinsics requires at least "
                        f"{MIN_SURVEYED_INTRINSICS_POINTS} ArUco+AprilTag ground-truth "
                        f"points surveyed on the linked playfield {definition.name!r}; "
                        f"found {surveyed_count}"
                    ),
                )

        frames = self._capture_frames(camera_slug)
        if not frames:
            raise RuntimeError(f"no frames available from camera {camera_slug!r}")

        first_frame = frames[0]
        image_size = (int(first_frame.shape[1]), int(first_frame.shape[0]))
        # The lens store's size-matched model (sprint 044): the first
        # source in the carry-forward order, and what makes
        # estimate_intrinsics refuse to silently replace a measured lens.
        stored_lens = (
            self._get_lens_model(camera_slug, image_size)
            if self._get_lens_model is not None
            else None
        )
        lens_warning = (
            self._get_lens_warning(camera_slug, image_size)
            if self._get_lens_warning is not None and stored_lens is None
            else None
        )
        if estimate_intrinsics and stored_lens is not None:
            return CalibrationResult(
                succeeded=False,
                matched_ids=(),
                missing_ids=(),
                intrinsics_estimated=False,
                lens_model_source=LENS_SOURCE_MEASURED,
                reason=(
                    f"estimate_intrinsics refused: camera {camera_slug!r} has a measured "
                    f"lens model installed ({stored_lens.image_size[0]}x"
                    f"{stored_lens.image_size[1]}, {stored_lens.views} views); a ChArUco "
                    f"measurement is better than a field estimate. Remove or re-measure "
                    f"the installed lens model instead."
                ),
            )

        detected = self._detect_averaged(frames)
        aruco_detected = detected.get(TagFamily.ARUCO, {})
        april_detected = detected.get(TagFamily.APRILTAG, {})

        required = definition.required_aruco_ids()
        matched_ids = tuple(sorted(i for i in required if i in aruco_detected))
        missing_ids = tuple(sorted(i for i in required if i not in aruco_detected))

        stray = sorted(set(aruco_detected) - set(required))
        if stray:
            logger.info(
                "calibrate(%s): ignoring %d detected ArUco id(s) not in the "
                "playfield definition: %s",
                camera_slug,
                len(stray),
                stray,
            )

        if missing_ids:
            return CalibrationResult(
                succeeded=False,
                matched_ids=matched_ids,
                missing_ids=missing_ids,
                lens_warning=lens_warning,
            )

        aruco_by_id = {entry.tag_id: entry for entry in definition.aruco_tags}
        april_by_id = {entry.tag_id: entry for entry in definition.april_tags}

        # The homography/intrinsics split (module docstring): the
        # homography solve and reprojection_error_cm below use *only*
        # the coplanar (height_cm == 0) subset of the matched ArUco
        # ids -- never AprilTags (never part of the homography, before
        # or after this ticket) and never an elevated ArUco entry, if a
        # playfield definition ever declares one. This keeps
        # reprojection_error_cm comparable to every calibration recorded
        # before this sprint, regardless of estimation mode.
        homography_ids = tuple(i for i in matched_ids if aruco_by_id[i].height_cm == 0.0)

        intrinsics_estimated: Optional[bool] = None
        intrinsics_mode: Optional[str] = None
        elevated_features_used: Optional[int] = None

        if estimate_intrinsics:
            intrinsics_points: List[Tuple[PixelPoint, float, float, float]] = [
                (aruco_detected[tag_id], entry.x, entry.y, entry.height_cm)
                for tag_id, entry in ((i, aruco_by_id[i]) for i in matched_ids)
            ]
            intrinsics_points.extend(
                (april_detected[tag_id], entry.x, entry.y, entry.height_cm)
                for tag_id, entry in april_by_id.items()
                if tag_id in april_detected
            )

            object_points = np.array(
                [[x, y, h] for (_pixel, x, y, h) in intrinsics_points], dtype=np.float64
            )
            image_points = np.array(
                [[pixel.x, pixel.y] for (pixel, _x, _y, _h) in intrinsics_points], dtype=np.float64
            )
            heights = object_points[:, 2]
            elevated_count = int(np.count_nonzero(heights > 0.0))
            z_spread_cm = float(heights.max() - heights.min()) if len(heights) else 0.0

            fit = _estimate_intrinsics(
                object_points,
                image_points,
                image_size,
                elevated_feature_count=elevated_count,
                z_spread_cm=z_spread_cm,
            )
            # Held-out validation, reported -- never silently discarded
            # (this ticket's own acceptance criterion): logged here
            # regardless of accept/reject, since _IntrinsicsFit itself
            # is an internal type CalibrationResult does not carry
            # held_out_rms_px on (only the accept/reject outcome and
            # mode/count do -- mirroring "reports what the interface
            # promises" for the fitted numbers themselves).
            logger.info(
                "calibrate(%s): intrinsics fit mode=%s elevated_features=%d "
                "held_out_rms_px=%s accepted=%s%s",
                camera_slug,
                fit.mode,
                fit.elevated_feature_count,
                f"{fit.held_out_rms_px:.4f}" if fit.held_out_rms_px is not None else "n/a",
                fit.accepted,
                f" reason={fit.reason!r}" if fit.reason else "",
            )
            if not fit.accepted:
                return CalibrationResult(
                    succeeded=False,
                    matched_ids=matched_ids,
                    missing_ids=(),
                    intrinsics_estimated=False,
                    intrinsics_mode=fit.mode,
                    elevated_features_used=fit.elevated_feature_count,
                    reason=f"intrinsics estimation rejected: {fit.reason}",
                    lens_warning=lens_warning,
                )
            camera_matrix = fit.camera_matrix
            dist_coeffs = fit.dist_coeffs
            intrinsics_estimated = True
            lens_source: Optional[str] = LENS_SOURCE_ESTIMATED
            intrinsics_mode = fit.mode
            elevated_features_used = fit.elevated_feature_count
        else:
            # Lens-model carry-forward (sprint 030 ticket 001, Design
            # Rationale Decision 3): an ordinary recalibration (no
            # intrinsics estimation of its own) neither estimates nor
            # discards whatever lens model this camera's *prior*
            # successful calibration already held; it carries it
            # forward unchanged onto the fresh ``Calibration`` this call
            # produces. ``None`` (never calibrated before, or
            # calibrated with no known lens model) reproduces exactly
            # today's intrinsics-free behaviour. Reads
            # ``self._get_prior_calibration``, not ``self.
            # get_calibration`` directly -- see that attribute's
            # docstring for why (the restart carry-forward bug fix).
            if stored_lens is not None:
                camera_matrix = stored_lens.camera_matrix
                dist_coeffs = stored_lens.dist_coeffs
                lens_source = LENS_SOURCE_MEASURED
            else:
                prior = self._get_prior_calibration(camera_slug)
                camera_matrix = prior.camera_matrix if prior is not None else None
                dist_coeffs = prior.dist_coeffs if prior is not None else None
                lens_source = (
                    (prior.lens_source or LENS_SOURCE_LEGACY)
                    if prior is not None and camera_matrix is not None
                    else None
                )
            intrinsics_estimated = False

        intrinsics_known = camera_matrix is not None and dist_coeffs is not None
        if not intrinsics_known:
            lens_source = None
        lens_model_source = lens_source if lens_source is not None else LENS_SOURCE_NONE

        # Undistort-before-solve (Design Rationale Decision 1): when
        # this camera's lens model is known, every detected marker
        # pixel *in the coplanar homography set* is undistorted
        # *before* it ever reaches the homography solve or the
        # reprojection-error measurement below -- both
        # ``_solve_homography`` and ``_reprojection_error_cm`` are
        # handed this same already-undistorted ``pixel_pts`` array, per
        # v1's own earned lesson (``archive/.../calibration.py:1342-
        # 1346``): pairing the homography solved from undistorted
        # points with an RMS measured on a *different* (still
        # distorted) point set silently reports a healthy error for a
        # solve that is not actually healthy. There is no separate
        # "undistorted pixel_pts" variable for this reason -- one array
        # feeds every downstream step, exactly like the intrinsics-free
        # path already does.
        if intrinsics_known:
            camera_matrix_array = np.array(camera_matrix, dtype=np.float64)
            dist_coeffs_array = np.array(dist_coeffs, dtype=np.float64)
            undistorted = {
                tag_id: distortion.undistort_point(
                    aruco_detected[tag_id], camera_matrix_array, dist_coeffs_array
                )
                for tag_id in homography_ids
            }
            pixel_pts = np.array(
                [[undistorted[i].x, undistorted[i].y] for i in homography_ids], dtype=np.float64
            )
        else:
            pixel_pts = np.array(
                [[aruco_detected[i].x, aruco_detected[i].y] for i in homography_ids],
                dtype=np.float64,
            )
        world_pts = np.array(
            [[aruco_by_id[i].x, aruco_by_id[i].y] for i in homography_ids], dtype=np.float64
        )

        degenerate = _degeneracy_reason(pixel_pts, world_pts)
        if degenerate is not None:
            return CalibrationResult(
                succeeded=False,
                matched_ids=matched_ids,
                missing_ids=(),
                reason=degenerate,
                intrinsics_estimated=intrinsics_estimated,
                lens_model_source=lens_model_source,
                lens_warning=lens_warning,
                intrinsics_mode=intrinsics_mode,
                elevated_features_used=elevated_features_used,
            )

        homography = _solve_homography(pixel_pts, world_pts)
        if homography is None:
            return CalibrationResult(
                succeeded=False,
                matched_ids=matched_ids,
                missing_ids=(),
                reason="homography solve failed: singular or ill-conditioned matrix",
                intrinsics_estimated=intrinsics_estimated,
                lens_model_source=lens_model_source,
                lens_warning=lens_warning,
                intrinsics_mode=intrinsics_mode,
                elevated_features_used=elevated_features_used,
            )

        rms = _reprojection_error_cm(homography, pixel_pts, world_pts)

        if rms > self._max_reprojection_error_cm:
            # Complete (every required id found -- missing_ids stays
            # empty) but sloppy: UC-04's open question, resolved. Named
            # via `reason`, not `missing_ids`, per the interface
            # docstring's "why a failure failed when ids alone don't
            # say" -- and nothing is stored, matching the "a failed
            # attempt leaves the previous calibration as it was"
            # contract every other rejection branch above already
            # honors.
            return CalibrationResult(
                succeeded=False,
                matched_ids=matched_ids,
                missing_ids=(),
                reprojection_error_cm=rms,
                reason=(
                    f"reprojection error {rms:.3f} cm exceeds the "
                    f"{self._max_reprojection_error_cm:.3f} cm acceptance threshold"
                ),
                intrinsics_estimated=intrinsics_estimated,
                lens_model_source=lens_model_source,
                lens_warning=lens_warning,
                intrinsics_mode=intrinsics_mode,
                elevated_features_used=elevated_features_used,
            )

        calibration = Calibration(
            stale=False,
            calibrated_at=time.time(),
            playfield=definition.name,
            homography=_matrix_to_tuple(homography),
            matched_ids=matched_ids,
            reprojection_error_cm=rms,
            camera_matrix=camera_matrix,
            dist_coeffs=dist_coeffs,
            lens_source=lens_source,
        )
        with self._lock:
            self._calibrations[camera_slug] = calibration

        return CalibrationResult(
            succeeded=True,
            matched_ids=matched_ids,
            missing_ids=(),
            reprojection_error_cm=rms,
            intrinsics_estimated=intrinsics_estimated,
                lens_model_source=lens_model_source,
                lens_warning=lens_warning,
            intrinsics_mode=intrinsics_mode,
            elevated_features_used=elevated_features_used,
        )

    def get_calibration(self, camera_slug: str) -> Optional[Calibration]:
        """The :class:`Calibration` a successful :meth:`calibrate` call
        for *camera_slug* most recently produced, or ``None`` if none
        has (yet) succeeded on this instance. See :meth:`calibrate`'s
        docstring for why this is additive rather than declared on
        ``calibration.interfaces.Calibrator``."""
        with self._lock:
            return self._calibrations.get(camera_slug)

    def _detect_averaged(
        self, frames: Sequence[np.ndarray]
    ) -> Dict[TagFamily, Dict[int, PixelPoint]]:
        """Multi-frame averaged centre detection, **both** fiducial
        families: run every frame through the same preprocess-then-detect
        pipeline ``daemon/detection.py`` uses, and average each
        ``(family, id)``'s detected pixel centre across every frame it
        appeared in. An id missing from some frames but present in
        others still averages over however many detections it actually
        got (mirrors ``archive/.../homography.py``'s
        ``detect_all_tags``).

        Keyed by family first (sprint 030 ticket 002) -- before this
        ticket, AprilTag hits were discarded here entirely, since the
        homography solve only ever matched against ``required_aruco_ids()``
        (ArUco only, and still true: :meth:`calibrate`'s
        ``homography_ids`` never includes an AprilTag). AprilTags are
        kept now because :func:`_estimate_intrinsics` uses **every**
        surveyed-and-detected point of *either* family as calibration
        ground truth (module docstring) -- ``main-playfield`` alone adds
        one (AprilTag 1, field centre) beyond its 10 ArUco corners.
        ``TagFamily.ARUCO``'s inner dict is exactly what this method
        returned before this ticket (now reached via
        ``detected[TagFamily.ARUCO]``); a family with zero detections
        this burst is simply absent from the outer dict, not present
        with an empty inner one -- callers use ``.get(family, {})``.

        Since sprint 045 this is a thin derivation from
        :meth:`_detect_markers` (it returns each observation's
        ``centre``), so the centre-point path and the corner-preserving
        path can never disagree about what was detected.
        """
        return {
            family: {tag_id: obs.centre for tag_id, obs in by_id.items()}
            for family, by_id in self._detect_markers(frames).items()
        }

    def _detect_markers(
        self, frames: Sequence[np.ndarray]
    ) -> Dict[TagFamily, Dict[int, MarkerObservation]]:
        """Multi-frame averaged marker observations, corners preserved.

        Every frame goes through the same preprocess-then-detect pipeline
        as :meth:`_detect_averaged` (``run_pipeline`` then
        ``TagDetector.detect(scale=rescale.scale)``, so corners come back
        in the original frame's pixels). For each ``(family, id)`` the four
        corners are averaged **per corner index** across the frames the
        marker appeared in -- a marker seen in only a subset of frames
        averages over those (``frames`` records how many). The corner order
        is the detector's own (ArUco TL, TR, BR, BL of the marker's own
        frame) and is never re-sorted, even for a marker rendered rotated or
        upside down: the pose solve (sprint 045 ticket 004) reads the
        marker's orientation from that order. ``corner_spread_px`` is the
        largest per-corner across-frame standard deviation, a cheap noise
        measure (0.0 for a single frame). A family with no detections is
        absent from the outer dict.
        """
        accum: Dict[TagFamily, Dict[int, List[Tuple[np.ndarray, Tuple[float, float]]]]] = {}
        for frame in frames:
            gray, rescale = run_pipeline(frame, self._pipeline_config)
            for detection in self._detector.detect(gray, scale=rescale.scale):
                accum.setdefault(detection.family, {}).setdefault(
                    detection.number, []
                ).append(
                    (
                        np.asarray(detection.corners, dtype=np.float64).reshape(4, 2),
                        (detection.center.x, detection.center.y),
                    )
                )
        result: Dict[TagFamily, Dict[int, MarkerObservation]] = {}
        for family, by_id in accum.items():
            observations: Dict[int, MarkerObservation] = {}
            for tag_id, stack in by_id.items():
                arr = np.stack([c for c, _ in stack])  # (n, 4, 2)
                centres = [ctr for _, ctr in stack]
                mean = arr.mean(axis=0)  # (4, 2)
                # Per-corner scalar stddev: sqrt(var_x + var_y).
                spread = float(np.sqrt(arr.var(axis=0).sum(axis=1)).max())
                corners = tuple(PixelPoint(x=float(c[0]), y=float(c[1])) for c in mean)
                observations[tag_id] = MarkerObservation(
                    tag_id=tag_id,
                    # Mean of the detector's own per-frame centres (the exact
                    # arithmetic the centre-only path always used).
                    centre=PixelPoint(
                        x=float(np.mean([p[0] for p in centres])),
                        y=float(np.mean([p[1] for p in centres])),
                    ),
                    corners=(corners[0], corners[1], corners[2], corners[3]),
                    frames=len(stack),
                    corner_spread_px=spread,
                )
            result[family] = observations
        return result
