"""cv2-free value types for ChArUco intrinsics calibration.

Client-side code may import this module; it never imports cv2. Dictionary
names (``DICT_5X5_100``) stay plain strings here -- resolving them to an
OpenCV constant happens daemon-side in ``detector.py`` only.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from typing import Optional

from aprilcam.errors import AprilCamError

#: Environment variable that overrides the default board spec.
BOARD_ENV_VAR = "APRILCAM_CHARUCO_BOARD"

#: Board used when neither the request nor the environment names one.
DEFAULT_BOARD_SPEC = "5x7:37:28:DICT_5X5_100"


class BoardLayout(str, enum.Enum):
    """Which OpenCV ChArUco layout a detector has locked onto."""

    UNKNOWN = "unknown"
    CURRENT = "current"
    LEGACY = "legacy"


@dataclass(frozen=True, kw_only=True)
class BoardSpec:
    """Physical ChArUco board: ``COLSxROWS:SQUARE_MM:MARKER_MM:DICT``."""

    cols: int
    rows: int
    square_mm: float
    marker_mm: float
    dict_name: str

    @classmethod
    def parse(cls, spec: str) -> "BoardSpec":
        """Parse ``'5x7:37:28:DICT_5X5_100'``; raise ``ValueError`` if malformed."""
        if not isinstance(spec, str):
            raise ValueError(f"board spec must be a string, got {type(spec).__name__}")
        fmt = "expected COLSxROWS:SQUARE_MM:MARKER_MM:DICT, e.g. " + DEFAULT_BOARD_SPEC
        parts = spec.strip().split(":")
        if len(parts) != 4:
            raise ValueError(f"malformed board spec {spec!r}: {fmt}")
        size, sq, mk, dname = (p.strip() for p in parts)
        try:
            cols_s, rows_s = size.lower().split("x")
            cols, rows = int(cols_s), int(rows_s)
            square_mm, marker_mm = float(sq), float(mk)
        except ValueError:
            raise ValueError(f"malformed board spec {spec!r}: {fmt}") from None
        if cols < 2 or rows < 2:
            raise ValueError(f"board spec {spec!r}: need at least 2x2 squares")
        if not (square_mm > 0 and marker_mm > 0):
            raise ValueError(f"board spec {spec!r}: square and marker sizes must be positive")
        if marker_mm >= square_mm:
            raise ValueError(f"board spec {spec!r}: marker size must be smaller than square size")
        if not dname.startswith("DICT_"):
            raise ValueError(f"board spec {spec!r}: dictionary must be a DICT_* name")
        return cls(cols=cols, rows=rows, square_mm=square_mm, marker_mm=marker_mm,
                   dict_name=dname)

    def __str__(self) -> str:
        return (f"{self.cols}x{self.rows}:{self.square_mm:g}:{self.marker_mm:g}:"
                f"{self.dict_name}")


def resolve_board_spec(request: Optional[str] = None) -> BoardSpec:
    """The one place board-spec precedence lives: request > env > default."""
    if request:
        return BoardSpec.parse(request)
    env = os.environ.get(BOARD_ENV_VAR)
    if env:
        return BoardSpec.parse(env)
    return BoardSpec.parse(DEFAULT_BOARD_SPEC)


@dataclass(frozen=True, kw_only=True)
class BoardDetection:
    """One physical board found in a frame; coordinates are native pixels."""

    corners: tuple[tuple[float, float], ...]
    ids: tuple[int, ...]
    marker_count: int
    layout: BoardLayout


class BoardDetectorBase:
    """Behavioral interface for board detectors (derive, do not duck-type)."""

    def detect(self, gray) -> list[BoardDetection]:
        raise NotImplementedError("BoardDetectorBase.detect")

    @property
    def layout(self) -> BoardLayout:
        raise NotImplementedError("BoardDetectorBase.layout")


class RejectReason(str, enum.Enum):
    """Machine-readable reason a detected board was not kept as a view."""

    MOVING = "moving"
    BLURRY = "blurry"
    NOT_NOVEL = "not_novel"
    AMBIGUOUS_POSE = "ambiguous_pose"
    TOO_FEW_CORNERS = "too_few_corners"


@dataclass(frozen=True, kw_only=True)
class ViewVerdict:
    """Collector decision for one board in one frame."""

    accepted: bool
    reason: Optional[RejectReason] = None
    corner_count: int = 0
    #: Degrees from fronto-parallel; ``None`` unless the pose is trusted.
    tilt_deg: Optional[float] = None
    azimuth_deg: Optional[float] = None
    pose_confident: bool = False
    #: Human-readable specifics for the reject (e.g. "blurry (0.31 of best)").
    detail: Optional[str] = None


@dataclass(frozen=True, kw_only=True)
class KeptView:
    """An accepted view: per-view corner data only, never a frame."""

    corners: tuple[tuple[float, float], ...]
    ids: tuple[int, ...]
    tilt_deg: Optional[float]
    azimuth_deg: Optional[float]
    #: Board extent in pixels: (x_min, y_min, x_max, y_max).
    extent: tuple[float, float, float, float]
    timestamp: float


@dataclass(frozen=True, kw_only=True)
class CoverageSummary:
    """Region and tilt-direction tallies over the kept views."""

    view_count: int
    #: 3x3 grid counts, row-major in NW, N, NE, W, C, E, SW, S, SE order.
    region_counts: tuple[int, ...]
    #: Trusted tilted views by direction: keys N, E, S, W.
    tilt_directions: dict
    tilted_count: int


# ---- solver results and the one status record -------------------------------

#: Banner states shown to the person holding the board.
BANNER_SHOW_BOARD = "show_board"
BANNER_KEEP_GOING = "keep_going"
BANNER_GOOD_ENOUGH = "good_enough"

#: Session states.
STATE_COLLECTING = "collecting"
STATE_FINISHED = "finished"
STATE_ABORTED = "aborted"
STATE_FAILED = "failed"


def derive_banner(*, board_visible: bool, view_count: int, good: bool) -> str:
    """The one place the banner is decided; sessions call this, renderers read it."""
    if good:
        return BANNER_GOOD_ENOUGH
    if view_count == 0 and not board_visible:
        return BANNER_SHOW_BOARD
    return BANNER_KEEP_GOING


@dataclass(frozen=True, kw_only=True)
class LensSolution:
    """Result of one ``cv2.calibrateCamera`` solve (k3 fixed)."""

    camera_matrix: tuple[tuple[float, ...], ...]
    dist_coeffs: tuple[float, ...]
    image_size: tuple[int, int]
    rms_px: float
    fx: float
    hfov_deg: float
    view_count: int
    #: ``None`` when plausible, else why the solve was rejected.
    plausibility_error: Optional[str] = None


@dataclass(frozen=True, kw_only=True)
class LensEvaluation:
    """Good-enough verdict for a set of views."""

    solution: LensSolution
    rms_px: float
    #: Half-sample mapping residual; ``None`` below MIN_VIEWS.
    residual_px: Optional[float]
    #: Size of the undistortion correction vs no lens model.
    correction_px: float
    good: bool


@dataclass(frozen=True, kw_only=True)
class IntrinsicsStatus:
    """The single status record every renderer (TUI, GUI, MCP, CLI) reads."""

    camera: str
    state: str = STATE_COLLECTING
    board_spec: Optional[str] = None
    layout: BoardLayout = BoardLayout.UNKNOWN
    image_size: Optional[tuple[int, int]] = None
    board_visible: bool = False
    view_count: int = 0
    tilted_count: int = 0
    #: 3x3 region counts keyed NW, N, NE, W, C, E, SW, S, SE.
    cell_coverage: dict = field(default_factory=dict)
    #: Trusted tilted views keyed N, E, S, W.
    tilt_coverage: dict = field(default_factory=dict)
    rms_px: Optional[float] = None
    residual_px: Optional[float] = None
    correction_px: Optional[float] = None
    fx: Optional[float] = None
    hfov_deg: Optional[float] = None
    #: Latched verdict (hysteresis over solves): what the banner, finish and
    #: ``--install`` use.
    good: bool = False
    #: Raw verdict of the most recent solve, before hysteresis.
    good_now: Optional[bool] = None
    banner: str = BANNER_SHOW_BOARD
    next_hint: Optional[str] = None
    last_reject: Optional[str] = None
    errors: tuple[str, ...] = ()
    started_at: float = 0.0
    solved_at: Optional[float] = None
    #: One convex hull per board currently detected, in native image pixels:
    #: a tuple of polygons, each 3+ (x, y) points. ``None`` when no board is
    #: visible (so a renderer never draws a stale outline).
    board_outline: Optional[tuple[tuple[tuple[float, float], ...], ...]] = None


@dataclass(frozen=True, kw_only=True)
class IntrinsicsFinishResult:
    """Outcome of finishing a session: did a lens model get installed."""

    installed: bool
    reason: Optional[str]
    status: IntrinsicsStatus
    #: Summary of the solved lens (matrix, distortion, fx, hfov, rms...), if any.
    lens: Optional[dict] = None


# ---- session errors ---------------------------------------------------------


class IntrinsicsError(AprilCamError):
    """Base class for intrinsics-session failures."""


class IntrinsicsSessionActive(IntrinsicsError):
    """A session is already running on this camera; finish or abort it first."""

    def __init__(self, camera: str) -> None:
        self.camera = camera
        super().__init__(
            f"an intrinsics session is already active on camera {camera!r}; "
            "finish or abort it before starting another")


class NoIntrinsicsSession(IntrinsicsError):
    """No session (active or recently ended) exists for this camera."""

    def __init__(self, camera: str) -> None:
        self.camera = camera
        super().__init__(f"no intrinsics session for camera {camera!r}")
