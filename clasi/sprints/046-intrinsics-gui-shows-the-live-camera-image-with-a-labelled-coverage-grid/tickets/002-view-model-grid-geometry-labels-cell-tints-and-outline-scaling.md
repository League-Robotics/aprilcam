---
id: '002'
title: 'View model: grid geometry, labels, cell tints and outline scaling'
status: open
use-cases: [SUC-002]
depends-on: ['001']
github-issue: ''
issue: intrinsics-gui-shows-the-live-camera-image-with-a-labelled-coverage-grid.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# View model: grid geometry, labels, cell tints and outline scaling

## Description

Put every display decision for the image overlay into pure functions in
`view/intrinsics_model.py`, so they are tested without a display or Pillow
(sprint.md Architecture items 4-6).

## Implementation Plan

Approach (all pure, no Tk, no cv2, no Pillow):
- `GRID_EDGE_FRACTION = 0.4` display copy of the collector's outer band
  (centre band is the remaining 20 %), plus `grid_rects(width, height)` ->
  nine `(x0, y0, x1, y1)` rectangles in `CELL_ORDER` (NW..SE), matching
  `collector._region_index` boundaries.
- `cell_overlays(cells: tuple[TileState, ...])` -> per-cell label and tint
  decision derived from the same `TileState.level` the tiles use; new
  `tint_colour(level)` table (`none` = no tint, `some` = amber, `good` = green)
  next to `tile_colour`. A dimmed variant helper for the failure state
  (`dim_colour` or a blend factor constant).
- `scale_polygons(polygons, image_size, dest_rect)` maps native-pixel outline
  points into the drawn rectangle (handles `None`/empty and a degenerate
  `image_size`); `fit_rect(image_size, column_rect)` returns the aspect-preserved,
  centred destination rectangle (same rule as `viewer._fit_size`; do not import
  `viewer` into the model, duplicate the three-line rule or accept sizes).
- Add the new names to `__all__`.

Files: `src/aprilcam/view/intrinsics_model.py`, `tests/aprilcam/view/test_intrinsics_model.py`.

## Acceptance Criteria

- [ ] `grid_rects` for several sizes (including odd and non-16:9) tiles the
      image exactly, with 40/20/40 splits and NW..SE order.
- [ ] A parity test (test code may import the cv2-bearing collector) asserts
      the display fraction equals `collector.EDGE_FRACTION` and that a point in
      each rect maps to the same index via `collector._region_index`.
- [ ] Cell tint is green exactly when the tile level is `good` (count >= 2),
      amber for `some`, none for `none`; tested against `build_camera_model`.
- [ ] `scale_polygons` and `fit_rect` tested for letterbox, scale up/down,
      `None`, empty and degenerate inputs.
- [ ] `tests/test_no_cv2_in_client_modules.py` passes.

## Testing

- **Existing tests to run**: `tests/aprilcam/view/`, `tests/test_no_cv2_in_client_modules.py`
- **New tests to write**: as listed in acceptance criteria
- **Verification command**: `uv run pytest tests/aprilcam/view tests/test_no_cv2_in_client_modules.py`
