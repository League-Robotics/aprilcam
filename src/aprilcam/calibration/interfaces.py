"""Calibration: the pixel ↔ world mapping.

**Daemon-only** (spec §4.4). In v1 the CLI and MCP server ran this math
locally with their own OpenCV import, which broke the vision boundary; in
v2 they trigger it over RPC and report the result.

The invariants below are the ones ``docs/design/specification.md`` §7
calls sacrosanct. They cost enormous effort to get right, and every sign
and axis in a change here is guilty until proven innocent.

See ``aprilcam.daemon.interfaces``'s module docstring for the interface
design rule applied below: ``CalibrationResult`` and ``Calibration`` are
value-shaped, so they are frozen ``@dataclass``\\ es declaring fields
(``Calibration.to_world``/``to_pixel`` are real behaviour over those
fields, so they stay methods -- not properties -- with
``NotImplementedError`` bodies until a sprint implements them).
``Calibrator`` and ``Deskewer`` are behavioral, so they are plain base
classes. No concrete implementation of any of these four exists yet
(calibration is a later sprint); converting now means that sprint
derives from a real class instead of duck-typing against a shape.

**Flat-field gain-map capture (sprint 037).** ``FlatFieldCapturer`` is
the same "behavioral -> plain base class" treatment as ``Calibrator``;
``FlatFieldResult`` and ``FlatFieldInfo`` are the same "value-shaped ->
frozen dataclass declaring fields" treatment as ``CalibrationResult``/
``Calibration``. ``FlatFieldResult`` carries a numpy ``gain`` array
directly (unlike ``Calibration.homography``'s plain-tuple workaround)
because it is never compared for equality the way a stored, reloaded
``Calibration`` is -- there is no ``flatfield.json``-round-trip
equality check anywhere in this codebase, only ``.gain is not None``
truthiness and per-field spread/reason assertions in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from aprilcam.types import PixelPoint, WorldPoint


@dataclass(frozen=True, kw_only=True)
class MarkerObservation:
    """One fiducial marker as observed over a calibration burst (sprint 045).

    Unlike the bare centre ``PixelPoint`` the centre-point path keeps, this
    retains the marker's four corners, each averaged across the burst's
    frames, so the pose solve (which needs the marker's apparent size and
    orientation, not just its centre) has its input. cv2-free: plain
    ``PixelPoint`` values and floats only.
    """

    tag_id: int
    centre: PixelPoint
    """Mean of the per-frame detected centres, in the original frame's pixels."""
    corners: tuple[PixelPoint, PixelPoint, PixelPoint, PixelPoint]
    """Per-corner mean in ArUco order (TL, TR, BR, BL) **of the marker's own
    frame**, exactly as the detector reports it: a marker printed rotated or
    upside down keeps its own corner numbering (never renumbered by image
    position), because the pose solve reads orientation from this order."""
    frames: int
    """How many frames of the burst this marker appeared in."""
    corner_spread_px: float
    """Max over the four corners of the across-frame standard deviation
    (root of x-variance plus y-variance), in pixels -- a cheap noise measure;
    0.0 for a single-frame observation."""


@dataclass(frozen=True, kw_only=True)
class CalibrationResult:
    """The outcome of a calibration attempt — explicitly good or bad.

    There is **no partial calibration**. Either every ArUco fiducial the
    playfield definition specifies was found and a homography was
    computed, or the attempt failed and nothing was stored (UC-04).
    """

    succeeded: bool
    matched_ids: tuple[int, ...]
    """ArUco ids found and matched to surveyed positions."""
    missing_ids: tuple[int, ...]
    """Specified ids that were **not** found.

    Non-empty means failure. Reported by id rather than as a count so
    the developer knows which marker to uncover (UC-04.S-2) — "9 of
    10 found" is not actionable; "ArUco 6 missing" is.
    """
    reprojection_error_cm: Optional[float] = None
    """How far surveyed markers land from where they should.

    Resolved (UC-04, ticket 023-006): completeness is necessary but
    not sufficient. A complete-but-sloppy solve is still a bad solve,
    so ``calibration.calibration.Calibrator`` also enforces a maximum
    acceptable error (``MAX_REPROJECTION_ERROR_CM``, a named,
    overridable constant — confirmed default 1.0 cm). On a
    threshold rejection this field still carries the measured error
    that failed the check; ``reason`` names the threshold it was
    measured against.
    """
    reason: Optional[str] = None
    """Why a failure failed, when ids alone don't say.

    ``missing_ids`` covers the common case; this covers the rest —
    a complete-but-degenerate solve (collinear markers, singular
    homography) must name its problem, not report failure with
    every id present and leave the developer to guess.
    """
    lens_model_source: Optional[str] = None
    """Which lens model this call applied (sprint 044): ``"measured"``
    (per-camera lens store), ``"estimated"``, ``"legacy"`` (embedded in a
    pre-sprint-044 calibration) or ``"none"``. ``None`` when the attempt
    failed before the lens decision was made."""
    lens_warning: Optional[str] = None
    """A non-fatal lens note (sprint 044 ticket 010) -- e.g. the stored
    lens model was measured at a different resolution than the frames and
    was ignored. ``None`` when there is nothing to warn about."""
    # --- Sprint 045: pose provenance. All optional; filled by the Calibrator
    # (ticket 006), absent (None / empty) on results that predate the pose
    # solve or failed before it ran.
    method: Optional[str] = None
    """How the homography was solved: ``"pose"`` (planar pose from marker
    corners) or ``"centre-point"`` (the fallback fit to marker centres)."""
    method_reason: Optional[str] = None
    """Why ``"centre-point"`` was used instead of the pose solve."""
    confidence: Optional[str] = None
    """``"full"`` | ``"reduced"`` | ``"low"`` (``calibration.gates``)."""
    corner_count: Optional[int] = None
    """Marker corner points the pose solve used."""
    reprojection_error_px: Optional[float] = None
    """RMS corner reprojection error of the pose fit, in pixels."""
    scale_agreement: Optional[float] = None
    """Ratio of per-marker implied scales (1.0 = perfect agreement)."""
    angular_spread_deg: Optional[float] = None
    """Angular spread of the markers as seen from the camera."""
    camera_height_cm: Optional[float] = None
    """Pose-derived camera height above the playfield plane."""
    camera_nadir: Optional[WorldPoint] = None
    """Pose-derived point on the playfield directly beneath the camera."""
    camera_tilt_deg: Optional[float] = None
    """Pose-derived angle between the optical axis and the plane normal."""
    notes: Tuple[str, ...] = ()
    """Human-readable remarks from the solve and its gates."""
    helpful_missing_ids: Tuple[int, ...] = ()
    """Helpful (not required) marker ids that were not detected."""
    leave_one_out_worst_id: Optional[int] = None
    """Id of the marker the leave-one-out check found worst."""
    leave_one_out_worst_cm: Optional[float] = None
    """That marker's leave-one-out residual, in cm."""
    layout_extent_ratio: Optional[float] = None
    """Minor/major extent of the marker layout (low = nearly collinear)."""
    leave_one_out_cm: Tuple[Tuple[int, float], ...] = ()
    """Per-marker leave-one-out residual as ``((marker_id, cm), ...)`` sorted
    by id: how far each marker lands from where the pose fit to all the
    *others* places it. Empty below 3 markers or on a non-pose result."""


@dataclass(frozen=True, kw_only=True)
class Calibration:
    """A camera's established mapping between its pixels and the world.

    **File-backed**: written by the daemon to the state directory and
    reloaded at startup, so a calibrated camera stays calibrated across
    restarts (UC-01). This is why calibration survives while pushed
    config does not — the rule is file-backed vs in-memory, not
    config vs calibration.
    """

    stale: bool
    """Whether the camera or field has moved since calibrating.

    Advisory but important: stale means reported coordinates may be
    wrong while still looking entirely reasonable. Consumers must be
    told rather than silently served plausible garbage
    (UC-11.S-3) — the worst failure mode in the system.
    """
    calibrated_at: float
    """When this mapping was solved — provenance, not decoration.

    "Ready" without "as of when, against what" is how a January
    calibration silently serves a March field (UC-05.S-1).
    """
    playfield: str
    """The playfield definition this was solved against.

    The other half of provenance: replacing this definition is what
    marks the calibration stale (UC-02), so the calibration must
    name which definition it believes in.
    """
    lens_source: Optional[str] = None
    """Provenance of the lens embedded in this calibration (sprint 044
    ticket 010): ``"measured"`` | ``"estimated"`` | ``"legacy"``, ``None``
    when no lens is embedded. The matrix itself never crosses the wire."""
    lens_views: Optional[int] = None
    lens_rms_px: Optional[float] = None
    lens_residual_px: Optional[float] = None
    lens_installed_at: Optional[float] = None
    """Summary of a *measured* lens (board views, solve RMS, held-out
    residual, install time); ``None`` otherwise. Transient: filled in by
    the daemon when reporting, never persisted in ``calibration.json``."""
    # --- Sprint 045: persisted pose provenance (all None on a pre-045 file).
    method: Optional[str] = None
    """``"pose"`` | ``"centre-point"``; ``None`` for a pre-045 file."""
    confidence: Optional[str] = None
    """``"full"`` | ``"reduced"`` | ``"low"``."""
    camera_height_cm: Optional[float] = None
    camera_nadir_x_cm: Optional[float] = None
    camera_nadir_y_cm: Optional[float] = None
    camera_tilt_deg: Optional[float] = None
    """Pose-derived camera position (height above the plane, nadir point in
    world cm, tilt). Meaningful only when ``method == "pose"``."""
    reprojection_error_px: Optional[float] = None
    corner_count: Optional[int] = None
    scale_agreement: Optional[float] = None

    def camera_geometry(self) -> Optional[Tuple[WorldPoint, float]]:
        """The pose-derived camera position as ``(nadir, height_cm)``, or
        ``None``. Returned only for a *fresh* pose calibration: a
        centre-point or pre-045 calibration has no measured camera
        position, and a stale one describes where the camera *was*."""
        if self.method != "pose" or self.stale:
            return None
        if (self.camera_height_cm is None or self.camera_nadir_x_cm is None
                or self.camera_nadir_y_cm is None):
            return None
        return (
            WorldPoint(self.camera_nadir_x_cm, self.camera_nadir_y_cm),
            self.camera_height_cm,
        )

    def to_world(self, pixel: PixelPoint) -> WorldPoint:
        """Map a detection to the field, in cm.

        A1-centred, +x east, +y north. The origin shift is a **pure
        subtraction on both axes — never a y-flip**. Violating this
        silently mirrors the field north/south, which is the single
        easiest way to break everything downstream while producing
        plausible-looking numbers.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Calibration.to_world")

    def to_pixel(self, world: WorldPoint) -> PixelPoint:
        """Inverse of :meth:`to_world`, for rendering."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Calibration.to_pixel")


class Calibrator:
    """Runs calibration for a camera against its playfield.

    A plain base class, not ``Protocol`` — see ``daemon.interfaces``'s
    module docstring for the design rule.
    """

    def calibrate(self, camera_slug: str) -> CalibrationResult:
        """Detect fiducials, match to surveyed positions, solve.

        Requires the camera to be linked to a playfield: with no
        playfield there is no coordinate system to map to and **nothing
        to calibrate**, which is a precondition error rather than a
        calibration failure (UC-15.S-2).

        A failed attempt leaves any existing good calibration untouched.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Calibrator.calibrate")


class Deskewer:
    """Produces the top-down playfield image (UC-06, UC-19).

    A plain base class, not ``Protocol`` — see ``daemon.interfaces``'s
    module docstring for the design rule.

    Sizing is a **fixed bounding box**: the field rectangle is scaled
    aspect-preserved to fit (default 800×800, per-playfield override), so
    the output is uniform-scale by construction. ``main-playfield`` at
    134.3 × 89.3 cm yields 800 × 532.

    The resulting px-per-cm is **derived and reported, never chosen** —
    clients convert world ↔ pixel using published metadata rather than a
    constant anyone had to agree on.

    On the deskewed image **north is up and east is right**. Image rows
    grow downward, so world → pixel flips y exactly **once**, inside this
    mapping, and never again in client code.
    """

    def deskew(self, frame: np.ndarray) -> np.ndarray:
        raise NotImplementedError(f"{type(self).__name__} does not implement Deskewer.deskew")


@dataclass(frozen=True, kw_only=True)
class FlatFieldResult:
    """The outcome of one flat-field gain-map build attempt (sprint 037).

    Mirrors ``CalibrationResult``'s "explicit failure is data" contract:
    a failing quality-gate check never raises, it reports
    ``succeeded=False`` with :attr:`reason` naming the check, and the
    *measured* spread is still carried on both a passing and a refused
    attempt -- the same "report the number even on a threshold
    rejection" pattern ``CalibrationResult.reprojection_error_cm`` uses.
    """

    succeeded: bool
    spread_before: Tuple[float, ...]
    """Per-channel p98/p2 luminance ratio inside the mask, measured on
    the batch-averaged, *uncorrected* image -- one value per channel of
    the input frames (length 1 for a mono burst, 3 for colour)."""
    spread_after: Tuple[float, ...]
    """The same per-channel p98/p2 ratio, measured on the image the
    built gain map would produce (``avg * gain``) -- computed and
    reported even when the attempt is refused, so a caller can see how
    much the (unreturned) map would have helped."""
    reason: Optional[str] = None
    """Which quality-gate check(s) failed, and why -- ``None`` when
    every check passed. Non-``None`` on a forced pass too, naming what
    ``force=True`` overrode."""
    forced_checks: Tuple[str, ...] = ()
    """Which gate(s) ``force=True`` overrode to produce a
    ``succeeded=True`` result despite a failing check -- empty whenever
    no check failed, regardless of whether *force* was passed."""
    gain: Optional[np.ndarray] = None
    """The built per-pixel, per-channel gain map -- present only when
    :attr:`succeeded` is ``True`` (a clean pass, or a forced pass over
    :attr:`forced_checks`). ``None`` on an unforced refusal: nothing is
    returned for a caller to persist."""


@dataclass(frozen=True, kw_only=True)
class FlatFieldInfo:
    """The ``flatfield.json`` metadata-sidecar shape (sprint 037).

    ``cv2``-free by design, like ``Calibration``/``CalibrationResult`` --
    read by ``client``/``mcp`` code (e.g. a ``get_flatfield`` query) that
    must never need OpenCV to report whether a camera has a flat-field
    correction in place. Persistence itself (writing/reading the JSON
    file next to ``flatfield.npy``) is ``daemon/service.py``'s job, the
    same division ``Calibration.to_dict``/``from_dict`` already
    establishes for ``calibration.json``.
    """

    present: bool
    """Whether this camera currently has a gain map applied."""
    captured_at: Optional[float] = None
    """When the map was built, if known. ``None`` for a pre-sprint-037
    map with no sidecar (a one-time migration gap, never a fabricated
    timestamp -- see ``calibration/DESIGN.md``'s Open Questions)."""
    shape: Optional[Tuple[int, ...]] = None
    """The stored array's shape, e.g. ``(H, W, 3)`` or ``(H, W)``."""
    gain_min: Optional[float] = None
    gain_max: Optional[float] = None
    stale: bool = False
    """Whether the camera's mounting or lighting may have changed since
    this map was captured. Never inferred here -- set by whatever wrote
    the sidecar."""


class FlatFieldCapturer:
    """Builds a per-camera flat-field gain map from a frame burst
    (sprint 037).

    A plain base class, not ``Protocol`` -- see ``daemon.interfaces``'s
    module docstring for the design rule.

    Mirrors ``Calibrator``'s "camera frame access is handed in, never
    owned" invariant exactly: :meth:`build` takes an already-captured
    frame burst and a precomputed boolean field-interior mask as plain
    arguments -- it never opens a camera, never touches a filesystem,
    and never reaches into any daemon state. Pushing/restoring a
    temporary exposure override, capturing the burst, and deriving the
    mask (from a ``Calibration`` or ArUco detection) are all the
    caller's job (``daemon/service.py``), exactly like frame capture and
    playfield resolution are for ``Calibrator.calibrate``.
    """

    def build(self, frames: Sequence[np.ndarray], mask: np.ndarray, *, force: bool = False) -> FlatFieldResult:
        """Build and quality-gate a gain map from *frames* and *mask*.

        *frames* is a burst of already-captured raw frames (BGR or
        mono), all the same shape. *mask* is a boolean field-interior
        array the same height/width as each frame.

        Never raises for a failed quality-gate check -- reported via
        ``FlatFieldResult.succeeded=False``/``reason``, mirroring
        ``Calibrator.calibrate``'s own "explicit failure is data"
        contract. *force* overrides a failing check and still reports
        which one it overrode, via ``FlatFieldResult.forced_checks``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement FlatFieldCapturer.build")
