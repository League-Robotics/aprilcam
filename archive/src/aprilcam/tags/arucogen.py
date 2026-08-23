from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import cv2 as cv
import numpy as np


POSITIONS = {
    0: "Upper Left",
    1: "Upper RIght",
    2: "Lower left",
    3: "Lower Right",
}


def render_aruco_marker(marker_id: int, size: int = 400) -> np.ndarray:
    # Use ArUco 4x4 dictionary (50 markers)
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    # Prefer generateImageMarker if available
    if hasattr(cv.aruco, "generateImageMarker"):
        img_gray = cv.aruco.generateImageMarker(dictionary, marker_id, size)
    else:
        img_gray = np.zeros((size, size), dtype=np.uint8)
        cv.aruco.drawMarker(dictionary, marker_id, size, img_gray, 1)
    return cv.cvtColor(img_gray, cv.COLOR_GRAY2BGR)


def add_labels_below(img: np.ndarray, id_text: str, extra_text: Optional[str] = None,
                     margin: int = 20, font_scale: float = 1.0, thickness: int = 2) -> np.ndarray:
    # Measure both lines (ID and optional extra)
    (w1, h1), base1 = cv.getTextSize(id_text, cv.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    if extra_text:
        (w2, h2), base2 = cv.getTextSize(extra_text, cv.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        text_height = h1 + base1 + 8 + h2
        text_width = max(w1, w2)
    else:
        (w2, h2, base2) = (0, 0, 0)
        text_height = h1
        text_width = w1

    pad = 12
    new_h = img.shape[0] + margin + text_height + base1 + pad
    new_w = max(img.shape[1], text_width + 2 * pad)
    out = np.full((new_h, new_w, 3), 255, dtype=np.uint8)
    # Center marker
    x0 = (new_w - img.shape[1]) // 2
    out[0:img.shape[0], x0:x0 + img.shape[1]] = img
    # Draw ID line
    x_id = (new_w - w1) // 2
    y_id = img.shape[0] + margin + h1
    cv.putText(out, id_text, (x_id, y_id), cv.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness, cv.LINE_AA)
    # Draw extra line if present
    if extra_text:
        x_ex = (new_w - w2) // 2
        y_ex = y_id + 8 + h2
        cv.putText(out, extra_text, (x_ex, y_ex), cv.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness, cv.LINE_AA)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate 4x4 ArUco markers (0..15) with labels.")
    parser.add_argument("--out-dir", default="aruco-tab-images", help="Output directory (default: aruco-tab-images)")
    parser.add_argument("--size", type=int, default=400, help="Marker square size in pixels (default: 400)")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for marker_id in range(16):
        marker = render_aruco_marker(marker_id, size=int(args.size))
        id_text = f"ID: {marker_id}"
        extra = POSITIONS.get(marker_id)
        labeled = add_labels_below(marker, id_text, extra_text=extra, margin=20, font_scale=1.0, thickness=2)
        out_path = out_dir / f"aruco4x4_{marker_id}.png"
        cv.imwrite(str(out_path), labeled)

    print(f"Wrote 16 markers to {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
