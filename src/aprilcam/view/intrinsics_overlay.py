"""Pure Pillow composition of the intrinsics live image (sprint 046).

``compose_camera_image`` turns JPEG bytes plus the camera's view model into a
PIL image with translucent cell tints, a thick 40/20/40 grid, large compass
labels and the board outline. No Tk, no cv2, no I/O, so tests spot-check
pixels directly.
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageColor, ImageDraw, ImageFont

from aprilcam.view.intrinsics_model import (
    DIM_FACTOR,
    CameraViewModel,
    cell_overlays,
    fit_rect,
    grid_rects,
    scale_polygons,
)

__all__ = ["compose_camera_image", "OUTLINE_COLOUR", "GRID_COLOUR", "TINT_ALPHA"]

OUTLINE_COLOUR = "#00e5ff"
GRID_COLOUR = "#ffffff"
TINT_ALPHA = 90


def _font(size: int):
    size = max(int(size), 8)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow: fixed-size bitmap font
        return ImageFont.load_default()


def _dim(img: Image.Image) -> Image.Image:
    return Image.blend(img.convert("RGB"), Image.new("RGB", img.size, "#000000"), DIM_FACTOR)


def _blank(dest_size) -> Image.Image:
    x0, y0, x1, y1 = fit_rect((16, 9), (0, 0, int(dest_size[0]), int(dest_size[1])))
    return Image.new("RGB", (x1 - x0, y1 - y0), "#101418")


def compose_camera_image(
    jpeg_bytes: bytes,
    camera_model: CameraViewModel,
    board_outline,
    image_size,
    dest_size,
    *,
    dimmed: bool = False,
    fallback: Optional[Image.Image] = None,
    label_px: Optional[int] = None,
) -> Image.Image:
    """Return the live image, aspect-fitted into *dest_size* ``(w, h)``, with
    overlay. *image_size* is the native size the outline is expressed in
    (falls back to the decoded size). Never raises: a decode failure yields
    *fallback* (dimmed) or a dim blank."""
    try:
        src = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        native = image_size if image_size and image_size[0] > 0 and image_size[1] > 0 else src.size
        x0, y0, x1, y1 = fit_rect(src.size, (0, 0, int(dest_size[0]), int(dest_size[1])))
        w, h = x1 - x0, y1 - y0
        base = src.resize((w, h)).convert("RGBA")

        tint = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(tint)
        rects = grid_rects(w, h)
        overlays = cell_overlays(camera_model.cells)
        for rect, ov in zip(rects, overlays):
            if ov.tint:
                r, g, b = ImageColor.getrgb(ov.tint)
                tdraw.rectangle(rect, fill=(r, g, b, TINT_ALPHA))
        img = Image.alpha_composite(base, tint)

        draw = ImageDraw.Draw(img)
        line_w = max(3, w // 250)
        xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
        ys = sorted({r[1] for r in rects} | {r[3] for r in rects})
        for x in xs[1:-1]:
            draw.line([(x, 0), (x, h)], fill=GRID_COLOUR, width=line_w)
        for y in ys[1:-1]:
            draw.line([(0, y), (w, y)], fill=GRID_COLOUR, width=line_w)

        font = _font(min(label_px, h // 7) if label_px else max(h // 14, 14))
        for rect, ov in zip(rects, overlays):
            cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
            draw.text((cx, cy), ov.label, font=font, fill="#ffffff", anchor="mm",
                      stroke_width=max(2, h // 220), stroke_fill="#000000")

        out_w = max(4, w // 160)
        for poly in scale_polygons(board_outline, native, (0, 0, w, h)):
            if len(poly) >= 2:
                pts = list(poly) + [poly[0]]
                draw.line(pts, fill=OUTLINE_COLOUR, width=out_w, joint="curve")

        result = img.convert("RGB")
        return _dim(result) if dimmed else result
    except Exception:  # noqa: BLE001 - never raise into Tk
        if fallback is not None:
            return _dim(fallback)
        return _dim(_blank(dest_size))
