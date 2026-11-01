"""The "NO SIGNAL" test card shown in place of an absent camera's frames.

Daemon-only: this module imports ``cv2``/``numpy`` at top level, so it
must never be imported from ``client/``, ``view/``, ``mcp/`` or ``cli/``
(``tests/test_no_cv2_in_client_modules.py`` guards those packages). The
daemon renders the card; clients only ever receive encoded frames.

The geometry follows the classic RCA Indian-head test pattern -- big
circle with concentric rings, four corner circles, converging-line
resolution wedges with numbered scale marks, a gray-scale step bar, and a
crosshair over a fine grid -- minus the caricature head. The camera's
number, name and "NO SIGNAL" sit in the centre disc instead.

The pattern is laid out in a 4:3 box (height ``h``, width ``4h/3``)
centred in the requested frame; for wider frames the grid simply
continues into the side margins. Monochrome, like the original.

Rendering is deterministic (no randomness, no clock) and cached per
``(width, height, number, name)``; cached arrays are read-only and
shared, so callers that need to draw on one must ``.copy()`` it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

from aprilcam.daemon.interfaces import PlaceholderRenderer

#: Size used when neither a configured nor a last-opened size is known.
DEFAULT_SIZE: Tuple[int, int] = (1280, 720)

#: Number of bars in the gray-scale step strip (black to white).
GRAY_STEPS = 10

#: Cached cards kept (LRU). A card is a few MB at 1080p.
CACHE_SIZE = 16

_FONT = cv2.FONT_HERSHEY_DUPLEX
_AA = cv2.LINE_AA

Color = Tuple[int, int, int]


def _g(v: int) -> Color:
    """A BGR color for gray level ``v``."""
    return (int(v), int(v), int(v))


# Gray levels.
_BG = _g(96)
_GRID = _g(168)
_BLACK = _g(12)
_WHITE = _g(245)
_DISC = _g(30)
_DISC_CROSS = _g(70)
_NAME_GRAY = _g(200)


def resolve_size(
    configured: Optional[Tuple[int, int]],
    last_opened: Optional[Tuple[int, int]],
) -> Tuple[int, int]:
    """Pick the card size: configured frame size, else last-opened, else 1280x720."""
    for size in (configured, last_opened):
        if size is not None and size[0] > 0 and size[1] > 0:
            return int(size[0]), int(size[1])
    return DEFAULT_SIZE


def gray_step_rect(width: int, height: int) -> Tuple[int, int, int, int]:
    """Pixel rect ``(x0, y0, x1, y1)`` of the gray-scale step strip."""
    h = height if width * 3 >= height * 4 else width * 3 // 4
    cx = width // 2
    half = int(0.19 * h)
    y0 = height // 2 + int(0.395 * h)
    y1 = height // 2 + int(0.47 * h)
    return cx - half, y0, cx + half, y1


class CvPlaceholderRenderer(PlaceholderRenderer):
    """OpenCV implementation of :class:`PlaceholderRenderer` with an LRU cache."""

    def __init__(self, cache_size: int = CACHE_SIZE) -> None:
        self._cache: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def render(self, width: int, height: int, number: int, name: str) -> np.ndarray:
        if width <= 0 or height <= 0:
            raise ValueError(f"test card size must be positive, got {width}x{height}")
        key = (int(width), int(height), int(number), str(name))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        card = _draw_card(*key)
        card.setflags(write=False)
        with self._lock:
            self._cache[key] = card
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return card


# ---------------------------------------------------------------- drawing


def _draw_card(width: int, height: int, number: int, name: str) -> np.ndarray:
    img = np.empty((height, width, 3), np.uint8)
    img[:] = _BG
    h = height if width * 3 >= height * 4 else width * 3 // 4  # pattern height
    pw = h * 4 // 3
    cx, cy = width // 2, height // 2
    ox, oy = cx - pw // 2, cy - h // 2

    def px(v: float) -> int:
        return int(round(v * h))

    # Fine grid across the whole frame, centred so the crosshair is a grid line.
    pitch = max(8, h // 12)
    thin = max(1, h // 480)
    for x in range(cx % pitch, width, pitch):
        cv2.line(img, (x, 0), (x, height - 1), _GRID, thin)
    for y in range(cy % pitch, height, pitch):
        cv2.line(img, (0, y), (width - 1, y), _GRID, thin)

    # Pattern frame: black/white keyline around the 4:3 box.
    kl = max(2, h // 120)
    cv2.rectangle(img, (ox, oy), (ox + pw - 1, oy + h - 1), _BLACK, kl * 2)
    cv2.rectangle(img, (ox + kl, oy + kl), (ox + pw - 1 - kl, oy + h - 1 - kl), _WHITE, max(1, kl // 2))

    # Crosshair through the whole pattern.
    ch = max(1, h // 300)
    cv2.line(img, (ox, cy), (ox + pw - 1, cy), _BLACK, ch, _AA)
    cv2.line(img, (cx, oy), (cx, oy + h - 1), _BLACK, ch, _AA)

    # Corner circles with rings and diagonal crosses.
    r_corner = px(0.105)
    for sx in (-1, 1):
        for sy in (-1, 1):
            _bullseye(img, cx + sx * (pw // 2 - r_corner - kl * 3), cy + sy * (h // 2 - r_corner - kl * 3), r_corner, h)

    # Big centre circle: concentric rings around a dark label disc.
    r_big = px(0.32)
    _rings(img, cx, cy, r_big, h)
    r_disc = px(0.238)
    cv2.circle(img, (cx, cy), r_disc, _DISC, -1, _AA)
    cv2.circle(img, (cx, cy), r_disc, _WHITE, max(1, h // 240), _AA)
    # Crosshair continues faintly inside the disc, behind the text.
    cv2.line(img, (cx - r_disc, cy), (cx + r_disc, cy), _DISC_CROSS, ch, _AA)
    cv2.line(img, (cx, cy - r_disc), (cx, cy + r_disc), _DISC_CROSS, ch, _AA)

    # Resolution wedges: horizontal-resolution fans left/right, vertical top.
    wedge_len = px(0.205)
    wedge_half = px(0.15)
    gap = px(0.015)
    for side in (-1, 1):
        x_near = cx + side * (r_big + gap + px(0.012))
        x_far = x_near + side * wedge_len
        _wedge_fan(img, x_near, x_far, cy, wedge_half, h, vertical_lines=True)
    _wedge_fan_horizontal_top(img, cx, oy, h, px)

    # Gray-scale steps along the bottom.
    x0, y0, x1, y1 = gray_step_rect(width, height)
    _gray_steps(img, x0, y0, x1, y1, h)

    # Label.
    _draw_label(img, cx, cy, r_disc, number, name, h)

    return img


def _bullseye(img: np.ndarray, x: int, y: int, r: int, h: int) -> None:
    cv2.circle(img, (x, y), r, _WHITE, -1, _AA)
    n = 4
    for i in range(n):
        ri = r * (n - i) // n
        cv2.circle(img, (x, y), ri, _BLACK if i % 2 == 0 else _WHITE, -1, _AA)
    cv2.circle(img, (x, y), r, _BLACK, max(1, h // 240), _AA)
    d = int(r * 0.7071)
    t = max(1, h // 400)
    cv2.line(img, (x - d, y - d), (x + d, y + d), _GRID, t, _AA)
    cv2.line(img, (x - d, y + d), (x + d, y - d), _GRID, t, _AA)


def _rings(img: np.ndarray, cx: int, cy: int, r: int, h: int) -> None:
    """Outer white band, black keyline, then alternating thin rings."""
    cv2.circle(img, (cx, cy), r + max(2, h // 90), _BLACK, -1, _AA)
    cv2.circle(img, (cx, cy), r, _WHITE, -1, _AA)
    n = 7
    r_in = int(r * 0.74)
    for i in range(n):
        ri = r - (r - r_in) * i // n
        cv2.circle(img, (cx, cy), ri, _BLACK if i % 2 else _WHITE, -1, _AA)
    cv2.circle(img, (cx, cy), r_in, _WHITE, max(1, h // 200), _AA)
    # Dotted radial ticks every 10 degrees just outside the ring.
    r_t0, r_t1 = r + max(2, h // 90) + 2, r + max(2, h // 90) + 2 + max(3, h // 100)
    for k in range(36):
        a = np.deg2rad(k * 10.0)
        c, s = np.cos(a), np.sin(a)
        cv2.line(
            img,
            (int(round(cx + r_t0 * c)), int(round(cy + r_t0 * s))),
            (int(round(cx + r_t1 * c)), int(round(cy + r_t1 * s))),
            _BLACK,
            max(1, h // 400),
            _AA,
        )


def _wedge_fan(img: np.ndarray, x_near: int, x_far: int, cy: int, half: int, h: int, vertical_lines: bool) -> None:
    """Fan of horizontal black wedges converging toward ``x_near`` (resolution wedge).

    Wide spacing at the far end, converging at the near end, with numbered
    scale marks (resolution in hundreds of TV lines) along the lower edge.
    """
    xa, xb = sorted((x_near, x_far))
    top, bot = cy - half, cy + half
    cv2.rectangle(img, (xa, top), (xb, bot), _WHITE, -1)
    n = 9
    pitch_far = (2 * half) / n
    conv = 0.12  # spacing at the near end, relative to the far end
    for i in range(n):
        # edges of black wedge i at the far and near ends
        far_c = top + (i + 0.5) * pitch_far
        near_c = cy + (far_c - cy) * conv
        far_h = pitch_far * 0.25
        near_h = far_h * conv
        p_near_x, p_far_x = x_near, x_far
        poly = np.array(
            [
                [p_near_x, near_c - near_h],
                [p_far_x, far_c - far_h],
                [p_far_x, far_c + far_h],
                [p_near_x, near_c + near_h],
            ],
            np.float32,
        )
        cv2.fillConvexPoly(img, np.round(poly * 16).astype(np.int32), _BLACK, _AA, 4)
    cv2.rectangle(img, (xa, top), (xb, bot), _BLACK, max(1, h // 240), _AA)
    # Scale marks: ticks + numerals below the box, spaced along its length.
    labels = ["2", "3", "4", "5", "6"]
    fs = max(0.3, h / 1500.0)
    th = max(1, h // 500)
    span = xb - xa
    for k, lab in enumerate(labels):
        frac = (k + 0.5) / len(labels)
        x = int(xa + frac * span) if x_far > x_near else int(xb - frac * span)
        cv2.line(img, (x, bot), (x, bot + max(3, h // 70)), _BLACK, th, _AA)
        (tw, tht), _ = cv2.getTextSize(lab, _FONT, fs, th)
        cv2.putText(img, lab, (x - tw // 2, bot + max(3, h // 70) + tht + 3), _FONT, fs, _BLACK, th, _AA)


def _wedge_fan_horizontal_top(img: np.ndarray, cx: int, oy: int, h: int, px: Callable[[float], int]) -> None:
    """Vertical-resolution wedge (converging vertical-ish lines) above the big circle."""
    half_w = px(0.17)
    top = oy + px(0.03)
    bot = oy + px(0.105)
    cv2.rectangle(img, (cx - half_w, top), (cx + half_w, bot), _WHITE, -1)
    n = 15
    pitch_far = (2 * half_w) / n
    conv = 0.12
    for i in range(n):
        far_c = cx - half_w + (i + 0.5) * pitch_far
        near_c = cx + (far_c - cx) * conv
        far_h = pitch_far * 0.25
        near_h = far_h * conv
        poly = np.array(
            [
                [near_c - near_h, bot],
                [far_c - far_h, top],
                [far_c + far_h, top],
                [near_c + near_h, bot],
            ],
            np.float32,
        )
        cv2.fillConvexPoly(img, np.round(poly * 16).astype(np.int32), _BLACK, _AA, 4)
    cv2.rectangle(img, (cx - half_w, top), (cx + half_w, bot), _BLACK, max(1, h // 240), _AA)


def _gray_steps(img: np.ndarray, x0: int, y0: int, x1: int, y1: int, h: int) -> None:
    n = GRAY_STEPS
    w = x1 - x0
    for i in range(n):
        v = int(round(255 * i / (n - 1)))
        xa = x0 + w * i // n
        xb = x0 + w * (i + 1) // n
        cv2.rectangle(img, (xa, y0), (xb - 1, y1), _g(v), -1)
    cv2.rectangle(img, (x0, y0), (x1 - 1, y1), _BLACK, max(1, h // 240))


def _fit_text(text: str, max_w: int, scale: float, thick: int) -> Tuple[str, float]:
    """Shrink (then ellipsise) ``text`` so it is no wider than ``max_w`` px."""
    s = scale
    while s > 0.25:
        (tw, _), _ = cv2.getTextSize(text, _FONT, s, thick)
        if tw <= max_w:
            return text, s
        s *= 0.92
    t = text
    while len(t) > 1:
        t = t[:-1]
        cand = t.rstrip() + "..."
        (tw, _), _ = cv2.getTextSize(cand, _FONT, s, thick)
        if tw <= max_w:
            return cand, s
    return text[:1], s


def _centered(img: np.ndarray, text: str, cx: int, cy: int, scale: float, thick: int, color: Color) -> None:
    (tw, th), base = cv2.getTextSize(text, _FONT, scale, thick)
    org = (cx - tw // 2, cy + th // 2)
    cv2.putText(img, text, org, _FONT, scale, _BLACK, thick + max(2, thick), _AA)  # dark halo
    cv2.putText(img, text, org, _FONT, scale, color, thick, _AA)


def _draw_label(img: np.ndarray, cx: int, cy: int, r_disc: int, number: int, name: str, h: int) -> None:
    max_w = int(r_disc * 1.7)
    unit = h / 480.0
    # (text, font scale, vertical offset as a fraction of the disc radius, gray)
    lines = [
        (f"CAMERA {number}", 0.95 * unit, -0.50, _WHITE),
        ("NO SIGNAL", 1.35 * unit, 0.0, _WHITE),
    ]
    if name:
        lines.append((name, 0.7 * unit, 0.50, _NAME_GRAY))
    for text, scale, dy, color in lines:
        thick = max(1, int(round(scale * 1.6)))
        text, fitted = _fit_text(text, max_w, scale, thick)
        thick = max(1, int(round(fitted * 1.6)))
        _centered(img, text, cx, cy + int(dy * r_disc), fitted, thick, color)
