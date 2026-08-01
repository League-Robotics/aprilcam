---
id: '003'
title: AprilTag 36h11 + ArUco 4x4 detection engine
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
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

- [x] `vision/aruco_compat.py` exists, providing the OpenCV 4.6/4.7
      ArUco API compatibility shim (ported from
      `archive/src/aprilcam/vision/aruco_compat.py`).
- [x] `vision/detector.py` exists with a `TagDetector`/`DetectorConfig`
      pair (or equivalent) scoped to `"36h11"` + `"aruco_4x4"` only —
      no `family="all"` option this sprint. Detection consumes
      `vision/pipeline.py`'s stages (ticket 001) rather than an inline
      preprocessing method.
- [x] `detect()` returns raw hits carrying: family (`TagFamily`-typed,
      not a bare string, to align with the `TagId` the daemon layer
      will build downstream — see ticket 004), non-negative dictionary
      id, and 4 corners in **pixel space, in the detector's native
      order** (do not reorder to front-left/front-right/back-right/
      back-left here — see the next bullet).
- [x] The mapping from the detector's native corner order to the
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
- [x] **No `-(N+1)` id encoding anywhere in this module.** Raw ArUco
      hits carry their native non-negative dictionary index tagged
      with `family=TagFamily.ARUCO`; the encoding v1 used to avoid
      colliding AprilTag and ArUco ids in one shared int-keyed dict is
      unnecessary now that family is a typed part of the identity —
      see sprint.md's Architecture Decision 2 for the full reasoning.
      Do not port it "to be safe."
- [x] Corner subpixel positions are stable within ε across repeated
      detection runs on the same fixture image (no randomness in the
      pipeline or detector configuration).
- [x] `uv run mypy` stays clean; `uv run pytest` stays green.

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

## Implementation Notes

**Corner-order verification — hypothesis confirmed, with the convention
made explicit rather than assumed.** `aprilcam taggen` does not exist in
the v2 CLI yet (only `archive/src/aprilcam/tags/taggen.py` /
`archive/src/aprilcam/cli/taggen_cli.py`), so per the ticket's own
"equivalent local helper" fallback, `tests/aprilcam/vision/_tag_fixtures.py`
renders markers directly with `cv2.aruco.generateImageMarker` (the same
primitive `taggen.render_tag` uses), composites them onto a white canvas
with a quiet-zone border, and rotates by a known angle via
`cv2.getRotationMatrix2D`/`cv2.warpAffine`.

Method: detect a synthetic tag at its canonical (unrotated) print
orientation, then detect the same tag rotated by 90°, 180°, 270°, and a
non-axis-aligned 37°, and compare each rotated detection's corners
against the canonical detection's corners transformed by that same known
rotation matrix. Ran for both families (36h11 and ArUco 4x4 share the
same underlying `ArucoDetector` machinery in this OpenCV build, 4.11.0).

Finding: **OpenCV's corner order is fixed relative to the marker's own
printed pattern, not the image frame** — detected corner index 0 is
always the marker's canonical top-left corner (as drawn with zero
rotation), corners 1/2/3 are canonical top-right/bottom-right/bottom-left,
regardless of the physical rotation applied before capture. This is what
makes stable "front" tracking possible at all.

Neither family's bit pattern carries a marking a human would recognise as
"this is the front edge" at a glance — the pattern exists to be decoded,
not eyeballed. Absent a physical/human-obvious marking, "front" is
necessarily a mounting convention rather than an intrinsic property of
the pattern; the only convention available without additional hardware
information is the marker's own canonical print orientation. This ticket
adopts **front = the marker's canonical top edge** (i.e., the orientation
`generateImageMarker` draws with no rotation). Under that convention the
OpenCV-native corner order (canonical TL, TR, BR, BL) is *already*
identical, index for index, to design.md §6's boundary/hat order
(front-left, front-right, back-right, back-left) — verified directly in
`test_canonical_orientation_front_edge_is_corners_0_to_1` (top edge =
corners 0→1, bottom edge = corners 2→3, left-to-right within each) and
`test_corner_order_tracks_the_pattern_not_the_image_frame` (rotation
tracking, both families, 4 angles). **No reordering is required anywhere
in this pipeline** for the convention to hold — ticket 004 can carry
`Detection.corners` straight through as `pixel_corners`.

Caveat recorded honestly (also in the module docstring): this is a
software-verifiable convention, not a law of physics — it depends on
physical tags actually being mounted with their canonical print
orientation facing the robot's front, an assembly choice this module has
no way to see or enforce. Ticket 007's hardware pass against
`main-playfield`'s real, physically-mounted tags is the cross-check
against an assembly that might not honour it.

**Pipeline composition — left to the caller, no cross-import with
`vision/pipeline.py`.** `TagDetector.detect(gray, *, scale=1.0)` takes an
already-preprocessed grayscale image and a bare `float` scale factor
(the same value as `pipeline.Rescale.scale`, dividing directly into the
corner array per that class's own docstring) rather than importing
`Rescale` itself. `vision/detector.py` has zero dependency on
`vision/pipeline.py` as a result — the two modules stay independently
testable per sprint.md Step 2's "changes independently" boundary. Ticket
004's `daemon/detection.py` is where `run_pipeline(...)` and
`TagDetector.detect(...)` actually get composed per burst; construct one
`TagDetector` per camera and reuse it across bursts (building the
per-family `ArucoDetector` objects is not free — this mirrors v1's own
"avoid per-frame overhead" comment).

**`DetectorConfig` scope.** Kept: `quad_decimate`, `quad_sigma`,
`corner_refine`, and the earned `april_min_wb_diff`/
`april_min_cluster_pixels`/`april_max_line_fit_mse` thresholds plus the
widened `adaptiveThreshWinSize{Min,Max,Step}` sweep (all v1 defaults,
unchanged). Dropped per the ticket's explicit instruction: `family`
(fixed to 36h11 + ArUco 4x4, always both, no `"all"`),
`detect_aruco_4x4` (no longer optional — always on), `detect_inverted`
(collapsed into an unconditional `detectInvertedMarker = True`, an
earned constant rather than a toggle nothing ever varied), and every
preprocessing field (`use_highpass`, `highpass_ksize`, `use_clahe`,
`use_sharpen`, `proc_width` — these already live in
`vision.pipeline.PipelineConfig`, ticket 001).

**`Detection` dataclass.** `frozen=True, kw_only=True, eq=False` — `eq`
suppressed because `corners` is a numpy array and numpy's `==` returns an
elementwise array rather than a bool, which would make an
auto-generated `__eq__`/`__hash__` raise the moment two `Detection`s were
compared. Fields: `family: TagFamily`, `number: int` (native
non-negative dictionary index, never `-(N+1)`), `corners: np.ndarray`
(`(4, 2)` float32, detector-native order), `center: PixelPoint` (the
corners' centroid — a derived convenience, not a separate source of
truth).

**Verification run.** `uv run pytest`: 285 passed (was 253, net +32, no
regressions — 32 new tests in `tests/aprilcam/vision/test_detector.py`).
`uv run mypy`: clean (42 source files, `cv2.*` remains covered by the
existing `ignore_missing_imports` override; no new overrides needed).
