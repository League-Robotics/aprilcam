"""cv2-free value types for ChArUco intrinsics calibration.

Client-side code may import this module; it never imports cv2. Dictionary
names (``DICT_5X5_100``) stay plain strings here -- resolving them to an
OpenCV constant happens daemon-side in ``detector.py`` only.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import Optional

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
