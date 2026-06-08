---
id: '005'
title: Homography-derived deskew transform and Playfield polygon seeding
status: open
use-cases: [SUC-001]
depends-on: ['004']
github-issue: ''
issue: static-camera-deskew-from-homography.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Homography-derived deskew transform and Playfield polygon seeding

## Description

The primary deskew fix: derive the deskew source polygon from the saved
homography so no live ArUco corner detection is required, and seed the
`Playfield` polygon at load.

Today deskew is gated on a live polygon: `display._update_deskew`
(`src/aprilcam/ui/display.py` ~L60) returns early when
`playfield.get_polygon()` is `None`, and the polygon is built only from live
ArUco corner detection (`PlayfieldBoundary._order_poly` requiring all of
`{0,1,2,3}` or `{1,3,5,7}`). The warp math is duplicated across
`PlayfieldBoundary.deskew`, `get_deskew_matrix`, and `_update_deskew`.

This ticket:

1. **`calibration/geometry.py`** (new, pure NumPy/OpenCV-math leaf, no I/O,
   no detection):
   - `corner_pixels_from_homography(H, width, height) -> (4,2)` mapping world
     corners `(0,0),(W,0),(W,H),(0,H)` through `H⁻¹` (UL, UR, LR, LL order to
     match the existing polygon convention).
   - `metric_deskew_matrix(poly, width_cm, height_cm, px_per_cm) -> (3x3,
     (out_w, out_h))` building the warp that maps the source polygon to a
     **metric top-down rectangle** of size `W×H` cm scaled by `px_per_cm`. The
     destination corners are `(0,0),(W·px_per_cm,0),(W·px_per_cm,H·px_per_cm),
     (0,H·px_per_cm)` and the returned output size is
     `(round(W·px_per_cm), round(H·px_per_cm))`. `px_per_cm` is a parameter with
     a sensible default chosen so the deskewed output is roughly the source
     resolution. This replaces (does not defer) the prior pixel-space warp that
     sized the rectangle from polygon edge lengths.
2. **`core/playfield.py`**: `PlayfieldBoundary` seeds its polygon up front when
   constructed with a saved homography + dimensions (deriving corners via
   `geometry.py`), so `get_polygon()` is non-`None` before any live detection.
   The `polygon=` arg is already accepted; this wires the homography path into
   it. `Playfield` passes the saved homography + dimensions (from the
   ticket-004 calibration record) into `PlayfieldBoundary` in the existing
   `_auto_discover_*` load path. The live-corner path is preserved as a
   fallback when no saved homography exists.
3. **`ui/display.py`**: `_update_deskew` and `deskew`/`get_deskew_matrix` call
   `metric_deskew_matrix` instead of duplicating the math, producing a metric
   `W×H` top-down view at the configured `px_per_cm` (replacing the old
   polygon-edge-length pixel rectangle). The dimensions `W×H` come from the
   ticket-004 calibration record and `px_per_cm` from config (sensible default).
   Because `get_polygon()` is now seeded, deskew engages without live corners.

Fill-in/invalidation (ticket 006) and undistortion/mode wiring (ticket 007)
build on this.

## Acceptance Criteria

- [ ] `corner_pixels_from_homography` returns the four playfield-corner pixels
  for a known homography; verified against a synthetic `H` round-trip.
- [ ] `metric_deskew_matrix` maps the source polygon to a metric top-down
  rectangle: a polygon whose source maps to a known `W×H` warps so that world
  corners land at `(0,0),(W·px_per_cm,0),(W·px_per_cm,H·px_per_cm),
  (0,H·px_per_cm)` within tolerance.
- [ ] The deskewed output dimensions are a deterministic function of `W×H` and
  `px_per_cm`, i.e. `(round(W·px_per_cm), round(H·px_per_cm))`, independent of
  the source polygon edge lengths.
- [ ] `px_per_cm` has a sensible default (chosen so the deskewed output is
  roughly the source resolution) and is overridable.
- [ ] With a saved calibration (homography + dimensions), `PlayfieldBoundary`
  reports a non-`None` polygon before any live frame is processed.
- [ ] `_update_deskew`/`deskew`/`get_deskew_matrix` use the shared metric-warp
  helper; no duplicated warp math remains.
- [ ] A fixed camera with a saved calibration deskews with **no** ArUco corner
  in the live stream (simulated: seeded polygon, no live corners fed).
- [ ] Deskew falls back to the live-corner polygon path when no saved
  homography exists.
- [ ] `uv run pytest` passes.

## Implementation Plan

- **Approach**: Land the pure geometry leaf first, refactor the three warp
  sites onto it, then wire homography seeding into `PlayfieldBoundary` and
  `Playfield`.
- **Files to create**: `src/aprilcam/calibration/geometry.py`.
- **Files to modify**: `src/aprilcam/core/playfield.py`,
  `src/aprilcam/ui/display.py`.
- **Testing plan**: see Testing; use the committed global-shutter homography as
  a regression fixture.
- **Docs**: docstrings on `geometry.py`; note the seeded-polygon behavior in
  `PlayfieldBoundary`.

## Testing

- **Existing tests to run**: `uv run pytest tests -k "playfield or deskew or
  display"`, then full `uv run pytest`.
- **New tests to write**:
  - `corner_pixels_from_homography` round-trip: build `H` from known corners,
    recover the corners within tolerance.
  - `metric_deskew_matrix`: for a known source polygon and `W×H`, assert the
    warped world corners land at the metric destination corners and the returned
    output size equals `(round(W·px_per_cm), round(H·px_per_cm))`.
  - `px_per_cm` scaling: doubling `px_per_cm` doubles the output dimensions.
  - seeded polygon: construct `PlayfieldBoundary` with a homography + W×H,
    assert `get_polygon()` is non-`None` with no `update()` call.
  - regression: seed from `global-shutter-camera/calibration.json` and warp a
    synthetic frame; assert output size equals the `W×H`-and-`px_per_cm`-derived
    metric size.
  - fallback: no homography → behavior matches the current live-corner path.
  - Mark OpenCV-dependent tests `needs_cv2`.
- **Verification command**: `uv run pytest`
