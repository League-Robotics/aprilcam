---
status: pending
---

# Colored-object detection does not exist — needs vision-subsystem design before it can be tested

## Description

Sprint 035 ticket 006 originally specified `test_color_objects.py`: an
L0 assertion family that would vision-confirm the 6 surveyed rectangles
and 8 dots on `main-playfield` across the white-balance variation axis
(`docs/test-plans/camera-image-matrix.md` §6, item 5).

That assertion cannot be written today because no live color/object
classifier exists anywhere in the codebase, and the current architecture
explicitly rules one out for this feature class:

- `src/aprilcam/vision/DESIGN.md` §6 ("Open Questions / Known
  Limitations"): "No object/color/contour detection lives in this
  subsystem... Whatever backs the MCP `get_objects` tool is not
  implemented anywhere in `src/aprilcam/vision/` as of this review."
- `docs/design/design.md` §6 ("Global conventions"), the "Feature
  roles" rule: "ArUco + AprilTags are calibration ground truth; colored
  rectangles/dots are queryable surveyed positions only, never
  vision-confirmed."

The stakeholder chose to drop the `test_color_objects.py` acceptance
criterion from ticket 006 (2026-08-24) rather than either:

1. **Build a minimal color classifier** so the test would have something
   real to call — new production vision code, out of scope for a
   test-writing ticket, and a reversal of the "never vision-confirmed"
   rule above.
2. **Assert the static `playfield.query.FeatureQuery` survey lookup**
   instead (the "where is the blue dot" answer) — rejected because that
   lookup never touches pixel data, so it would return the identical
   answer for every white-balance-varied fixture: a vacuous pass across
   the exact stress axis the family was meant to exercise.

## What this issue tracks

If colored-object detection is ever wanted as a real, vision-confirmed
feature, it needs vision-subsystem design work first — deciding whether
and how it fits the `src/aprilcam/vision/` architecture, what algorithm
and accuracy target it would use, and how (or whether) it changes the
"never vision-confirmed" rule above. It should not be approached as a
test bolted onto an assumption that a classifier already exists.

## Related

- `clasi/sprints/035-apriltag-reliability-investigation-and-layered-test-system/tickets/006-l0-image-matrix-package-geometry-calibration-robustness-color-objects-preprocessing-tests.md`
  — the dropped-criterion note with full rationale.
- `docs/test-plans/camera-image-matrix.md` §6 — assertion-family list,
  corrected to no longer promise this family.
- `clasi/sprints/035-apriltag-reliability-investigation-and-layered-test-system/sprint.md`
  — SUC-001, corrected the same way.
