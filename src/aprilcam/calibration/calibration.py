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

**Lens distortion (sprint 030 ticket 001).** ``Calibration`` gains
optional ``camera_matrix``/``dist_coeffs``; ``to_world``/``to_pixel``
undistort/redistort via ``geometry.undistort_point``/``distort_point``
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

from aprilcam.calibration import geometry
from aprilcam.calibration.interfaces import Calibration as _CalibrationBase
from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.calibration.interfaces import Calibrator as _CalibratorBase
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.interfaces import PlayfieldDefinition as PlayfieldDefinitionIface
from aprilcam.config.loader import PlayfieldDefinition as _ConcretePlayfieldDefinition
from aprilcam.errors import NoPlayfieldLinked
from aprilcam.types import PixelPoint, TagFamily, WorldPoint
from aprilcam.vision.detector import TagDetector
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


def _default_detector_factory() -> TagDetector:
    return TagDetector()


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

    def to_world(self, pixel: PixelPoint) -> WorldPoint:
        if self._has_intrinsics():
            pixel = geometry.undistort_point(
                pixel, self._camera_matrix_array(), self._dist_coeffs_array()
            )
        return geometry.pixel_to_world(self._matrix(), pixel)

    def to_pixel(self, world: WorldPoint) -> PixelPoint:
        pixel = geometry.world_to_pixel(self._matrix(), world)
        if self._has_intrinsics():
            pixel = geometry.distort_point(
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
        return cls(
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
    max_reprojection_error_cm:
        The reprojection-error acceptance ceiling (ticket 023-006) --
        defaults to the module-level :data:`MAX_REPROJECTION_ERROR_CM`
        (1.0 cm) but is genuinely overridable per instance, since the
        confirmed default is a domain-tuning call (sprint.md Decisions),
        not a structural one.
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
    ) -> None:
        self._get_camera_config = get_camera_config
        self._get_playfield = get_playfield
        self._capture_frames = capture_frames
        self._pipeline_config = pipeline_config
        self._detector = detector_factory()
        self._max_reprojection_error_cm = max_reprojection_error_cm
        self._calibrations: Dict[str, Calibration] = {}
        self._lock = threading.Lock()

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

        averaged = self._detect_averaged(frames)

        required = definition.required_aruco_ids()
        matched_ids = tuple(sorted(i for i in required if i in averaged))
        missing_ids = tuple(sorted(i for i in required if i not in averaged))

        stray = sorted(set(averaged) - set(required))
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
                succeeded=False, matched_ids=matched_ids, missing_ids=missing_ids
            )

        # Lens-model carry-forward (sprint 030 ticket 001, Design
        # Rationale Decision 3): an ordinary recalibration (no
        # intrinsics estimation of its own -- that is ticket 002's
        # ``estimate_intrinsics``) neither estimates nor discards
        # whatever lens model this camera's *prior* successful
        # calibration already held; it carries it forward unchanged
        # onto the fresh ``Calibration`` this call produces. ``None``
        # (never calibrated before, or calibrated with no known lens
        # model) reproduces exactly today's intrinsics-free behaviour.
        prior = self.get_calibration(camera_slug)
        camera_matrix = prior.camera_matrix if prior is not None else None
        dist_coeffs = prior.dist_coeffs if prior is not None else None
        intrinsics_known = camera_matrix is not None and dist_coeffs is not None

        # Undistort-before-solve (Design Rationale Decision 1): when
        # this camera's lens model is known, every detected marker
        # pixel is undistorted *before* it ever reaches the homography
        # solve or the reprojection-error measurement below -- both
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
                tag_id: geometry.undistort_point(
                    averaged[tag_id], camera_matrix_array, dist_coeffs_array
                )
                for tag_id in matched_ids
            }
            pixel_pts = np.array(
                [[undistorted[i].x, undistorted[i].y] for i in matched_ids], dtype=np.float64
            )
        else:
            pixel_pts = np.array(
                [[averaged[i].x, averaged[i].y] for i in matched_ids], dtype=np.float64
            )
        world_pts = np.array(
            [[definition.aruco_position(i).x, definition.aruco_position(i).y] for i in matched_ids],
            dtype=np.float64,
        )

        degenerate = _degeneracy_reason(pixel_pts, world_pts)
        if degenerate is not None:
            return CalibrationResult(
                succeeded=False, matched_ids=matched_ids, missing_ids=(), reason=degenerate
            )

        homography = _solve_homography(pixel_pts, world_pts)
        if homography is None:
            return CalibrationResult(
                succeeded=False,
                matched_ids=matched_ids,
                missing_ids=(),
                reason="homography solve failed: singular or ill-conditioned matrix",
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
        )
        with self._lock:
            self._calibrations[camera_slug] = calibration

        return CalibrationResult(
            succeeded=True, matched_ids=matched_ids, missing_ids=(), reprojection_error_cm=rms
        )

    def get_calibration(self, camera_slug: str) -> Optional[Calibration]:
        """The :class:`Calibration` a successful :meth:`calibrate` call
        for *camera_slug* most recently produced, or ``None`` if none
        has (yet) succeeded on this instance. See :meth:`calibrate`'s
        docstring for why this is additive rather than declared on
        ``calibration.interfaces.Calibrator``."""
        with self._lock:
            return self._calibrations.get(camera_slug)

    def _detect_averaged(self, frames: Sequence[np.ndarray]) -> Dict[int, PixelPoint]:
        """Multi-frame averaged ArUco corner detection: run every frame
        through the same preprocess-then-detect pipeline
        ``daemon/detection.py`` uses, keep only ArUco hits (AprilTags are
        calibration ground truth per spec but this ticket's acceptance
        gate is the ArUco-only ``required_aruco_ids()`` set -- see the
        module docstring), and average each id's detected pixel centre
        across every frame it appeared in. An id missing from some
        frames but present in others still averages over however many
        detections it actually got (mirrors
        ``archive/.../homography.py``'s ``detect_all_tags``).
        """
        accum: Dict[int, List[Tuple[float, float]]] = {}
        for frame in frames:
            gray, rescale = run_pipeline(frame, self._pipeline_config)
            for detection in self._detector.detect(gray, scale=rescale.scale):
                if detection.family != TagFamily.ARUCO:
                    continue
                accum.setdefault(detection.number, []).append(
                    (detection.center.x, detection.center.y)
                )
        return {
            tag_id: PixelPoint(
                x=float(np.mean([p[0] for p in pts])),
                y=float(np.mean([p[1] for p in pts])),
            )
            for tag_id, pts in accum.items()
        }
