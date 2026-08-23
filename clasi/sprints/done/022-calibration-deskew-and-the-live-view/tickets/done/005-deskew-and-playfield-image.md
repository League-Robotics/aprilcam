---
id: '005'
title: Deskew and playfield image
status: done
use-cases:
- SUC-005
depends-on:
- '003'
- '004'
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Deskew and playfield image

## Description

Implements SUC-005 (parent UC-06, UC-19): the `Deskewer` implementation
(fixed 800×800-bounding-box warp, aspect-preserved, derived
`px_per_cm`), a real `Daemon.get_frame(deskewed=True)` for calibrated
cameras (currently an unconditional `NotCalibrated` since sprint 020),
`aprilcam playfield image`, and `client/renderer.py` — the tag-drawing
half of the already-declared `Renderer` interface, needed so
`playfield image`'s output (and ticket 006's live view) can show tag
boundaries.

Builds:

- `calibration/deskew.py` (new) — `Deskewer.deskew(frame)`: warps the
  field into the fixed bounding box (`main-playfield`'s 134.3×89.3 cm
  → 800×532), constructed from a camera's stored `Calibration` (ticket
  003) plus its linked `PlayfieldDefinition`'s dimensions. Reports
  derived `px_per_cm`/`world_extent`, never a chosen constant.
  Preserves the anchor invariant: deskewed image `(0,0)` = field's
  north-west corner, world `(-width/2, +height/2)`.
- `daemon/service.py` — `get_frame(deskewed=True)` real implementation
  for a calibrated camera: warps the raw frame, embeds the latest
  ring-buffer `TagFrame` (unchanged mechanism from sprint 021's
  Decision 4 — still never triggers a fresh burst itself), populates
  the response's `px_per_cm`/`world_extent`. Uncalibrated-camera branch
  (`NotCalibrated`) is unchanged.
- `proto/aprilcam_v2.proto` — `GetFrameResponse` gains `px_per_cm`/
  `world_extent` fields, appended, never renumbering existing fields.
- `client/renderer.py` (new) — concrete `Renderer.render(frame,
  layers=None, extra=())`: draws tag boundary quads and front "hats"
  from an embedded `TagFrame`'s `world_corners`/`yaw_rad` (deskewed
  frames) using Pillow (no cv2, matching the client library's cv2-free
  discipline). The annotation-drawing path is present per the
  interface but naturally inert this sprint (`frame.annotations` is
  always `None` — nothing to draw, not a stub).
- `cli/playfield.py` extended — `aprilcam playfield image [-o file]`.

**Archive pointers**: mine `archive/src/aprilcam/calibration/
geometry.py`'s `metric_deskew_matrix` for the warp-matrix construction
approach — note it currently `raise RuntimeError`s without cv2; confirm
what the deskew math genuinely needs cv2 for vs. what is pure NumPy.
The daemon may use cv2 (daemon-only dependency, per project
constraints); `client/renderer.py` specifically must stay cv2-free —
that boundary is non-negotiable regardless of what `calibration/
deskew.py` (daemon-side) ends up using.

## Acceptance Criteria

- [x] `main-playfield`'s 134.3×89.3 cm field deskews to **exactly
      800×532** (aspect-preserved into the 800×800 box) (UC-06.S-1,
      UC-19.S-1).
- [x] **North is up, east is right on the deskewed image** — verified
      two ways: (1) a fixture tag surveyed north of another appears at
      a **smaller** pixel-y (image rows grow downward, exactly one
      y-flip inside the published mapping); (2) world (0,0) converted
      through the returned `px_per_cm`/`world_extent` metadata lands on
      AprilTag 1's known pixel position in the image, within tolerance.
- [x] `px_per_cm` reported in the response is **derived from the
      actual output dimensions** (`frame_width / field_width_cm`),
      never a hardcoded constant — assert it matches for both
      `main-playfield`'s dimensions and a second, differently-sized
      synthetic playfield fixture.
- [x] A deskewed request on an **uncalibrated** camera still fails
      explicitly with `NotCalibrated`; no file is written by the CLI
      and no raw frame is substituted (UC-06.S-3, UC-19.S-2) — this
      exact sprint-020 contract, regression-tested to confirm the new
      calibrated branch didn't loosen it.
- [x] `client/renderer.py`: rendering a fixture `ImageFrame` with a tag
      at a known `world_corners` position produces drawn pixels at the
      expected neighborhood (pixel-level check against the frame's own
      published metadata, no live window needed).
- [x] `client/renderer.py` on a frame with `annotations=None` draws
      only tags, raising nothing and producing no annotation output —
      confirms the inert path is genuinely a no-op, not an exception.
- [x] Every client module touched here still contains no `cv2` import
      — extend the existing no-cv2-import sweep test (or add one if it
      doesn't yet exist at this point in the tree) to cover
      `client/renderer.py`.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/daemon/test_service.py` (the `NotCalibrated`
  regression above), `uv run pytest tests/aprilcam/client/`.
- **New tests to write**: `tests/aprilcam/calibration/test_deskew.py`
  (output dimensions, aspect preservation, `px_per_cm` derivation,
  anchor invariant, north-up check — all against fixtures, no camera
  hardware); `tests/aprilcam/client/test_renderer.py` (fixture
  `ImageFrame` → pixel-neighborhood checks, inert-annotations no-op,
  no-cv2-import sweep); `tests/aprilcam/daemon/test_service.py`
  additions for the calibrated `get_frame(deskewed=True)` path.
- **Verification command**: `uv run pytest && uv run mypy` clean; zero
  `typing.Protocol` — `Deskewer`/`Renderer` derive from their declared
  base classes; baseline 388 passing, additive only.
