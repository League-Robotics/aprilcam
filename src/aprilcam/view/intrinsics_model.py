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

import math
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
    "blocking_reasons",
    "coverage_complete",
    "KEYS_LINE",
    "CONFIRM_WINDOW_S",
    "exit_code_for",
    "connection_banner",
    "GRID_EDGE_FRACTION",
    "DIM_FACTOR",
    "CellOverlay",
    "grid_rects",
    "cell_overlays",
    "tint_colour",
    "dim_colour",
    "scale_polygons",
    "fit_rect",
    "ColumnLayout",
    "column_layout",
    "fit_font_size",
    "panel_metric_sizes",
    "LONGEST_ERROR_NOW",
    "LONGEST_COULD_CHANGE",
    "PANEL_FRACTION",
]

CELL_ORDER = ("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE")
TILT_ORDER = ("N", "E", "S", "W")

#: Display-only copies of the daemon's thresholds.
_GOOD_RESIDUAL_PX = 4
_GOOD_RMS_PX = 0.6
_MIN_VIEWS = 12
_MIN_SOLVE_VIEWS = 6

#: Display-only banner for "every tile is green but the solve is not good":
#: more waving is unlikely to help, so say so instead of KEEP GOING.
_BANNER_ACCEPT = "accept"

BANNER_TEXT = {
    BANNER_SHOW_BOARD: "SHOW ME THE BOARD",
    BANNER_KEEP_GOING: "KEEP GOING",
    BANNER_GOOD_ENOUGH: "GOOD ENOUGH",
    _BANNER_ACCEPT: "ENTER TO ACCEPT",
}
#: Colour class per banner: renderers map these to concrete colours.
BANNER_CLASS = {
    BANNER_SHOW_BOARD: "dim",
    BANNER_KEEP_GOING: "yellow",
    BANNER_GOOD_ENOUGH: "green",
    _BANNER_ACCEPT: "yellow",
}
GOOD_HINT = "Press Enter to finish and install.  Or keep going to tighten it further."
#: Always on screen, so the operator is never stuck without a way out.
KEYS_LINE = "Enter: install (twice to accept a not-good result)    Esc: quit without saving"
#: Seconds a first not-good Enter stays armed for the confirming second press.
CONFIRM_WINDOW_S = 10.0
CONFIRM_NOTICE = ("NOT good enough -- press Enter again within 10 s to install it anyway "
                  "(Esc quits without saving)")


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
    #: Why a solved camera is not good yet, in threshold terms (empty when good).
    blockers: tuple[str, ...] = ()
    #: Every region and tilt direction has 2+ views (all tiles green).
    coverage_done: bool = False


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


def blocking_reasons(status: IntrinsicsStatus) -> tuple[str, ...]:
    """Why a solved camera is not GOOD ENOUGH, from the same thresholds the
    daemon's verdict uses. Empty when good or not yet solved."""
    if status.good or status.rms_px is None:
        return ()
    reasons = []
    if status.rms_px >= _GOOD_RMS_PX:
        reasons.append(f"error {status.rms_px:.2f} px (needs < {_GOOD_RMS_PX})")
    if status.residual_px is None:
        reasons.append(f"stability check needs {max(0, _MIN_VIEWS - status.view_count)} more views")
    elif status.residual_px >= _GOOD_RESIDUAL_PX:
        reasons.append(f"unstable: re-solves disagree by {status.residual_px:.1f} px "
                       f"(needs < {_GOOD_RESIDUAL_PX})")
    if not reasons:
        reasons.append("waiting for the daemon to accept the solve")
    return tuple(reasons)


def coverage_complete(status: IntrinsicsStatus) -> bool:
    """True when every region and tilt tile is green (2+ views each)."""
    return (all(int(status.cell_coverage.get(k, 0)) >= 2 for k in CELL_ORDER)
            and all(int(status.tilt_coverage.get(k, 0)) >= 2 for k in TILT_ORDER))


def build_camera_model(status: IntrinsicsStatus) -> CameraViewModel:
    """Display strings and tile states for one camera's status."""
    n = status.view_count
    if status.rms_px is None:
        if status.errors:
            error_now, error_class = "solve failed", "red"
        else:
            error_now = f"error: need {max(0, _MIN_SOLVE_VIEWS - n)} more views"
            error_class = "plain"
        could, could_class = "could move --", "plain"
    else:
        error_now = f"error now {status.rms_px:.2f} px (good < {_GOOD_RMS_PX})"
        error_class = "green" if status.good else "yellow"
        if status.residual_px is None:
            could = f"could move (need {max(0, _MIN_VIEWS - n)} more views)"
            could_class = "yellow"
        else:
            could = (f"could move {status.residual_px:.1f} px "
                     f"(good < {_GOOD_RESIDUAL_PX})")
            if status.correction_px is not None:
                could += f"   lens correction {status.correction_px:.1f} px"
            could_class = "green" if status.residual_px < _GOOD_RESIDUAL_PX else "yellow"
    banner = status.banner if status.banner in BANNER_TEXT else BANNER_SHOW_BOARD
    blockers = blocking_reasons(status)
    done = coverage_complete(status)
    next_line = None if status.good else status.next_hint
    if done and blockers:
        # All green yet not good: name what is failing instead of KEEP GOING.
        banner = _BANNER_ACCEPT
        next_line = ("Coverage done, but not good enough: " + "; ".join(blockers)
                     + ".  Enter twice installs it anyway.")
    return CameraViewModel(
        camera=status.camera,
        views_line=f"views: {n}   tilted: {status.tilted_count}",
        error_now=error_now,
        could_change=could,
        error_class=error_class,
        could_change_class=could_class,
        cells=_tiles(status.cell_coverage, CELL_ORDER),
        tilts=_tiles(status.tilt_coverage, TILT_ORDER),
        next_line=next_line,
        reject_note=status.last_reject,
        errors=tuple(status.errors),
        good=status.good,
        banner_text=BANNER_TEXT[banner],
        banner_class=BANNER_CLASS[banner],
        blockers=blockers,
        coverage_done=done,
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
    elif all(c.good or c.banner_text == BANNER_TEXT[_BANNER_ACCEPT] for c in cams):
        key = _BANNER_ACCEPT
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
    """What Enter does: ``finish`` the listed (good) cameras, ``confirm``
    (nothing good yet: arm a second press), ``force`` (the confirming second
    press: install the listed cameras as they are), or ``quit`` (nothing
    left to finish)."""

    action: str  # "finish" | "confirm" | "force" | "quit"
    cameras: tuple[str, ...] = ()
    notice: Optional[str] = None


def key_action(keysym: str) -> Optional[str]:
    """Map a Tk keysym to ``enter``/``escape`` (or ``None``)."""
    if keysym in ("Return", "KP_Enter"):
        return "enter"
    if keysym == "Escape":
        return "escape"
    return None


def decide_enter(good_by_camera: dict, armed: bool = False) -> EnterDecision:
    """Enter, given ``{camera: good}`` for the cameras still owned (not yet
    installed). Good cameras are finished normally. With none good, the
    first press only arms (``confirm``); a second press while *armed*
    forces every owned camera's install with whatever it has."""
    if not good_by_camera:
        return EnterDecision(action="quit")
    good = tuple(c for c, g in good_by_camera.items() if g)
    if good:
        return EnterDecision(action="finish", cameras=good)
    if armed:
        return EnterDecision(action="force", cameras=tuple(good_by_camera))
    return EnterDecision(action="confirm", notice=CONFIRM_NOTICE)


def exit_code_for(installed: Sequence[str], cameras: Sequence[str]) -> int:
    """0 only when every camera ended installed, else 1 (text-mode parity)."""
    return 0 if set(installed) == set(cameras) and cameras else 1


def connection_banner(error: Optional[str]) -> Optional[str]:
    """Error banner text for a failed poll, or ``None`` when healthy."""
    if not error:
        return None
    return f"DAEMON CONNECTION LOST -- {error}"


# ---------------------------------------------------------------------------
# Live-image overlay geometry (sprint 046): pure, no Tk / Pillow / cv2.
# ---------------------------------------------------------------------------

#: Display copy of ``intrinsics.collector.EDGE_FRACTION`` (the client must not
#: import the cv2-bearing collector); a parity test pins the two together.
GRID_EDGE_FRACTION = 1 / 3

#: Blend toward black applied to the overlay when the live image is stale/failed.
DIM_FACTOR = 0.4

_TINT_COLOURS = {"none": None, "some": "#ffbf00", "good": "#2ecc71"}

Rect = tuple  # (x0, y0, x1, y1)


def grid_rects(width: int, height: int) -> tuple:
    """Nine ``(x0, y0, x1, y1)`` rectangles tiling a ``width x height`` image
    in :data:`CELL_ORDER` (NW..SE): three equal bands per axis (thirds)."""
    w, h = max(int(width), 0), max(int(height), 0)
    xs = (0, int(round(GRID_EDGE_FRACTION * w)), int(round((1 - GRID_EDGE_FRACTION) * w)), w)
    ys = (0, int(round(GRID_EDGE_FRACTION * h)), int(round((1 - GRID_EDGE_FRACTION) * h)), h)
    # Rounding can only make two interior edges coincide on tiny images; keep
    # them monotone so the nine rects always tile exactly.
    xs = (xs[0], xs[1], max(xs[1], xs[2]), xs[3])
    ys = (ys[0], ys[1], max(ys[1], ys[2]), ys[3])
    return tuple(
        (xs[c], ys[r], xs[c + 1], ys[r + 1]) for r in range(3) for c in range(3)
    )


@dataclass(frozen=True, kw_only=True)
class CellOverlay:
    """Per-cell overlay: label text and tint (``None`` = no tint)."""

    name: str
    label: str
    level: str
    tint: Optional[str]


def tint_colour(level: str) -> Optional[str]:
    """Hex tint for a coverage level: none -> no tint, some -> amber, good -> green."""
    return _TINT_COLOURS.get(level)


def cell_overlays(cells: Sequence[TileState]) -> tuple:
    """Label and tint per cell, from the same ``TileState.level`` the tiles use."""
    return tuple(
        CellOverlay(name=c.name, label=f"{c.name} {c.count}", level=c.level,
                    tint=tint_colour(c.level))
        for c in cells
    )


def dim_colour(hex_colour: str, factor: float = DIM_FACTOR) -> str:
    """Blend a ``#rrggbb`` colour toward black by *factor* (0 = unchanged)."""
    h = hex_colour.lstrip("#")
    keep = 1.0 - factor
    r, g, b = (int(int(h[i:i + 2], 16) * keep) for i in (0, 2, 4))
    return f"#{r:02x}{g:02x}{b:02x}"


def fit_rect(image_size, column_rect) -> tuple:
    """Aspect-preserved, centred ``(x0, y0, x1, y1)`` for an image of
    *image_size* ``(w, h)`` inside *column_rect* (letterbox; scales up and
    down; same rule as ``viewer._fit_size``). Degenerate inputs yield a
    1x1 rectangle at the column's top-left."""
    cx0, cy0, cx1, cy1 = column_rect
    aw, ah = cx1 - cx0, cy1 - cy0
    try:
        sw, sh = image_size
    except (TypeError, ValueError):
        sw = sh = 0
    if sw <= 0 or sh <= 0 or aw <= 0 or ah <= 0:
        return (cx0, cy0, cx0 + 1, cy0 + 1)
    scale = min(aw / sw, ah / sh)
    w, h = max(int(sw * scale), 1), max(int(sh * scale), 1)
    x0 = cx0 + (aw - w) // 2
    y0 = cy0 + (ah - h) // 2
    return (x0, y0, x0 + w, y0 + h)


def scale_polygons(polygons, image_size, dest_rect) -> tuple:
    """Map native-pixel polygon points into *dest_rect*. ``None``, empty, or a
    degenerate *image_size* give ``()``."""
    if not polygons:
        return ()
    try:
        sw, sh = image_size
    except (TypeError, ValueError):
        return ()
    if sw <= 0 or sh <= 0:
        return ()
    x0, y0, x1, y1 = dest_rect
    fx, fy = (x1 - x0) / sw, (y1 - y0) / sh
    return tuple(
        tuple((x0 + px * fx, y0 + py * fy) for px, py in poly) for poly in polygons
    )


# ---------------------------------------------------------------------------
# Per-camera column geometry (fixed: independent of which notes exist).
# ---------------------------------------------------------------------------

#: Width of the right-hand panel as a fraction of the camera column.
PANEL_FRACTION = 0.30
_COLUMN_PAD = 12


@dataclass(frozen=True, kw_only=True)
class ColumnLayout:
    """Fixed slots for one camera column. ``image`` is the box the live image
    is aspect-fitted into; the rest are the right panel's slots, top to
    bottom. Every rect is ``(x0, y0, x1, y1)``."""

    image: tuple
    panel: tuple
    name: tuple
    error_now: tuple
    could_change: tuple
    tilts: tuple  # four rects, N E S W
    message: tuple


#: Longest strings the two metric slots can hold (``build_camera_model``).
LONGEST_ERROR_NOW = "error now 99.99 px (good < 0.6)"
LONGEST_COULD_CHANGE = "could move 99.9 px (good < 4)   lens correction 99.9 px"
#: Metric slots are sized for this many wrapped lines.
METRIC_MAX_LINES = 2
#: Average glyph width / font size for bold Helvetica, rounded up so the
#: estimate errs on "too wide" (word-wrap waste is covered by _WRAP_SLACK).
_CHAR_W_EM = 0.62
_WRAP_SLACK = 1.15
_MIN_FONT_PX = 8


def _text_lines(text: str, size: int, width: int) -> int:
    """Estimated wrapped line count of *text* at *size* px in *width* px."""
    if width <= 0:
        return 10**6
    return max(1, math.ceil(len(text) * size * _CHAR_W_EM * _WRAP_SLACK / width))


def fit_font_size(texts: Sequence[str], width: int, max_size: int,
                  max_lines: int = METRIC_MAX_LINES, min_size: int = _MIN_FONT_PX) -> int:
    """Largest font size (px, <= *max_size*) at which every text wraps to at
    most *max_lines* lines in *width* px, by a pure width estimate (so it is
    testable without a display). Never below *min_size*."""
    for size in range(max(int(max_size), min_size), min_size, -1):
        if all(_text_lines(t, size, width) <= max_lines for t in texts):
            return size
    return min_size


def panel_metric_sizes(panel_w: int, fs: FontSizes) -> tuple:
    """``(error_now_px, could_change_px)`` fonts for a panel *panel_w* wide:
    the largest sizes at which the longest expected strings take at most two
    lines. ``could_change`` never exceeds two thirds of the metric tier."""
    err = fit_font_size([LONGEST_ERROR_NOW], panel_w, fs.metric)
    could = fit_font_size([LONGEST_COULD_CHANGE], panel_w, fs.metric * 2 // 3)
    return err, could


def column_layout(x0: int, y: int, col_w: int, limit: int, fs: FontSizes) -> ColumnLayout:
    """Geometry of one camera column from its left edge *x0*, top *y*, width
    *col_w* and bottom *limit*. Depends only on those and the font sizes --
    never on notes, errors or frame state -- so nothing moves when they
    appear or disappear."""
    pad = _COLUMN_PAD
    panel_w = max(int(col_w * PANEL_FRACTION), 60)
    px1 = x0 + col_w - pad
    px0 = px1 - panel_w
    img = (x0 + pad, y, max(px0 - pad, x0 + pad + 1), max(limit, y + 1))
    ny = y
    name_h = 2 * (fs.body + 4)
    err_px, could_px = panel_metric_sizes(panel_w - 4, fs)
    # Each slot holds the lines it can wrap to (two) at its own font, plus
    # leading, so a wrapped second line can never run into the slot below.
    # (A panel too narrow even at the minimum font gets the extra lines too.)
    pw = panel_w - 4
    err_lines = max(METRIC_MAX_LINES, _text_lines(LONGEST_ERROR_NOW, err_px, pw))
    could_lines = max(METRIC_MAX_LINES, _text_lines(LONGEST_COULD_CHANGE, could_px, pw))
    err_h = int(err_lines * err_px * 1.3) + 4
    could_h = int(could_lines * could_px * 1.3) + 4
    tside = max(panel_w // 4, 18)
    msg_h = 2 * (fs.body + 6)
    name = (px0, ny, px1, ny + name_h)
    ny += name_h + 6
    err = (px0, ny, px1, ny + err_h)
    ny += err_h
    could = (px0, ny, px1, ny + could_h)
    ny += could_h + 6
    tilts = tuple((px0 + k * tside, ny, px0 + (k + 1) * tside, ny + tside) for k in range(4))
    ny += tside + 8
    msg = (px0, ny, px1, ny + msg_h)
    return ColumnLayout(image=img, panel=(px0, y, px1, ny + msg_h), name=name, error_now=err,
                        could_change=could, tilts=tilts, message=msg)
