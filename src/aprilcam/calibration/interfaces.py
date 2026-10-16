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
    intrinsics_estimated: Optional[bool] = None
    """Whether *this call* estimated a lens model (sprint 030 ticket
    002's ``estimate_intrinsics``) — ``True`` (estimated and stored
    this call), ``False`` (not estimated this call; any previously
    stored intrinsics carry forward unchanged, whether or not one
    existed), or ``None`` (the concept does not apply — the attempt
    failed before reaching that decision, e.g. a missing required
    ArUco id). Never the ``camera_matrix``/``dist_coeffs`` values
    themselves: mirrors ``get_calibration``'s existing "reports what
    the interface promises, never the internal homography matrix"
    precedent — an operator gets told *whether* intrinsics changed,
    not the numbers.
    """
    intrinsics_mode: Optional[str] = None
    """Which intrinsics-estimation model ran this call —
    ``"coplanar-constrained"`` (fixed principal point/aspect ratio,
    zero tangential distortion, fixed k3, k1-only) or ``"elevated"``
    (freed further once the linked playfield's surveyed elevated
    ground truth clears a documented count-and-z-spread threshold —
    ``calibration.calibration`` for the exact numbers and the evidence
    behind them). ``None`` whenever :attr:`intrinsics_estimated` is not
    ``True`` — a mode only "ran" on a call that actually estimated.
    """
    elevated_features_used: Optional[int] = None
    """How many surveyed-and-detected elevated (``height_cm > 0``)
    features contributed to this call's intrinsics estimation —
    reported so an operator who adds an elevated tag and sees no change
    in :attr:`intrinsics_mode` has been told something concrete rather
    than left to guess. ``None`` under the same condition as
    :attr:`intrinsics_mode`.
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
