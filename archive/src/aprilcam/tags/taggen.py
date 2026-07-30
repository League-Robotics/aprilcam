"""Generate AprilTag 36h11 or ArUco 4x4 marker images (PDF or PNG)."""

from __future__ import annotations

import argparse
import re
import tempfile
from pathlib import Path
from typing import Optional

import cv2 as cv
import numpy as np


# ---------------------------------------------------------------------------
# ID parsing
# ---------------------------------------------------------------------------

def parse_ids(spec: str) -> list[int]:
    """Parse a comma-separated ID spec like ``"0-3,7,10-12"`` into a sorted
    list of unique ints.  Ranges are inclusive on both ends.
    """
    ids: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            ids.update(range(lo, hi + 1))
        else:
            ids.add(int(part))
    return sorted(ids)


# ---------------------------------------------------------------------------
# Tag rendering
# ---------------------------------------------------------------------------

def render_tag(tag_id: int, family: str = "36h11", size: int = 800) -> np.ndarray:
    """Render a single tag as a **grayscale** ndarray.

    *family* must be ``"36h11"`` or ``"aruco4x4"``.
    """
    if family == "36h11":
        dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_APRILTAG_36h11)
    elif family == "aruco4x4":
        dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    else:
        raise ValueError(f"Unknown family {family!r}; expected '36h11' or 'aruco4x4'")

    if hasattr(cv.aruco, "generateImageMarker"):
        img_gray = cv.aruco.generateImageMarker(dictionary, tag_id, size)
    else:
        img_gray = np.zeros((size, size), dtype=np.uint8)
        cv.aruco.drawMarker(dictionary, tag_id, size, img_gray, 1)
    return img_gray


# ---------------------------------------------------------------------------
# Quiet-zone helpers
# ---------------------------------------------------------------------------

def add_quiet_zone_gray(img: np.ndarray, quiet_ratio: float = 2.0 / 7.0) -> np.ndarray:
    """Add a white border to a **grayscale** image."""
    if quiet_ratio <= 0:
        return img
    h, w = img.shape[:2]
    pad = max(1, int(round(min(h, w) * float(quiet_ratio))))
    out = np.full((h + 2 * pad, w + 2 * pad), 255, dtype=np.uint8)
    out[pad:pad + h, pad:pad + w] = img
    return out


def add_quiet_zone(img: np.ndarray, quiet_ratio: float) -> np.ndarray:
    """Add a white border to a **BGR** image (kept for PNG mode)."""
    if quiet_ratio <= 0:
        return img
    h, w = img.shape[:2]
    pad = max(1, int(round(min(h, w) * float(quiet_ratio))))
    out = np.full((h + 2 * pad, w + 2 * pad, 3), 255, dtype=np.uint8)
    out[pad:pad + h, pad:pad + w] = img
    return out


# ---------------------------------------------------------------------------
# Label helper (PNG mode)
# ---------------------------------------------------------------------------

def add_label_below(
    img: np.ndarray,
    label: str,
    margin: int = 20,
    font_scale: float = 1.0,
    thickness: int = 2,
) -> np.ndarray:
    """Append a text label below a BGR image (PNG mode only)."""
    (w, h), baseline = cv.getTextSize(
        label, cv.FONT_HERSHEY_SIMPLEX, font_scale, thickness,
    )
    pad = 10
    new_h = img.shape[0] + margin + h + baseline + pad
    new_w = max(img.shape[1], w + 2 * pad)
    out = np.full((new_h, new_w, 3), 255, dtype=np.uint8)
    x_tag = (new_w - img.shape[1]) // 2
    out[0:img.shape[0], x_tag:x_tag + img.shape[1]] = img
    x_text = (new_w - w) // 2
    y_text = img.shape[0] + margin + h
    cv.putText(
        out, label, (x_text, y_text),
        cv.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness, cv.LINE_AA,
    )
    return out


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

ARUCO_POSITIONS = {
    0: "Upper Left",
    1: "Upper Right",
    2: "Lower Left",
    3: "Lower Right",
}


def _family_label(family: str, tag_id: int) -> str:
    if family == "36h11":
        return f"AprilTag 36h11 ID: {tag_id}"
    pos = ARUCO_POSITIONS.get(tag_id)
    if pos:
        return f"ArUco 4x4 ID: {tag_id} - {pos}"
    return f"ArUco 4x4 ID: {tag_id}"


def generate_pdf(
    tag_ids: list[int],
    family: str = "36h11",
    size: int = 800,
    output_path: str | Path = "tags.pdf",
) -> Path:
    """Create a multi-page PDF with one tag per page.

    Page size: 59 mm x 102 mm.  Tag is centered with quiet zone;
    a label is printed below.
    """
    from fpdf import FPDF  # lazy import keeps startup fast

    PAGE_W_MM = 59.0
    PAGE_H_MM = 102.0

    pdf = FPDF(unit="mm", format=(PAGE_W_MM, PAGE_H_MM))
    pdf.set_auto_page_break(auto=False)

    for tag_id in tag_ids:
        pdf.add_page()

        # Render tag with quiet zone
        gray = render_tag(tag_id, family=family, size=size)
        # Minimal quiet zone for PDF — just enough for detection
        gray = add_quiet_zone_gray(gray, quiet_ratio=0.04)

        # Write to a temporary PNG so fpdf can embed it
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        cv.imwrite(tmp.name, gray)
        tmp.close()

        # Calculate placement — leave room for label at bottom
        label = _family_label(family, tag_id)
        label_h_mm = 6.0  # approximate height reserved for label text
        margin_mm = 1.0
        avail_w = PAGE_W_MM - 2 * margin_mm
        avail_h = PAGE_H_MM - 2 * margin_mm - label_h_mm

        # Fill the page width. The tag image includes quiet zone, so the
        # inner tag will be tag_dim / (1 + 2*quiet_ratio). With the page
        # at 59mm and 1mm margins, avail_w=57mm. Use a smaller quiet zone
        # for PDF so the tag fills the page: quiet_ratio ~0.04 gives a
        # 2mm border, leaving ~53mm for the inner tag.
        tag_dim = min(avail_w, avail_h)
        x = (PAGE_W_MM - tag_dim) / 2
        y = margin_mm + (avail_h - tag_dim) / 2

        pdf.image(tmp.name, x=x, y=y, w=tag_dim, h=tag_dim)

        # Label — right below the tag image
        pdf.set_font("Helvetica", size=7)
        label_y = y + tag_dim + 1.0  # 1mm gap below the tag
        pdf.set_xy(0, label_y)
        pdf.cell(PAGE_W_MM, 4, label, align="C")

        # Clean up temp file
        Path(tmp.name).unlink(missing_ok=True)

    output_path = Path(output_path)
    pdf.output(str(output_path))
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate AprilTag or ArUco marker images (PDF or PNG).",
    )
    parser.add_argument(
        "ids", nargs="?", default="0-3",
        help='Tag IDs — e.g. "0-3,7,10-12" (default: "0-3")',
    )
    parser.add_argument(
        "-A", "--aruco", action="store_true",
        help="Generate ArUco 4x4 markers instead of AprilTag 36h11",
    )
    parser.add_argument(
        "--png", action="store_true",
        help="Output individual PNG files instead of a single PDF",
    )
    parser.add_argument(
        "-o", "--out", default=None,
        help="Output path — PDF file or PNG directory (default: tags.pdf or tag-images/)",
    )
    parser.add_argument(
        "--size", type=int, default=800,
        help="Tag image size in pixels (default: 800)",
    )
    parser.add_argument(
        "--label", action="store_true",
        help="Add label below tag (PNG mode only)",
    )
    args = parser.parse_args(argv)

    family = "aruco4x4" if args.aruco else "36h11"
    tag_ids = parse_ids(args.ids)

    if not tag_ids:
        print("No valid IDs specified.")
        return 1

    if args.png:
        # --- PNG mode ---
        out_dir = Path(args.out) if args.out else Path("tag-images")
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = "aruco4x4" if args.aruco else "tag36h11"
        for tag_id in tag_ids:
            gray = render_tag(tag_id, family=family, size=args.size)
            img = cv.cvtColor(gray, cv.COLOR_GRAY2BGR)
            img = add_quiet_zone(img, 2.0 / 7.0)
            if args.label:
                img = add_label_below(
                    img, _family_label(family, tag_id),
                    margin=30, font_scale=1.2, thickness=2,
                )
            out_path = out_dir / f"{prefix}_{tag_id}.png"
            cv.imwrite(str(out_path), img)
        print(f"Wrote {len(tag_ids)} PNG(s) to {out_dir.resolve()}")
    else:
        # --- PDF mode (default) ---
        out_path = Path(args.out) if args.out else Path("tags.pdf")
        generate_pdf(tag_ids, family=family, size=args.size, output_path=out_path)
        print(f"Wrote {len(tag_ids)} tag(s) to {out_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
