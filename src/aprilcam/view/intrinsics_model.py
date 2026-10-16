"""Pure display model for the ChArUco intrinsics workflow (sprint 044).

One :class:`~aprilcam.intrinsics.interfaces.IntrinsicsStatus` per camera is
the only input every renderer reads. This module turns those records into
display strings and a tile model with **pure functions** -- no Tk, no cv2,
no I/O -- so tests exercise the rendering rules directly and the text
(``--no-gui``) and Tk renderers cannot drift apart: the text lines are
built *from* the view model.

The thresholds quoted in the display strings mirror the daemon's
(``intrinsics.solver``); they are repeated here only as display text because
the client must not import the cv2-bearing solver. The daemon's ``good``
flag stays the sole authority on "good enough".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from aprilcam.intrinsics.interfaces import (
    BANNER_GOOD_ENOUGH,
    BANNER_KEEP_GOING,
    BANNER_SHOW_BOARD,
    IntrinsicsFinishResult,
    IntrinsicsStatus,
)

__all__ = [
    "CELL_ORDER",
    "TILT_ORDER",
    "TileState",
    "CameraViewModel",
    "IntrinsicsViewModel",
    "build_camera_model",
    "build_view_model",
    "status_text_lines",
    "view_model_text_lines",
    "finish_text_lines",
    "FontSizes",
    "EnterDecision",
    "font_sizes",
    "colour_for",
    "tile_colour",
    "key_action",
    "decide_enter",
    "exit_code_for",
    "connection_banner",
]

CELL_ORDER = ("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE")
TILT_ORDER = ("N", "E", "S", "W")

#: Display-only copies of the daemon's thresholds.
_GOOD_RESIDUAL_PX = 4
_MIN_VIEWS = 12
_MIN_SOLVE_VIEWS = 6

BANNER_TEXT = {
    BANNER_SHOW_BOARD: "SHOW ME THE BOARD",
    BANNER_KEEP_GOING: "KEEP GOING",
    BANNER_GOOD_ENOUGH: "GOOD ENOUGH",
}
#: Colour class per banner: renderers map these to concrete colours.
BANNER_CLASS = {
    BANNER_SHOW_BOARD: "dim",
    BANNER_KEEP_GOING: "yellow",
    BANNER_GOOD_ENOUGH: "green",
}
GOOD_HINT = "Press Enter to finish and install.  Or keep going to tighten it further."


@dataclass(frozen=True, kw_only=True)
class TileState:
    """One coverage tile: ``level`` is ``none`` (0 views), ``some`` (1) or ``good`` (2+)."""

    name: str
    count: int
    level: str


@dataclass(frozen=True, kw_only=True)
class CameraViewModel:
    camera: str
    views_line: str
    error_now: str
    could_change: str
    #: ``green``/``yellow``/``red``/``plain`` hints for the two lines above.
    error_class: str
    could_change_class: str
    cells: tuple[TileState, ...]
    tilts: tuple[TileState, ...]
    next_line: Optional[str]
    reject_note: Optional[str]
    errors: tuple[str, ...]
    good: bool
    banner_text: str
    banner_class: str


@dataclass(frozen=True, kw_only=True)
class IntrinsicsViewModel:
    banner_text: str
    banner_class: str
    cameras: tuple[CameraViewModel, ...]
    #: The one NEXT line (per-camera hints joined, at most two) or the good-enough prompt.
    next_line: str
    all_good: bool


def _level(n: int) -> str:
    return "good" if n >= 2 else ("some" if n == 1 else "none")


def _tiles(counts: dict, order: Sequence[str]) -> tuple[TileState, ...]:
    return tuple(
        TileState(name=k, count=int(counts.get(k, 0)), level=_level(int(counts.get(k, 0))))
        for k in order
    )


def build_camera_model(status: IntrinsicsStatus) -> CameraViewModel:
    """Display strings and tile states for one camera's status."""
    n = status.view_count
    if status.rms_px is None:
        if status.errors:
            error_now, error_class = "solve failed", "red"
        else:
            error_now = f"error: need {max(0, _MIN_SOLVE_VIEWS - n)} more views"
            error_class = "plain"
        could, could_class = "could still change by: --", "plain"
    else:
        error_now = f"error now: {status.rms_px:.2f} px"
        error_class = "green" if status.good else "yellow"
        if status.residual_px is None:
            could = f"could still change by: (need {max(0, _MIN_VIEWS - n)} more views)"
            could_class = "yellow"
        else:
            could = (f"could still change by: {status.residual_px:.1f} px   "
                     f"(good < {_GOOD_RESIDUAL_PX})")
            if status.correction_px is not None:
                could += f"      lens correction itself: {status.correction_px:.1f} px"
            could_class = "green" if status.residual_px < _GOOD_RESIDUAL_PX else "yellow"
    banner = status.banner if status.banner in BANNER_TEXT else BANNER_SHOW_BOARD
    return CameraViewModel(
        camera=status.camera,
        views_line=f"views: {n}   tilted: {status.tilted_count}",
        error_now=error_now,
        could_change=could,
        error_class=error_class,
        could_change_class=could_class,
        cells=_tiles(status.cell_coverage, CELL_ORDER),
        tilts=_tiles(status.tilt_coverage, TILT_ORDER),
        next_line=None if status.good else status.next_hint,
        reject_note=status.last_reject,
        errors=tuple(status.errors),
        good=status.good,
        banner_text=BANNER_TEXT[banner],
        banner_class=BANNER_CLASS[banner],
    )


def build_view_model(statuses: Sequence[IntrinsicsStatus]) -> IntrinsicsViewModel:
    """Combine one status per camera into the whole-window model.

    The overall banner is GOOD ENOUGH only when *every* camera is good;
    otherwise KEEP GOING once any camera has something, else SHOW ME THE BOARD.
    """
    cams = tuple(build_camera_model(s) for s in statuses)
    all_good = bool(cams) and all(c.good for c in cams)
    if all_good:
        key = BANNER_GOOD_ENOUGH
    elif any(s.banner != BANNER_SHOW_BOARD for s in statuses):
        key = BANNER_KEEP_GOING
    else:
        key = BANNER_SHOW_BOARD
    if all_good:
        next_line = GOOD_HINT
    else:
        multi = len(cams) > 1
        hints = [
            (f"CAM {c.camera}: {c.next_line}" if multi else c.next_line)
            for c in cams if not c.good and c.next_line
        ]
        next_line = "\n".join(hints[:2])
    return IntrinsicsViewModel(
        banner_text=BANNER_TEXT[key], banner_class=BANNER_CLASS[key],
        cameras=cams, next_line=next_line, all_good=all_good)


def _tile_row(tiles: Sequence[TileState]) -> str:
    return " ".join(f"{t.name}:{t.count if t.count else '.'}" for t in tiles)


def _camera_text(c: CameraViewModel) -> list[str]:
    lines = [
        f"[{c.camera}] {c.banner_text}   {c.views_line}",
        f"  {c.error_now}",
        f"  {c.could_change}",
    ]
    for i in range(0, 9, 3):
        lines.append("  coverage " + _tile_row(c.cells[i:i + 3]))
    lines.append("  tilt     " + _tile_row(c.tilts))
    if c.reject_note:
        lines.append(f"  note: {c.reject_note}")
    for e in c.errors:
        lines.append(f"  ERROR: {e}")
    return lines


def view_model_text_lines(model: IntrinsicsViewModel) -> list[str]:
    """Text-mode lines carrying every field of *model*."""
    lines = [f"=== {model.banner_text} ==="]
    for c in model.cameras:
        lines.extend(_camera_text(c))
    if model.next_line:
        for i, part in enumerate(model.next_line.split("\n")):
            lines.append(("NEXT: " if i == 0 else "      ") + part)
    return lines


def status_text_lines(statuses: Sequence[IntrinsicsStatus]) -> list[str]:
    """``statuses -> text lines`` (view model built then rendered)."""
    return view_model_text_lines(build_view_model(statuses))


def finish_text_lines(result: IntrinsicsFinishResult) -> list[str]:
    """The install message shared by the text and GUI modes."""
    s = result.status
    summary = []
    if s.rms_px is not None:
        summary.append(f"error {s.rms_px:.2f} px")
    if s.fx is not None:
        summary.append(f"fx {s.fx:.1f}")
    if s.hfov_deg is not None:
        summary.append(f"hfov {s.hfov_deg:.1f} deg")
    tail = f" ({', '.join(summary)})" if summary else ""
    if result.installed:
        return [
            f"[{s.camera}] lens model INSTALLED{tail}",
            f"  run aprilcam camera calibrate {s.camera} to apply",
        ]
    lines = [f"[{s.camera}] lens model NOT installed{tail}"]
    if result.reason:
        lines.append(f"  reason: {result.reason}")
    return lines


# ---------------------------------------------------------------------------
# GUI-facing pure helpers (sprint 044 ticket 008): sizes, colours, key rules.
# ---------------------------------------------------------------------------

#: Concrete colours per class hint (dark background, readable at distance).
_COLOURS = {
    "green": "#3ddc84",
    "yellow": "#ffd23f",
    "red": "#ff5555",
    "dim": "#8a8a8a",
    "plain": "#e8e8e8",
}
_TILE_COLOURS = {"none": "#333333", "some": "#b8860b", "good": "#2e9e5b"}


def colour_for(cls: str) -> str:
    """Hex colour for a ``green``/``yellow``/``red``/``dim``/``plain`` class."""
    return _COLOURS.get(cls, _COLOURS["plain"])


def tile_colour(level: str) -> str:
    """Hex fill for a coverage tile level (``none``/``some``/``good``)."""
    return _TILE_COLOURS.get(level, _TILE_COLOURS["none"])


@dataclass(frozen=True, kw_only=True)
class FontSizes:
    """Point sizes (pixels, negative-Tk style handled by the caller) for each text tier."""

    banner: int
    metric: int
    body: int
    tile: int
    next_line: int


def font_sizes(screen_height: int, camera_count: int = 1) -> FontSizes:
    """Scale every font from the screen height so the display reads from
    ~10 feet: the banner is about H/9. With several cameras side by side the
    per-camera tiers shrink a little (columns are narrower) but never below
    a floor."""
    h = max(int(screen_height), 200)
    shrink = 1.0 if camera_count <= 1 else (0.8 if camera_count == 2 else 0.65)
    return FontSizes(
        banner=max(h // 9, 20),
        metric=max(int(h / 14 * shrink), 14),
        body=max(int(h / 30 * shrink), 10),
        tile=max(int(h / 34 * shrink), 10),
        next_line=max(int(h / 22), 12),
    )


@dataclass(frozen=True, kw_only=True)
class EnterDecision:
    """What Enter does: ``finish`` the listed cameras, show a ``notice``
    (nothing good yet), or ``quit`` (nothing left to finish)."""

    action: str  # "finish" | "notice" | "quit"
    cameras: tuple[str, ...] = ()
    notice: Optional[str] = None


def key_action(keysym: str) -> Optional[str]:
    """Map a Tk keysym to ``enter``/``escape`` (or ``None``)."""
    if keysym in ("Return", "KP_Enter"):
        return "enter"
    if keysym == "Escape":
        return "escape"
    return None


def decide_enter(good_by_camera: dict) -> EnterDecision:
    """Enter, given ``{camera: good}`` for the cameras still owned (not yet
    installed). Only good cameras are ever finished -- never forced."""
    if not good_by_camera:
        return EnterDecision(action="quit")
    good = tuple(c for c, g in good_by_camera.items() if g)
    if not good:
        return EnterDecision(
            action="notice",
            notice="not GOOD ENOUGH yet -- keep going (Enter installs only a good result)")
    return EnterDecision(action="finish", cameras=good)


def exit_code_for(installed: Sequence[str], cameras: Sequence[str]) -> int:
    """0 only when every camera ended installed, else 1 (text-mode parity)."""
    return 0 if set(installed) == set(cameras) and cameras else 1


def connection_banner(error: Optional[str]) -> Optional[str]:
    """Error banner text for a failed poll, or ``None`` when healthy."""
    if not error:
        return None
    return f"DAEMON CONNECTION LOST -- {error}"
