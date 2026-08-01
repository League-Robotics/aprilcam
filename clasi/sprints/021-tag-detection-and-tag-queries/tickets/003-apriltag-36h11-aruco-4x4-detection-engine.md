---
id: '003'
title: AprilTag 36h11 + ArUco 4x4 detection engine
status: open
use-cases: [SUC-001, SUC-002]
depends-on: ['001']
github-issue: ''
issue: tag-detection-and-tag-queries.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# AprilTag 36h11 + ArUco 4x4 detection engine

## Description

Builds the second half of `vision/`: the detector itself, over
ticket 001's preprocessing pipeline output. Returns **raw pixel-space
hits only** — no world conversion, no calibration awareness, no
scheduling, no camera access. This is "the last layer where pixels are
a legitimate output" (dev-plan Stage 5) — everything above this module
(daemon integration, ticket 004) is responsible for deciding what the
pixels mean.

Scoped narrower than v1: the issue calls for exactly AprilTag 36h11
and ArUco 4x4, not v1's full four-AprilTag-family option.

## Acceptance Criteria

- [ ] `vision/aruco_compat.py` exists, providing the OpenCV 4.6/4.7
      ArUco API compatibility shim (ported from
      `archive/src/aprilcam/vision/aruco_compat.py`).
- [ ] `vision/detector.py` exists with a `TagDetector`/`DetectorConfig`
      pair (or equivalent) scoped to `"36h11"` + `"aruco_4x4"` only —
      no `family="all"` option this sprint. Detection consumes
      `vision/pipeline.py`'s stages (ticket 001) rather than an inline
      preprocessing method.
- [ ] `detect()` returns raw hits carrying: family (`TagFamily`-typed,
      not a bare string, to align with the `TagId` the daemon layer
      will build downstream — see ticket 004), non-negative dictionary
      id, and 4 corners in **pixel space, in the detector's native
      order** (do not reorder to front-left/front-right/back-right/
      back-left here — see the next bullet).
- [ ] The mapping from the detector's native corner order to the
      design.md §6 boundary/hat contract
      (front-left, front-right, back-right, back-left, with the 0→1
      edge as the front edge) is stated explicitly as a documented,
      **verified** convention in this module's docstring — not
      assumed. Verify it: generate a tag image at a known rotation via
      `aprilcam taggen` (or an existing tag PNG under a known applied
      rotation), detect it, and confirm which returned corner index
      lands where a human reading the tag would call "front-left" for
      that family's marking convention. If this ticket's owner cannot
      establish a confident convention from the family's own marker
      pattern, flag it explicitly in the ticket's completion notes
      rather than guess — ticket 007's hardware pass is the final
      cross-check, not the first one.
- [ ] **No `-(N+1)` id encoding anywhere in this module.** Raw ArUco
      hits carry their native non-negative dictionary index tagged
      with `family=TagFamily.ARUCO`; the encoding v1 used to avoid
      colliding AprilTag and ArUco ids in one shared int-keyed dict is
      unnecessary now that family is a typed part of the identity —
      see sprint.md's Architecture Decision 2 for the full reasoning.
      Do not port it "to be safe."
- [ ] Corner subpixel positions are stable within ε across repeated
      detection runs on the same fixture image (no randomness in the
      pipeline or detector configuration).
- [ ] `uv run mypy` stays clean; `uv run pytest` stays green.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/vision/ -v`
  (ticket 001's pipeline tests must still pass), full suite,
  `uv run mypy`.
- **New tests to write**: `tests/aprilcam/vision/test_detector.py` —
  NO cameras.
  - Smoke tests against the restored `tests/images/` fixtures:
    detection runs without error and returns plausible results
    (correct family separation — an ArUco-only image reports no
    AprilTag hits and vice versa). Do **not** hard-code a specific
    confirmed tag-id set from these fixtures — `field-state.json` is
    stakeholder-unconfirmed (sprint.md Architecture Open Question 1).
  - Ground-truth recall/corner-order tests against **synthetic**
    fixtures: render one or more tag images via `aprilcam taggen` (or
    an equivalent local helper) at known ids and known rotations,
    composite onto a plain background, and assert (a) the correct
    id+family is recovered, (b) corner order matches the documented
    boundary/hat convention for a tag placed at a known rotation, (c)
    repeated detection on the identical image yields identical corner
    positions.
  - Corner subpixel stability test (ε-bounded across N repeated
    detection calls on one fixture).
- **Verification command**:
  `uv run pytest tests/aprilcam/vision/ -v`

## Implementation Plan

### Approach

1. Port `archive/src/aprilcam/vision/aruco_compat.py` into
   `vision/aruco_compat.py` largely as-is — it is a narrow OpenCV
   version-compatibility shim, not detection logic; no meaningful
   redesign needed.
2. Convert `archive/src/aprilcam/core/detector.py`'s `TagDetector`/
   `DetectorConfig`/`_build_detectors` into `vision/detector.py`,
   dropping everything not in scope: the `"all"` family option, the
   `detect_inverted`/`detect_aruco_4x4` toggles collapse into a fixed
   36h11+ArUco-4x4 configuration (still expose tuning knobs —
   `quad_decimate`, `quad_sigma`, `corner_refine`, the
   `april_min_wb_diff`/`april_min_cluster_pixels`/
   `april_max_line_fit_mse` earned constants and the widened
   `adaptiveThreshWinSize*` range — these are the "earned constants"
   the issue asks to mine, keep their defaults).
3. Route preprocessing through `vision/pipeline.py` (ticket 001)
   rather than duplicating `_preprocess` — construct the detector to
   accept an already-preprocessed grayscale image, with the pipeline
   composition happening one layer up (in this module's own
   `detect()` wrapper, or left to the caller — decide based on
   whichever keeps `vision/detector.py` genuinely stateless and
   free of pipeline-config duplication; document the choice).
4. Return a `Detection` dataclass: `family: TagFamily`, `number: int`
   (non-negative, native dictionary index), `corners: <4x2 array,
   detector-native order>`, `center` (optional convenience). Do **not**
   build `TagId` here — that composition (`TagId(family, number)`) and
   any corner reordering to the boundary/hat contract happens in
   ticket 004's daemon-integration layer, keeping this module a pure
   "pixels in, raw hits out" boundary.
5. Resolve the corner-order verification (acceptance criterion above)
   before ticket 004 depends on it — this is the one genuinely
   open empirical question in this ticket; budget real time for it.

### Files to create/modify

- `src/aprilcam/vision/aruco_compat.py` (new)
- `src/aprilcam/vision/detector.py` (new)
- `tests/aprilcam/vision/test_detector.py` (new)
- Possibly a small `testdev/` or `tests/` helper for generating a
  synthetic known-rotation tag fixture, if `aprilcam taggen` cannot be
  invoked directly from a test — keep it minimal.

### Documentation updates

Module docstring in `vision/detector.py` states the verified
corner-order convention explicitly (this **is** the documentation
deliverable for the open question — do not leave it only in test
comments).
