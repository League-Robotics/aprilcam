---
id: '001'
title: 'Renderer overlay rework: red U, green hat, ID label, world crosshair'
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: playfield-view-should-draw-tag-ids-hats-and-a-tag-sidebar-like-v1.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Renderer overlay rework: red U, green hat, ID label, world crosshair

## Description

Replace `client/renderer.py`'s current tag overlay (`_draw_tag` at
`:154-177`, `_tag_points` at `:115-151`) with v1's visual scheme, drawn
in Pillow (`ImageDraw`) — no cv2, ever
(`tests/test_no_cv2_in_client_modules.py` still sweeps this file).
Geometry-wise this is drawing only: every position drawn already exists
on `TagRecord` (`types.py:74-126`) or the frame's own published
`px_per_cm`/`world_extent` metadata. No new correction math, no daemon
change, no wire change.

**Colors and shapes (mined from `aprilcamv1/ui/display.py:162-278`,
sprint.md's Architecture Step 5):**

- **Red `#FF0000` open 3-sided "U"**, 2px lines: `front_right→back_right`,
  `back_right→back_left`, `back_left→front_left`. The front edge
  (`front_left→front_right`, i.e. `points[0]→points[1]` per
  `TagRecord`'s own corner-order contract) is **never** drawn red — this
  is the one shape difference from today's full 4-sided `draw.polygon`
  call.
- **Green `#00FF00` peaked hat**, 2px lines, closing the open front
  edge: two lines from `front_left` and `front_right` to a shared apex
  at `mid(front_left, front_right) + top_dir * clamp(0.35 * |front_right
  − front_left|, 6.0, 80.0)`, where `top_dir = normalize(mid(front) −
  centroid)` and `centroid` is the mean of all four boundary points
  (exactly as today's `_draw_tag` already computes `center`). This
  replaces the current single-line hat (`_HAT_LENGTH_RATIO`-based) —
  remove `_HAT_LENGTH_RATIO`, it no longer applies.
- **Tag ID label**: `record.tag.number` as text, centered at `centroid`
  (or at the boundary quad's own center — same point the hat direction
  is computed from), red fill with a dark/black outline via Pillow's
  `ImageDraw.text(..., stroke_width=2, stroke_fill="black")` (Pillow
  >=8.0's stroke API; confirm the installed Pillow version supports it —
  `pyproject.toml`'s pinned floor is Pillow ≥10.0 per the project's
  tech stack, well past this). Use a font sized proportionally to the
  tag's own boundary (roughly matching v1's `FONT_HERSHEY_SIMPLEX` scale
  0.8 by eye is fine — this is a presentation constant, not geometry;
  document whatever constant you pick, the same way `_HAT_LENGTH_RATIO`
  documented its own choice).
- **Yellow `#FFFF00` crosshair**, 2px lines, at `record.world` converted
  via the already-imported `world_to_deskewed_pixel` (same formula
  `_tag_points` already uses for `world_corners` — do not write a second
  conversion). Arm length = `max(4.0, 0.5 * frame.px_per_cm)` pixels
  (the pixel length of 0.5 cm at that frame's scale, floored at 4px).
  Skip entirely when `record.world is None` (uncalibrated observation)
  — draw nothing, do not raise. The crosshair is deliberately **not**
  derived from `world_corners`' centroid: `world_corners` are
  intentionally *not* mount-corrected (`types.py`'s own `TagRecord`
  docstring), so the U/hat outline the tag as physically seen while the
  crosshair marks the corrected center — that separation is the entire
  point of drawing both. Only draw the crosshair on a **deskewed**
  frame; a raw frame has no world→pixel mapping to convert with (mirror
  `_frame_can_place_annotations`'s existing "skip, don't raise"
  discipline for exactly this reason).

**Raw frames** (`frame.deskewed is False`): keep drawing the U/hat/ID
from `pixel_corners` exactly as today's `_tag_points`' raw branch
already resolves them — same shapes/colors as the deskewed path, just
sourced from `pixel_corners` instead of `world_corners`. Never draw a
crosshair on a raw frame (no world mapping exists there at all).

**Suggested internal shape** (not binding — pick whatever keeps the
module readable): split today's single `_draw_tag(draw, points)` into
something like `_draw_boundary_and_hat(draw, points)` (the U + hat, from
points alone, as today) plus `_draw_tag_id(draw, points, number)` plus a
new `_crosshair_pixel(record, frame) -> Optional[_Point]` /
`_draw_crosshair(draw, pixel, arm_px)` pair, called separately in
`render()`'s per-record loop so the crosshair's "skip if no world
mapping" check is independent of the boundary's "skip if no corners"
check (a raw record with `pixel_corners` but no `world` should still get
its U/hat/ID, just no crosshair).

## Acceptance Criteria

- [x] The front edge (`front_left→front_right`) is never drawn red; the
      other three sides of the boundary quad are, in `#FF0000`, 2px.
- [x] The hat is two `#00FF00` 2px lines from `front_left`/`front_right`
      to the documented apex formula — verified at both the 6px floor
      and the 80px ceiling of the clamp (construct fixtures where
      `0.35 * |front edge|` falls below 6 and above 80).
- [x] A tag-ID label (`record.tag.number`) is drawn centered on the tag,
      red with a visible dark outline, on both deskewed and raw frames.
- [x] A yellow `#FFFF00` 2px crosshair is drawn at `record.world`
      (converted via `world_to_deskewed_pixel`, not at the raw corners'
      centroid) with arm length `max(4px, 0.5 * px_per_cm)` — verified
      with a `px_per_cm` low enough to hit the 4px floor and one high
      enough that the floor doesn't apply.
- [x] No crosshair is drawn when `record.world is None`, or on a raw
      (non-deskewed) frame — in both cases the U/hat/ID still draw from
      whichever corners are available.
- [x] A record with neither `world_corners` (deskewed) nor
      `pixel_corners` (raw) available is skipped entirely, matching
      today's existing behavior (`test_render_skips_a_record_with_no_
      world_corners_on_a_deskewed_frame` /
      `test_render_skips_a_record_with_no_pixel_corners_on_a_raw_frame`
      — these must keep passing unmodified).
- [x] The annotation half of `renderer.py` (`_draw_marker`/`_draw_path`/
      `_draw_polygon`/`_draw_circle`/`_draw_text` and their shared
      `_to_pixel`/`_color`/`_line_width` helpers) is untouched — this
      ticket only changes tag drawing.
- [x] `tests/test_no_cv2_in_client_modules.py` still passes (no `cv2`
      import introduced).
- [x] `uv run mypy` clean.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/client/test_renderer.py
  tests/test_no_cv2_in_client_modules.py -v`
- **Tests to update** (pinned geometry/color assertions tied to the old
  scheme — update in place, do not delete):
  - `test_render_draws_the_boundary_at_the_position_the_metadata_predicts`
    (`:121`) samples the front-edge midpoint expecting boundary paint —
    under the new scheme the front edge is never drawn, so move this
    sample to one of the three red sides (e.g. the midpoint of
    `back_right→back_left`) and rename/reword the docstring accordingly.
  - `test_render_draws_a_hat_beyond_the_front_edge_pointing_away_from_
    the_tag_centre` (`:134`) samples a point just north of the old
    single-line hat's direction — move the sample to a point along the
    new apex direction (`mid(front) → apex`), matching the new formula.
  - `test_render_leaves_the_rest_of_the_image_as_plain_background`
    (`:157`) — intent unchanged (a far corner is untouched); confirm the
    sampled corner still clears the new, potentially larger ID-label
    footprint (adjust the corner/tag fixture geometry apart if it now
    overlaps).
  - The annotation-half tests (`:523` and its neighbors — `Style`
    color/width honoring) are **not** expected to need changes; run them
    to confirm, but do not touch them speculatively.
- **New tests to write**: 3-sided U (assert the front-edge midpoint
  stays background-colored), hat apex at both clamp bounds, ID label
  presence (a patch near the tag centroid differs from background,
  distinguishable from the boundary/hat by color channel dominance —
  e.g. assert a red-dominant patch distinct from the green hat and the
  red boundary lines, or simply assert *some* drawing at the expected
  label bounding box), crosshair position/arm-length at both the floor
  and non-floor cases, crosshair skipped when `world is None`, crosshair
  skipped on a raw frame, raw-frame U/hat/ID still drawn from
  `pixel_corners`.
- **Verification command**: `uv run pytest
  tests/aprilcam/client/test_renderer.py
  tests/test_no_cv2_in_client_modules.py -v && uv run mypy`

**Documentation updates**: `client/renderer.py`'s own module docstring
(currently describes the lime-box/single-hat scheme in its opening
paragraphs) and `src/aprilcam/client/DESIGN.md` (seeded into this
sprint's `design/client-DESIGN.md` overlay) — update both to describe
the new red-U/green-hat/ID/crosshair scheme in place of the retired one.
