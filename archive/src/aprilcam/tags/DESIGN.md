# Tags

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** stable

---

## 1. Purpose

Offline generation of the physical fiducials the rest of the system detects:
AprilTag 36h11 and ArUco 4x4 marker images, as printable PDFs or PNGs. The
seam is *producing markers vs. perceiving them* — this package renders bits to
paper and never reads a camera. It is the one sanctioned OpenCV import outside
the daemon (needed only for the ArUco dictionary bit patterns and rasterizer).

## 2. Orientation

Two standalone modules:

- **`taggen.py`** — the real tool, wired to the CLI as `aprilcam taggen`.
  `parse_ids` turns a spec like `"0-3,7,10-12"` into a sorted unique id list
  (default `"0-3"`). `render_tag` rasterizes one grayscale marker for family
  `"36h11"` or `"aruco4x4"` (handles the `generateImageMarker` vs.
  `drawMarker` OpenCV API split). Quiet-zone helpers pad white borders;
  `add_label_below` captions PNGs. `generate_pdf` (via fpdf2, lazily
  imported) lays one tag per 59×102 mm page with a centered label — default
  output `tags.pdf`. PNG mode (`--png`) writes one file per id instead.
- **`arucogen.py`** — an older standalone generator: 16 labeled ArUco 4x4
  PNGs with a corner-position legend (`POSITIONS` map, IDs 0-3 =
  UL/UR/LL/LR). It has its own `main()` and is **not** wired into the CLI
  dispatcher; run it only as a module. `taggen -A` covers the same need.

## 3. Constraints and Invariants

- **This package keeps its cv2 dependency by explicit stakeholder decision**
  (spec §9.4) — it is an offline utility already behind the `[daemon]` extra.
  Do not spend effort making it Pillow-only unless the base-install
  requirement changes.
- **Never opens a camera, never imports daemon/client code** — adding either
  collapses the produce/perceive seam and drags OpenCV toward thin clients.
- **PDF page geometry is physical** — 59×102 mm pages with a ~0.04 quiet
  ratio were tuned so the printed inner tag fills the page while remaining
  detectable. Changing the quiet ratio or page size changes the physical tag
  size on every reprint; existing surveyed playfields assume the current
  print dimensions.
- **`ARUCO_POSITIONS` labels (IDs 0-3 → corners) are informational legacy** —
  current playfields use perimeter ArUco IDs 1-8 with positions surveyed in
  the playfield definition, not this table. The label is a printing aid only;
  never treat it as ground truth.

## 4. Design

Rendering is grayscale-first: `render_tag` returns a single-channel array,
PDF mode keeps it grayscale (smaller embeds), PNG mode converts to BGR only
when labels/quiet zones need it. The PDF path writes each marker to a temp
PNG because fpdf2 embeds images from files; temp files are unlinked per page.
fpdf2 is imported inside `generate_pdf` to keep CLI startup fast when only
parsing arguments or writing PNGs. `parse_ids` accepts ranges inclusive on
both ends and deduplicates, so overlapping specs are harmless.

`arucogen.py` predates `taggen.py`'s `-A` flag and survives as a convenience
script; the misspellings in its `POSITIONS` map ("Upper RIght") are cosmetic.

## 5. Interfaces

### Exposes

- `parse_ids(spec: str) -> list[int]` — `"0-3,7,10-12"` → sorted unique ints.
- `render_tag(tag_id, family="36h11", size=800) -> np.ndarray` — grayscale;
  `ValueError` on unknown family.
- `generate_pdf(tag_ids, family="36h11", size=800, output_path="tags.pdf") -> Path`
  — one tag per page, labeled.
- `add_quiet_zone_gray` / `add_quiet_zone` / `add_label_below` — composition
  helpers.
- `taggen.main(argv)` — the `aprilcam taggen` entry point: PDF by default,
  `--png` for per-id files, `-A/--aruco` for ArUco 4x4, `--size`, `--label`,
  `-o/--out`.
- `arucogen.main(argv)` — standalone; not in the CLI dispatcher.

### Consumes

- OpenCV (`cv2.aruco` dictionaries + rasterizer), NumPy, fpdf2 (PDF mode
  only, lazy).

## 6. Open Questions / Known Limitations

- **`arucogen.py` is redundant** with `taggen -A` and unwired from the CLI;
  fold-in or deletion is housekeeping the v2 spec leaves open (spec §4.10
  describes only `aprilcam taggen`).
- **cv2-free rendering** via pre-rendered dictionaries is noted as an option
  in spec §4.10 but resolved against for now by decision §9.4 (keep OpenCV).
- **PDF page size is hardcoded** (59×102 mm label stock); other stock sizes
  require code changes, not flags. Fine until someone prints on A4.
