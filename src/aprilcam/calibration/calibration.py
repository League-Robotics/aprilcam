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
solve-order mechanics only. Sprint 045 deleted the single-view estimator
that once *produced* intrinsics; a measured lens now comes from the
per-camera lens store (``calibration.lens``).
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

from aprilcam.calibration import distortion, gates, geometry, pose
from aprilcam.calibration.interfaces import Calibration as _CalibrationBase
from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.calibration.interfaces import Calibrator as _CalibratorBase
from aprilcam.calibration.interfaces import MarkerObservation
from aprilcam.calibration.lens import (
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
    (:meth:`Calibrator.calibrate`); the measured lens store populates them
    (sprint 044). They
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
    per-camera lens store), ``'estimated'`` (an old file's
    single-view fit, no longer produced) or
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
    observe_target:
        Optional calibration-target observer (sprint 045 ticket 011), a
        ``pose.TargetObserver``: ``(gray_frame, CalibrationTarget) ->
        corners`` with ``field_cm`` and ``pixel`` attributes. Injected (the
        daemon builds a ChArUco one) so this package never imports
        ``intrinsics``. Without it a definition's ``calibration_target`` is
        ignored and the calibrator behaves exactly as before.
    max_reprojection_error_cm:
        The reprojection-error acceptance ceiling (ticket 023-006) --
        defaults to the module-level :data:`MAX_REPROJECTION_ERROR_CM`
        (1.0 cm) but is genuinely overridable per instance, since the
        confirmed default is a domain-tuning call (sprint.md Decisions),
        not a structural one.
    get_prior_calibration:
        Resolves a camera slug -> the ``Calibration`` its *most recent
        successful* calibration produced, if any (sprint 030 ticket
        002) -- what :meth:`calibrate` reads to carry a lens model forward.
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
        observe_target: Optional[pose.TargetObserver] = None,
    ) -> None:
        self._observe_target = observe_target
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

    def calibrate(self, camera_slug: str) -> CalibrationResult:
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

        An ordinary recalibration carries forward whatever lens model
        *camera_slug*'s prior successful calibration already held, via
        :attr:`_get_prior_calibration` (**not** :meth:`get_calibration`
        directly -- see that attribute's own docstring for the
        carry-forward-after-restart bug this distinction fixes).

        **Strategy (sprint 045).** After the marker policy check
        (``required`` ids all seen, else a failure naming ``missing_ids``;
        ``helpful`` ones only sharpen the result), one of two solves runs.
        The **pose** path (:meth:`_calibrate_by_pose`) is used iff the lens
        store holds a *measured* model at this frame size and a matched
        on-plane marker has a ``size_mm``: it solves the camera's planar
        pose from the marker corners, derives the homography from it, and
        gates the result (``calibration/gates.py``), reporting ``method``,
        ``confidence`` (``full`` / ``reduced`` / ``low``) and every gate
        metric on the :class:`CalibrationResult`. Otherwise the unchanged
        **centre-point** path (:meth:`_calibrate_by_centres`) runs and the
        result says ``method='centre-point'`` with a one-line
        ``method_reason``. A gate failure stores nothing: the previous
        calibration survives.
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

        frames = self._capture_frames(camera_slug)
        if not frames:
            raise RuntimeError(f"no frames available from camera {camera_slug!r}")

        first_frame = frames[0]
        image_size = (int(first_frame.shape[1]), int(first_frame.shape[0]))
        # The lens store's size-matched model (sprint 044): the first
        # source in the carry-forward order.
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
        detected_markers = self._detect_markers(frames)
        aruco_markers = detected_markers.get(TagFamily.ARUCO, {})
        aruco_detected = {tag_id: obs.centre for tag_id, obs in aruco_markers.items()}

        # Policy (sprint 045): ``required`` ids must all be seen; ``helpful``
        # ones only sharpen the result. A definition with no policy anywhere
        # lists every id as required -- today's all-or-nothing behaviour.
        required = definition.required_marker_ids()
        helpful = definition.helpful_marker_ids()
        listed = set(required) | set(helpful)
        matched_ids = tuple(sorted(i for i in listed if i in aruco_detected))
        missing_ids = tuple(sorted(i for i in required if i not in aruco_detected))
        helpful_missing = tuple(sorted(i for i in helpful if i not in aruco_detected))

        stray = sorted(set(aruco_detected) - listed)
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

        # Strategy choice: the pose path needs a measured lens at this frame
        # size and at least one matched on-plane marker with a known size.
        aruco_by_id = {entry.tag_id: entry for entry in definition.aruco_tags}
        target = definition.calibration_target if self._observe_target is not None else None
        target_corners: Optional[Tuple[np.ndarray, np.ndarray]] = None
        target_notes: List[str] = []
        usable_markers = any(
            aruco_by_id[i].height_cm == 0.0 and definition.marker_size_mm(i) is not None
            for i in matched_ids
        )
        if target is not None:
            lens_reason = self._pose_ineligibility(
                camera_slug, definition, aruco_by_id, matched_ids, stored_lens,
                lens_warning, has_target=True,
            )
            if lens_reason is not None:
                if not usable_markers:
                    return CalibrationResult(
                        succeeded=False,
                        reason=(
                            "calibration target needs a measured lens model "
                            f"({lens_reason}); measure the lens first"
                        ),
                        matched_ids=matched_ids,
                        missing_ids=missing_ids,
                        lens_warning=lens_warning,
                        method="pose",
                        method_reason=lens_reason,
                    )
                target_notes.append(f"calibration target ignored: {lens_reason}")
            else:
                target_corners, target_problem = self._observe_target_burst(frames, target)
                if target_corners is None:
                    if not usable_markers:
                        return CalibrationResult(
                            succeeded=False,
                            reason=f"calibration target not usable: {target_problem}",
                            matched_ids=matched_ids,
                            missing_ids=missing_ids,
                            lens_warning=lens_warning,
                            lens_model_source=LENS_SOURCE_MEASURED,
                            method="pose",
                            method_reason="calibration target declared",
                        )
                    target_notes.append(
                        f"calibration target not usable ({target_problem}); "
                        "calibrated from the markers alone"
                    )
        centre_reason = self._pose_ineligibility(
            camera_slug, definition, aruco_by_id, matched_ids, stored_lens,
            lens_warning, has_target=target_corners is not None,
        )
        if centre_reason is None:
            assert stored_lens is not None
            return self._calibrate_by_pose(
                camera_slug=camera_slug,
                definition=definition,
                aruco_markers=aruco_markers,
                matched_ids=matched_ids,
                required=required,
                helpful=helpful,
                helpful_missing=helpful_missing,
                lens=stored_lens,
                lens_warning=lens_warning,
                target_corners=target_corners,
                target_notes=tuple(target_notes),
            )
        logger.info("calibrate(%s): centre-point path: %s", camera_slug, centre_reason)
        return self._calibrate_by_centres(
            camera_slug=camera_slug,
            definition=definition,
            image_size=image_size,
            aruco_detected=aruco_detected,
            matched_ids=matched_ids,
            helpful_missing=helpful_missing,
            stored_lens=stored_lens,
            lens_warning=lens_warning,
            method_reason=centre_reason,
        )

    def _pose_ineligibility(
        self,
        camera_slug: str,
        definition: _ConcretePlayfieldDefinition,
        aruco_by_id: Dict[Any, Any],
        matched_ids: Sequence[int],
        stored_lens: Optional[LensModel],
        lens_warning: Optional[str],
        *,
        has_target: bool = False,
    ) -> Optional[str]:
        """Why the centre-point path must run instead of the pose path, as
        the one-line ``method_reason``; ``None`` when the pose path applies.
        *has_target*: calibration-target corners stand in for marker sizes."""
        if stored_lens is None:
            if lens_warning:
                return "lens measured at a different resolution"
            prior = self._get_prior_calibration(camera_slug)
            if prior is not None and prior.camera_matrix is not None:
                return f"lens source is {prior.lens_source or LENS_SOURCE_LEGACY}"
            return "no measured lens model"
        if stored_lens.source != LENS_SOURCE_MEASURED:
            return f"lens source is {stored_lens.source}"
        if not has_target and not any(
            aruco_by_id[i].height_cm == 0.0 and definition.marker_size_mm(i) is not None
            for i in matched_ids
        ):
            return "definition has no marker size"
        return None

    def _calibrate_by_pose(
        self,
        *,
        camera_slug: str,
        definition: _ConcretePlayfieldDefinition,
        aruco_markers: Dict[int, MarkerObservation],
        matched_ids: Tuple[int, ...],
        required: Tuple[int, ...],
        helpful: Tuple[int, ...],
        helpful_missing: Tuple[int, ...],
        lens: LensModel,
        lens_warning: Optional[str],
        target_corners: Optional[Tuple[np.ndarray, np.ndarray]] = None,
        target_notes: Tuple[str, ...] = (),
    ) -> CalibrationResult:
        """Planar-pose calibration from marker corners and the measured lens.

        *target_corners* (``(obj, img)`` from :meth:`_observe_target_burst`)
        are calibration-target correspondences, pooled with the marker
        corners when both exist (``pose: markers+target``) and solved by the
        same :func:`pose.solve_planar_pose` under the same gates.

        An orchestrator: ``calibration/pose.py`` does the arithmetic,
        ``calibration/gates.py`` decides. Nothing is stored on failure, so a
        previous calibration survives a rejected attempt.
        """
        aruco_by_id = {entry.tag_id: entry for entry in definition.aruco_tags}
        notes: List[str] = list(target_notes)
        surveyed: Dict[int, pose.SurveyedMarker] = {}
        excluded: List[int] = []
        for tag_id in matched_ids:
            entry = aruco_by_id[tag_id]
            size_mm = definition.marker_size_mm(tag_id)
            if entry.height_cm != 0.0:
                excluded.append(tag_id)
                logger.info(
                    "calibrate(%s): marker %d is elevated (%.1f cm); ignored by the pose solve",
                    camera_slug, tag_id, entry.height_cm,
                )
                continue
            if size_mm is None:
                excluded.append(tag_id)
                notes.append(f"marker {tag_id} has no size; not used in the pose solve")
                continue
            surveyed[tag_id] = pose.SurveyedMarker(
                tag_id=tag_id, centre_cm=(entry.x, entry.y), size_mm=size_mm
            )
        used = sorted(surveyed)
        obs = [aruco_markers[i] for i in used]
        camera_matrix = np.array(lens.camera_matrix, dtype=np.float64)
        dist_coeffs = np.array(lens.dist_coeffs, dtype=np.float64)

        common: Dict[str, Any] = dict(
            matched_ids=matched_ids,
            missing_ids=(),
            lens_model_source=LENS_SOURCE_MEASURED,
            lens_warning=lens_warning,
            method="pose",
            method_reason=(
                "measured lens model and marker sizes" if target_corners is None
                else "measured lens model; pose: "
                + ("markers+target" if used else "target")
            ),
            helpful_missing_ids=helpful_missing,
        )

        def failure(reason: str, **metrics: Any) -> CalibrationResult:
            worst_id = metrics.get("leave_one_out_worst_id")
            if worst_id is not None and f"marker {worst_id}" not in reason:
                # An aggregate gate tripped first: still name the marker the
                # other markers disagree with most, so a mis-surveyed or
                # swapped marker is identified, not just an rms.
                reason += (
                    f" (worst leave-one-out: marker {worst_id}, "
                    f"{metrics['leave_one_out_worst_cm']:.2f} cm from where the others place it)"
                )
            return CalibrationResult(
                succeeded=False,
                reason=f"pose calibration rejected: {reason}",
                notes=tuple(notes),
                **common,
                **metrics,
            )

        try:
            rotations: Dict[int, pose.MarkerRotation] = {}
            turns: Dict[int, int] = {}
            loo: Dict[int, float] = {}
            obj = np.zeros((0, 3), dtype=np.float64)
            img = np.zeros((0, 2), dtype=np.float64)
            if used:
                rotations = pose.detect_marker_rotations(obs, surveyed, camera_matrix, dist_coeffs)
                turns = {i: rotations[i].quarter_turns for i in used}
                obj, img = pose.correspondences(obs, surveyed, turns)
                loo = pose.leave_one_out_residuals(obs, surveyed, turns, camera_matrix, dist_coeffs)
            if target_corners is not None:
                obj = np.vstack([obj, target_corners[0]])
                img = np.vstack([img, target_corners[1]])
            planar = pose.solve_planar_pose(obj, img, camera_matrix, dist_coeffs)
        except ValueError as exc:
            return failure(f"pose solve failed: {exc}")

        height, nadir, tilt = pose.camera_position_from_pose(planar)
        err_cm = pose.reprojection_error_cm(planar, camera_matrix, dist_coeffs, obj, img)
        rms_cm = float(np.sqrt(np.mean(err_cm ** 2)))
        if used:
            local, glob = pose.marker_distances(obs, surveyed, planar, camera_matrix, dist_coeffs)
        else:
            local, glob = [], []
        scale = (
            gates.scale_agreement(local, glob) if used and len(local) == len(used) else None
        )
        if target_corners is not None and not used:
            notes.append(
                "scale agreement not applicable: the calibration target is the only scale"
            )
        spread = gates.angular_spread_deg(pose.viewing_rays(img, camera_matrix, dist_coeffs))
        # Two points are trivially collinear, so the layout gate speaks only
        # for three or more markers; the two-marker case is already labelled
        # ``reduced`` with its own note about the poorly constrained camera.
        centres = np.array([surveyed[i].centre_cm for i in used], dtype=np.float64)
        extent = gates.layout_extent_ratio(centres) if len(used) >= 3 else None
        worst = gates.per_marker_leave_one_out(loo)
        metrics = gates.GateMetrics(
            rms_px=planar.rms_px,
            rms_cm=rms_cm,
            scale_ratio=scale,
            angular_spread_deg=spread,
            ambiguity_ratio=planar.ambiguity_ratio,
            layout_extent_ratio=extent,
            loo_worst=worst,
            rotation_ambiguous_ids=tuple(
                i for i in used if rotations[i].ratio < gates.MIN_ROTATION_RATIO
            ),
        )
        measured: Dict[str, Any] = dict(
            reprojection_error_cm=rms_cm,
            corner_count=int(len(obj)),
            reprojection_error_px=planar.rms_px,
            scale_agreement=scale,
            angular_spread_deg=spread,
            camera_height_cm=height,
            camera_nadir=nadir,
            camera_tilt_deg=tilt,
            leave_one_out_worst_id=worst[0] if worst else None,
            leave_one_out_worst_cm=worst[1] if worst else None,
            layout_extent_ratio=extent,
            leave_one_out_cm=tuple(sorted((int(k), float(v)) for k, v in loo.items())),
        )
        label, gate_notes, fail_reason = gates.classify(
            used,
            [i for i in required if i not in excluded],
            [i for i in helpful if i not in excluded],
            metrics,
            has_target=target_corners is not None,
        )
        notes.extend(gate_notes)
        if fail_reason is None and rms_cm > self._max_reprojection_error_cm:
            fail_reason = (
                f"corner reprojection rms {rms_cm:.3f} cm exceeds the "
                f"{self._max_reprojection_error_cm:.3f} cm acceptance threshold"
            )
        if fail_reason is not None:
            return failure(fail_reason, **measured)
        if len(used) == 2:
            # Two markers fix the field mapping but leave the camera's roll
            # about their baseline weakly constrained: never better than reduced.
            if label == gates.LABEL_FULL:
                label = gates.LABEL_REDUCED
            notes.append("two markers: camera position is poorly constrained")

        homography = pose.homography_from_pose(planar, camera_matrix)
        calibration = Calibration(
            stale=False,
            calibrated_at=time.time(),
            playfield=definition.name,
            homography=_matrix_to_tuple(homography),
            matched_ids=matched_ids,
            reprojection_error_cm=rms_cm,
            camera_matrix=lens.camera_matrix,
            dist_coeffs=lens.dist_coeffs,
            lens_source=LENS_SOURCE_MEASURED,
            method="pose",
            confidence=label,
            camera_height_cm=height,
            camera_nadir_x_cm=nadir.x,
            camera_nadir_y_cm=nadir.y,
            camera_tilt_deg=tilt,
            reprojection_error_px=planar.rms_px,
            corner_count=int(len(obj)),
            scale_agreement=scale,
        )
        with self._lock:
            self._calibrations[camera_slug] = calibration
        return CalibrationResult(
            succeeded=True, confidence=label, notes=tuple(notes), **common, **measured
        )

    #: Fewest distinct target corners a burst must yield to be solved from.
    MIN_TARGET_CORNERS = 8

    def _observe_target_burst(
        self, frames: Sequence[np.ndarray], target: Any
    ) -> Tuple[Optional[Tuple[np.ndarray, np.ndarray]], Optional[str]]:
        """Run the injected target observer over *frames*, averaging each
        board corner's pixel across the frames it was seen in.

        Returns ``((obj (N, 3), img (N, 2)), None)`` -- field cm with z = 0 and
        averaged raw pixels -- or ``(None, why)`` when the board was not found
        or too few corners were read. A corner is keyed by its field position
        (the observer's output is deterministic per corner id).
        """
        assert self._observe_target is not None
        accum: Dict[Tuple[float, float], List[Tuple[float, float]]] = {}
        try:
            for frame in frames:
                gray = (
                    cv.cvtColor(frame, cv.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
                )
                for c in self._observe_target(gray, target):
                    key = (round(float(c.field_cm[0]), 4), round(float(c.field_cm[1]), 4))
                    accum.setdefault(key, []).append((float(c.pixel.x), float(c.pixel.y)))
        except ValueError as exc:
            return None, f"target could not be read: {exc}"
        if not accum:
            return None, "the calibration board was not found in the frame"
        if len(accum) < self.MIN_TARGET_CORNERS:
            return None, (
                f"only {len(accum)} board corners read (need at least "
                f"{self.MIN_TARGET_CORNERS})"
            )
        keys = sorted(accum)
        obj = np.array([[k[0], k[1], 0.0] for k in keys], dtype=np.float64)
        img = np.array([np.mean(accum[k], axis=0) for k in keys], dtype=np.float64)
        return (obj, img), None

    def _calibrate_by_centres(
        self,
        *,
        camera_slug: str,
        definition: _ConcretePlayfieldDefinition,
        image_size: Tuple[int, int],
        aruco_detected: Dict[int, PixelPoint],
        matched_ids: Tuple[int, ...],
        helpful_missing: Tuple[int, ...],
        stored_lens: Optional[LensModel],
        lens_warning: Optional[str],
        method_reason: str,
    ) -> CalibrationResult:
        """The centre-point strategy: homography from marker centres alone
        (DLT over surveyed positions), unchanged by sprint 045 apart from
        reporting ``method``/``method_reason``."""
        extra: Dict[str, Any] = dict(
            method="centre-point",
            method_reason=method_reason,
            helpful_missing_ids=helpful_missing,
            notes=(
                (f"helpful marker(s) not seen: {list(helpful_missing)}",)
                if helpful_missing else ()
            ),
        )
        aruco_by_id = {entry.tag_id: entry for entry in definition.aruco_tags}

        # Only the coplanar (height_cm == 0) subset of the matched ArUco ids
        # feeds the homography solve and reprojection_error_cm: an elevated
        # point is off the field plane and would corrupt the solve the way
        # unmodelled parallax does.
        homography_ids = tuple(i for i in matched_ids if aruco_by_id[i].height_cm == 0.0)

        # Lens-model carry-forward (sprint 030 ticket 001, Design Rationale
        # Decision 3): the measured lens at this frame size wins; else
        # whatever this camera's *prior* successful calibration held is
        # carried forward unchanged (a legacy or 'estimated' file keeps its
        # source); ``None`` reproduces the intrinsics-free behaviour. Reads
        # ``self._get_prior_calibration``, not ``self.get_calibration``
        # directly -- see that attribute's docstring (restart carry-forward).
        camera_matrix: Optional[Tuple[_Row3, _Row3, _Row3]]
        dist_coeffs: Optional[Tuple[float, ...]]
        lens_source: Optional[str]
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
            **extra,
            succeeded=False,
                matched_ids=matched_ids,
                missing_ids=(),
                reason=degenerate,
                lens_model_source=lens_model_source,
                lens_warning=lens_warning,
            )

        homography = _solve_homography(pixel_pts, world_pts)
        if homography is None:
            return CalibrationResult(
            **extra,
            succeeded=False,
                matched_ids=matched_ids,
                missing_ids=(),
                reason="homography solve failed: singular or ill-conditioned matrix",
                lens_model_source=lens_model_source,
                lens_warning=lens_warning,
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
            **extra,
            succeeded=False,
                matched_ids=matched_ids,
                missing_ids=(),
                reprojection_error_cm=rms,
                reason=(
                    f"reprojection error {rms:.3f} cm exceeds the "
                    f"{self._max_reprojection_error_cm:.3f} cm acceptance threshold"
                ),
                lens_model_source=lens_model_source,
                lens_warning=lens_warning,
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
            **extra,
            succeeded=True,
            matched_ids=matched_ids,
            missing_ids=(),
            reprojection_error_cm=rms,
            lens_model_source=lens_model_source,
            lens_warning=lens_warning,
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
        """Multi-frame averaged ArUco centre detection: run every frame
        through the same preprocess-then-detect pipeline
        ``daemon/detection.py`` uses, and average each id's detected pixel
        centre across every frame it appeared in. An id missing from some
        frames but present in others still averages over however many
        detections it actually got (mirrors ``archive/.../homography.py``'s
        ``detect_all_tags``). Keyed by family (always just ``ARUCO``); a
        burst with no detections returns an empty dict.

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
                if detection.family is not TagFamily.ARUCO:
                    continue  # AprilTags played no part in calibration
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
