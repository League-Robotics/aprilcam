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
    """

    homography: Tuple[_Row3, _Row3, _Row3]
    matched_ids: Tuple[int, ...]
    """ArUco ids the homography was solved from -- provenance, echoing
    the ``CalibrationResult.matched_ids`` this calibration succeeded
    with."""
    reprojection_error_cm: Optional[float] = None
    """RMS distance (cm) between each matched marker's surveyed world
    position and where the solved homography reprojects its detected
    pixel position -- informational (sprint.md Open Question 1: no
    acceptance threshold is enforced this sprint)."""

    def to_world(self, pixel: PixelPoint) -> WorldPoint:
        return geometry.pixel_to_world(self._matrix(), pixel)

    def to_pixel(self, world: WorldPoint) -> PixelPoint:
        return geometry.world_to_pixel(self._matrix(), world)

    def _matrix(self) -> np.ndarray:
        return np.array(self.homography, dtype=np.float64)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for ``calibration.json`` (daemon/service.py owns
        the atomic file write; this is just the shape)."""
        return {
            "stale": self.stale,
            "calibrated_at": self.calibrated_at,
            "playfield": self.playfield,
            "homography": [list(row) for row in self.homography],
            "matched_ids": list(self.matched_ids),
            "reprojection_error_cm": self.reprojection_error_cm,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Calibration":
        """The inverse of :meth:`to_dict` -- ``daemon/service.py``'s
        startup reload path. Raises ``KeyError``/``ValueError``/
        ``TypeError`` on a malformed record; the daemon treats a corrupt
        ``calibration.json`` as "no calibration held" rather than
        failing to start (mirrors ``camera.registry.CameraRegistry``'s
        "a corrupt file must never crash the daemon" stance)."""
        homography_raw = data["homography"]
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
    """

    def __init__(
        self,
        *,
        get_camera_config: CameraConfigLookup,
        get_playfield: PlayfieldLookup,
        capture_frames: FrameSource,
        detector_factory: DetectorFactory = _default_detector_factory,
        pipeline_config: Optional[PipelineConfig] = None,
    ) -> None:
        self._get_camera_config = get_camera_config
        self._get_playfield = get_playfield
        self._capture_frames = capture_frames
        self._pipeline_config = pipeline_config
        self._detector = detector_factory()
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

        calibration = Calibration(
            stale=False,
            calibrated_at=time.time(),
            playfield=definition.name,
            homography=_matrix_to_tuple(homography),
            matched_ids=matched_ids,
            reprojection_error_cm=rms,
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
