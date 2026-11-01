"""Tests for the NO SIGNAL test-card renderer (sprint 047 ticket 004)."""

from __future__ import annotations

import numpy as np
import pytest

from aprilcam.daemon.interfaces import PlaceholderRenderer
from aprilcam.daemon.testcard import (
    DEFAULT_SIZE,
    GRAY_STEPS,
    CvPlaceholderRenderer,
    gray_step_rect,
    resolve_size,
)

SIZES = [(640, 480), (1280, 720), (1280, 800), (1920, 1080)]


@pytest.fixture()
def renderer() -> CvPlaceholderRenderer:
    return CvPlaceholderRenderer()


def _gray(card: np.ndarray) -> np.ndarray:
    return card[:, :, 0].astype(int)


def _transitions(line: np.ndarray, threshold: int = 100) -> int:
    binary = line > threshold
    return int(np.count_nonzero(binary[1:] != binary[:-1]))


def test_base_class_raises() -> None:
    with pytest.raises(NotImplementedError):
        PlaceholderRenderer().render(10, 10, 0, "x")
    assert isinstance(CvPlaceholderRenderer(), PlaceholderRenderer)


@pytest.mark.parametrize("size", SIZES + [(321, 777), (200, 100)])
def test_exact_size_bgr_uint8(renderer: CvPlaceholderRenderer, size: tuple) -> None:
    card = renderer.render(size[0], size[1], 1, "Cam")
    assert card.shape == (size[1], size[0], 3)
    assert card.dtype == np.uint8
    assert np.array_equal(card[:, :, 0], card[:, :, 1])  # monochrome


def test_invalid_size(renderer: CvPlaceholderRenderer) -> None:
    with pytest.raises(ValueError):
        renderer.render(0, 480, 1, "x")


def test_deterministic_across_instances() -> None:
    a = CvPlaceholderRenderer().render(1280, 800, 3, "Logitech BRIO")
    b = CvPlaceholderRenderer().render(1280, 800, 3, "Logitech BRIO")
    assert np.array_equal(a, b)


def test_cache_reuse_and_readonly(renderer: CvPlaceholderRenderer) -> None:
    a = renderer.render(640, 480, 1, "A")
    assert renderer.render(640, 480, 1, "A") is a
    assert not a.flags.writeable
    assert renderer.render(640, 480, 2, "A") is not a
    assert renderer.render(640, 481, 1, "A") is not a


def test_cache_is_bounded() -> None:
    r = CvPlaceholderRenderer(cache_size=2)
    first = r.render(100, 100, 1, "a")
    r.render(100, 100, 2, "a")
    r.render(100, 100, 3, "a")
    assert r.render(100, 100, 1, "a") is not first  # evicted, re-rendered
    assert np.array_equal(r.render(100, 100, 1, "a"), first)


@pytest.mark.parametrize("size", SIZES)
def test_gray_steps_monotonic(renderer: CvPlaceholderRenderer, size: tuple) -> None:
    card = _gray(renderer.render(size[0], size[1], 1, "Cam"))
    x0, y0, x1, y1 = gray_step_rect(*size)
    ym = (y0 + y1) // 2
    w = x1 - x0
    vals = [card[ym, x0 + w * (2 * i + 1) // (2 * GRAY_STEPS)] for i in range(GRAY_STEPS)]
    assert vals == sorted(vals)
    assert len(set(vals)) == GRAY_STEPS
    assert vals[0] < 30 and vals[-1] > 230


@pytest.mark.parametrize("size", SIZES)
def test_rings_wedges_crosshair(renderer: CvPlaceholderRenderer, size: tuple) -> None:
    w, h = size
    card = _gray(renderer.render(w, h, 1, "Cam"))
    cx, cy = w // 2, h // 2
    ph = h if w * 3 >= h * 4 else w * 3 // 4
    # Rings: many black/white transitions walking right from the disc edge.
    ring_row = card[cy - 3, cx + int(0.24 * ph) : cx + int(0.34 * ph)]
    assert _transitions(ring_row) >= 6
    # Wedges: a column through the left wedge crosses many black/white bars.
    wedge_col = card[cy - int(0.14 * ph) : cy + int(0.14 * ph), cx - int(0.46 * ph)]
    assert _transitions(wedge_col) >= 6
    # Crosshair: the centre line outside every feature is darker than its neighbours.
    x = cx + int(0.62 * ph)  # between wedge and frame edge, on the horizontal line
    assert card[cy, x] < card[cy - 4 * max(1, ph // 240), x] - 40


@pytest.mark.parametrize("size", SIZES)
def test_label_present_and_varies(renderer: CvPlaceholderRenderer, size: tuple) -> None:
    w, h = size
    cx, cy = w // 2, h // 2
    ph = h if w * 3 >= h * 4 else w * 3 // 4
    r = int(0.2 * ph)
    box = lambda c: c[cy - r : cy + r, cx - r : cx + r, 0]  # noqa: E731
    a = renderer.render(w, h, 1, "Alpha")
    b = renderer.render(w, h, 2, "Beta")
    assert box(a).std() > 20  # not a uniform disc: text is there
    assert (box(a) > 150).sum() > 50  # bright glyph pixels
    assert not np.array_equal(box(a), box(b))
    # The label sits inside the disc only: corners of the frame are identical.
    assert np.array_equal(a[:20, :20], b[:20, :20])


def test_long_name_does_not_overflow_disc(renderer: CvPlaceholderRenderer) -> None:
    card = renderer.render(640, 480, 1, "A" * 200)
    assert card.shape == (480, 640, 3)


def test_resolve_size_chain() -> None:
    assert resolve_size((800, 600), (1920, 1080)) == (800, 600)
    assert resolve_size(None, (1920, 1080)) == (1920, 1080)
    assert resolve_size(None, None) == DEFAULT_SIZE == (1280, 720)
    assert resolve_size((0, 0), (640, 480)) == (640, 480)
