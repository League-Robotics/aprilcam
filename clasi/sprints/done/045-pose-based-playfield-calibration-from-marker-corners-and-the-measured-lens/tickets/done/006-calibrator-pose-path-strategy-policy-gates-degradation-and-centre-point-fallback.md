---
id: '006'
title: 'Calibrator pose path: strategy, policy, gates, degradation and centre-point
  fallback'
status: done
use-cases:
- SUC-001
- SUC-003
- SUC-004
depends-on:
- '003'
- '004'
- '005'
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Calibrator pose path: strategy, policy, gates, degradation and centre-point fallback

## Description

Wire the pose solve into `Calibrator.calibrate` as a second strategy next to
the existing centre-point one, with the policy, gates and degradation ladder.
`Calibrator` stays an orchestrator: all arithmetic is in `pose.py`, all
decisions in `gates.py`.

Flow after capture and `_detect_markers`:
1. **Strategy choice.** Pose path iff (a) the stored lens model at this frame
   size has source `measured`, and (b) at least one matched, on-plane
   (`height_cm == 0`) marker has an effective `size_mm`. Otherwise the
   unchanged centre-point path runs and the result says `method:
   centre-point` plus a one-line reason (`no measured lens model`, `lens
   measured at a different resolution`, `lens source is estimated/legacy`,
   `definition has no marker size`). The existing resolution-mismatch
   `lens_warning` is preserved.
2. **Policy.** `required_marker_ids()` missing -> failure with `missing_ids`
   (exactly today's shape and wording). `helpful` missing -> `notes` only.
   No policy anywhere = all required = today.
3. **Solve and gates** (pose path): matched on-plane markers' corners ->
   `solve_planar_pose`; evaluate reprojection (px and cm), scale agreement,
   angular spread, ambiguity, and the per-marker leave-one-out residual via
   `gates.py`. Marker orientation is detected per marker inside the solve
   (ticket 004); no rotation comes from the definition. A leave-one-out
   failure's `reason` names the worst marker id and its cm residual. A gate failure returns
   `succeeded=False` with the measured numbers and a `reason` naming the gate
   and threshold; nothing is stored and the previous calibration survives.
4. **Degradation.** `full` / `reduced` (>= 2 matched, note) / `low` (exactly 1
   matched; "pose-only, low confidence"; also forced by an ambiguous IPPE
   result) / fail. Elevated markers are ignored by the pose solve (logged).
5. **Build** `Calibration` with homography from the pose, the lens used, and
   the ticket-005 provenance fields; `CalibrationResult` fills every metric.

The centre-point path keeps `_degeneracy_reason`, `_solve_homography`,
`_reprojection_error_cm` and the 1.0 cm threshold untouched. While the
estimator still exists (removed in ticket 008), `estimate_intrinsics=True`
continues to take the old branch; the pose path is not entered in that case.

## Acceptance Criteria

- [x] Four markers, measured lens, sized definition: `method: pose`,
      `corner_count == 16`, `confidence: full`, camera height/nadir within
      synthetic tolerance, all metrics populated.
- [x] Two-marker diagonal definition (or two of four visible with the others
      `helpful`): succeeds, `confidence: reduced`, warning note.
- [x] One marker visible (others `helpful`): succeeds, `confidence: low`.
- [x] Any `required` marker covered: fails with `missing_ids`, identical
      wording/shape to today; prior calibration untouched.
- [x] Definition without `size_mm`, or no/mismatched/non-measured lens:
      centre-point path, `method_reason` set, results numerically identical
      to the pre-sprint implementation (existing tests unchanged and green).
- [x] Each gate failing in isolation (inflated corner noise, wrong
      `size_mm`, markers clustered in one image corner, one marker's
      surveyed position displaced / two markers swapped) is rejected with the right reason and the numbers
      reported.
- [x] Mixed-orientation markers (all four rotations incl. upside down) in
      the definition-driven synthetic scene calibrate with `method: pose`
      and the same result as upright.
- [x] A swapped/misplaced marker (e.g. 9 and 10 exchanged) fails the
      leave-one-out gate naming the marker, not just an aggregate rms.
- [x] Derived homography agrees with the centre-point homography on the same
      synthetic data to < 0.2 cm over the field (no-distortion case).

## Implementation Plan

**Files to modify:** `src/aprilcam/calibration/calibration.py` (new private
methods `_calibrate_by_pose`, `_calibrate_by_centres`; `calibrate` dispatches),
`src/aprilcam/daemon/service.py` only if the Calibrator constructor wiring
needs the new optional arguments (none expected).

**Files to create/extend:** `tests/aprilcam/calibration/test_calibration_pose.py`
using the fake-frame-source fixtures from `test_calibration.py` and an
injected lens lookup.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/calibration tests/aprilcam/daemon/test_service.py -x`
- **New tests to write**: one per acceptance bullet, synthetic frames only.
- **Verification command**: `uv run pytest tests/aprilcam/calibration tests/aprilcam/daemon`

## Documentation Updates

`Calibrator.calibrate` docstring; DESIGN.md in ticket 012.
