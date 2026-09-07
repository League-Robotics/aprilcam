---
id: '001'
title: TagRecord.image_yaw_deg and daemon-side image-space yaw computation
status: done
use-cases: []
depends-on: []
github-issue: ''
issue: uncalibrated-tag-records-should-report-image-space-yaw-in-degrees.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# TagRecord.image_yaw_deg and daemon-side image-space yaw computation

## Description

Add `TagRecord.image_yaw_deg` — a new, distinct field carrying the
tag's orientation in raw pixel space, derived from `pixel_corners`'
front edge (`corners[0]->corners[1]`, the same edge
`daemon/detection.py`'s `_front_edge_yaw` already uses for the
world-frame `yaw_rad`) — and populate it in `_build_tag_record` for
**every** record that has `pixel_corners`, calibrated or not.

This is the foundation ticket: SUC-002's contract and tickets 002/005
both depend on this field existing and being correctly signed before
they can display or stream it.

**Sign convention (stakeholder-fixed, not open for revision):** 0° =
the tag's front edge toward the **top of the image**,
clockwise-positive as seen on screen (image y grows downward, so this
is the opposite rotational sense from every world-frame angle in this
codebase, which is CCW-positive). Document this explicitly in
`TagRecord`'s own docstring, next to the existing `yaw_rad` paragraph,
making clear the two fields are unrelated frames/conventions and
neither substitutes for the other.

**Suggested formula** (derive independently against the sign-convention
description above, do not just copy this without checking it against a
cardinal-facing test): with `dx = corners[1].x - corners[0].x`,
`dy = corners[1].y - corners[0].y` (raw pixel space), `image_yaw_deg =
degrees(atan2(dx, -dy))`. Verify: an edge pointing straight up
(`dx=0, dy=-1`) gives 0°; rotating clockwise on screen to point right
(`dx=1, dy=0`) gives 90°; pointing down (`dx=0, dy=1`) gives 180°;
pointing left (`dx=-1, dy=0`) gives -90° (equivalently 270°) — confirm
this matches the stakeholder's convention before committing to it, and
adjust if your own derivation disagrees.

Per `docs/design/design.md` §6's new "Image-space yaw" bullet (this
sprint's `design/design.md` overlay) and `camera-DESIGN.md`/
`daemon-DESIGN.md`'s cross-references — read the sprint's `design/`
overlay directory for the full rationale before implementing.

## Acceptance Criteria

- [x] `types.TagRecord` gains `image_yaw_deg: Optional[float] = None`,
      documented with the sign convention (0 = top of image,
      clockwise-positive as seen on screen) directly in the class
      docstring, clearly distinguished from `yaw_rad`.
- [x] `daemon/detection.py`'s `_build_tag_record` computes
      `image_yaw_deg` from `pixel_corners` whenever `pixel_corners` is
      not `None` — in **both** the calibrated and uncalibrated
      branches (a calibrated record also gets it; it never conflicts
      with or overrides `yaw_rad`).
- [x] `yaw_rad`'s existing behavior is completely unchanged — no
      change to `_front_edge_yaw`, no change to what world-calibrated
      records report.
- [x] Unit tests on recorded/synthetic `pixel_corners` sets cover the
      four cardinal facings (front edge pointing up/right/down/left in
      image space) plus one oblique angle, asserting the exact
      `image_yaw_deg` value each produces.
- [x] A record with `pixel_corners is None` (should not occur in
      practice, but the field must degrade safely) leaves
      `image_yaw_deg` as `None`, never a fabricated value.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/test_detection.py tests/aprilcam/test_types.py -m "not camera and not integration"` (adjust paths to this repo's actual test layout for `detection.py`/`types.py`).
- **New tests to write**: a parametrized test over four cardinal
  `pixel_corners` sets plus one oblique set, asserting
  `_build_tag_record(...).image_yaw_deg` against hand-computed expected
  degrees for each; one test confirming a calibrated record's
  `yaw_rad` is unaffected by this change.
- **Verification command**: `uv run pytest tests/aprilcam/daemon -m "not camera and not integration"`
