---
id: '004'
title: 'Planar pose solve and gates: calibration/pose.py and gates.py'
status: done
use-cases:
- SUC-001
- SUC-003
- SUC-005
depends-on:
- '001'
- '003'
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Planar pose solve and gates: calibration/pose.py and gates.py

## Description

The numerical heart of the sprint: two new modules, no orchestration.

**`calibration/pose.py` (cv2):**
- `marker_object_points(centre_cm, size_mm, quarter_turns=0) -> (4,3)` -- the
  four corner positions in the field frame (cm, z = 0), in the same TL, TR, BR,
  BL order as `MarkerObservation`. Convention (guilty until proven innocent):
  field x east, y north; an upright marker (`quarter_turns = 0`) has its top
  edge toward north, so TL = centre + (-s/2, +s/2); each quarter turn rotates
  the marker counter-clockwise seen from above. Pin each statement with a test.
- **Orientation is detected, never declared.** `detect_marker_rotation(...)`
  fits the 4 detected corners (in the marker's own order) against each of the
  4 possible 90-degree rotations of the marker's object points and returns the
  rotation with the smallest residual (equivalently: the marker's in-plane
  rotation), plus the winner/runner-up residual ratio. Method: seed from a
  homography on the surveyed centres of all matched markers (or the other
  markers' pose when refining), score each candidate by corner residual,
  iterate once after the pose refit. Real example (main field): 1, 2, 3
  upright; 4 about +90; 8 about -90; 5, 6, 7, 9, 10 upside down. A marker
  whose ratio is poor (ambiguous, e.g. a single marker seen near-edge-on) is
  flagged and caps confidence at `low` in ticket 006.
- `solve_planar_pose(object_pts, image_pts_raw, camera_matrix, dist_coeffs) -> PlanarPose`:
  undistort to `P = K`, IPPE via `solvePnPGeneric` (keep both solutions and
  the error ratio, as `collector._pose` does), pick the best, refine with
  `solvePnPRefineLM` against the raw points through the distortion model.
  `PlanarPose` carries rvec/tvec, per-corner residuals, RMS px, and the
  ambiguity ratio.
- Derivations: `homography_from_pose(pose, K)` -- undistorted pixel -> field
  cm (inverse of `K [r1 r2 t]`), exactly the convention
  `Calibration.to_world` consumes; `camera_position_from_pose(pose)` ->
  `(height_cm, nadir WorldPoint, tilt_deg)` from `C = -R^T t`.
- `reprojection_error_cm(...)`: corner residuals mapped through the derived
  homography into field cm.
- Defines the `TargetObserver` callable type used later by ticket 011.

**`calibration/gates.py` (numpy only, cv2-free):** pure decisions, thresholds
as named constants (Open Question 3 defaults): `angular_spread_deg(rays)`,
`scale_agreement(local_distances, global_distances)` (median ratio; `None` for
fewer than 2 markers), `ambiguity_ok`, and `per_marker_leave_one_out(...)` (worst marker id and
cm residual; `None` below 3 markers) and `classify(matched, required,
helpful, metrics) -> (label, notes, failure_reason)` implementing the
degradation ladder (full / reduced / low / fail). The marker-local distance
used by the scale check is computed in `pose.py` (per-marker IPPE with its own
print size) and passed in as numbers.

Rationale for putting this in `calibration/` and not sharing code with
`intrinsics/collector.py`: see sprint.md Decision 1. Add a short comment in
both places pointing at each other.

## Acceptance Criteria

- [x] Synthetic ground truth: a camera at 144 cm over nadir (3.47, -2.85)
      (the camera-2 numbers), tilt a few degrees, looking at four corner
      markers of the secondary field, with a realistic distortion; the solve
      recovers height within 1 cm and nadir within 1 cm without noise, and
      within 2 cm with 0.3 px corner noise.
- [x] Derived homography reproduces the true pixel -> world mapping for
      arbitrary points (north-up, no mirror; east = +x), checked at points
      off the markers.
- [x] 16 corners from four markers; two-marker diagonal (8) and one-marker
      (4) solves still return a pose (flagged by ambiguity ratio where
      applicable).
- [x] Orientation detection: a synthetic scene with markers at all four
      rotations (0, +90, 180 i.e. upside down, -90) solves to the same pose
      (height, nadir, homography) as the same scene with every marker
      upright, within noise tolerance; the detected quarter-turn of each
      marker equals the one used to render it. A mixed layout matching the
      main field (1, 2, 3 upright, 4 +90, 8 -90, 5, 6, 7, 9, 10 upside down)
      is one of the cases.
- [x] Quarter-turn convention pinned: with the wrong rotation forced, the
      residual is large (so the detector's choice is meaningful), and the
      winner/runner-up ratio is reported.
- [x] Leave-one-out residual: for each marker, fit the pose to the others and
      report that marker's centre error in cm; synthetic test displaces one
      marker's surveyed position (the swapped-9/10 case: two markers
      exchanged) and the worst id is named correctly; clean data stays below
      the threshold. Worked reference: 0.75 cm worst on the corrected main
      field, 30 cm rms before the 9/10 correction.
- [x] Gate functions unit-tested for every branch of the degradation ladder
      and for threshold edges; threshold defaults justified by a noise sweep
      recorded in the ticket's implementation notes.
- [x] `gates.py` imports no cv2 (`tests/test_no_cv2_in_client_modules.py`
      extended or a dedicated import test if that guards only client
      packages).

## Implementation Plan

**Approach.** TDD against synthetic projection: build K, dist, pose; project
corners with `cv2.projectPoints`; solve; compare. Keep functions pure with
explicit arrays in/out; no logging of secrets, no state.

**Files to create:** `src/aprilcam/calibration/pose.py`,
`src/aprilcam/calibration/gates.py`, `tests/aprilcam/calibration/test_pose.py`,
`tests/aprilcam/calibration/test_gates.py`.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/calibration/test_geometry.py tests/aprilcam/calibration/test_distortion.py`
- **New tests to write**: as above, including a seeded noise sweep, the
  four-rotation and upside-down scenes, and the leave-one-out displacement test.
- **Verification command**: `uv run pytest tests/aprilcam/calibration/test_pose.py tests/aprilcam/calibration/test_gates.py`

## Documentation Updates

Module docstrings carry the conventions; DESIGN.md in ticket 012.

## Implementation Notes

**Conventions pinned (tests in `tests/aprilcam/calibration/test_pose.py`).**
Field frame x east, y north, z up; looking-down camera is `diag(1,-1,-1)`
(no mirror). Marker corners TL,TR,BR,BL; upright TL = centre + (-s/2, +s/2).
`quarter_turns` = counter-clockwise seen from above: one turn puts TL at the
south-west and the top edge faces west. `homography_from_pose` = inverse of
`K [r1 r2 t]`, undistorted pixel -> field cm (what `Calibration.to_world`
applies after `undistort_point`).

**Design points.** Orientation: >= 4 markers seed a centre homography, score
each marker's 4 quarter turns by corner residual in cm, refit the pose and
rescore (up to 2 passes); < 4 markers enumerate all 4^n turn combinations by
pose rms (one marker is inherently ambiguous, ratio ~1). The ambiguity ratio
is IPPE's runner-up/best error (LM refinement drags the runner-up onto the
same minimum and would erase it). Leave-one-out and the scale distances are
computed in `pose.py` (`leave_one_out_residuals`, `marker_distances`) and
decided in `gates.py`. `TargetObserver` is `Callable[[ndarray, Any],
Sequence[FieldCorner]]`; `FieldCorner` is a placeholder for ticket 011.

**Noise sweep** (camera 144 cm up, tilt 4 deg, 1280x720, fx 1000, k1 -0.25,
46 mm markers, worst of 40 seeds). Corner noise 0.1/0.3/0.6/1.0 px, 10-marker
main layout: rms px 0.16/0.49/0.98/1.64; rms cm 0.03/0.08/0.15/0.26;
leave-one-out worst 0.07/0.20/0.39/0.65 cm; scale |ratio-1| 0.003/0.007/0.016/0.030;
nadir error 0.08/0.23/0.46/0.77 cm; height error <= 0.12 cm; min rotation
ratio 122/41/21/12. Four corner markers at 0.3 px: nadir <= 0.36 cm, height <=
0.07 cm. Markers printed 3 % / 10 % larger than defined: scale 0.037 / 0.098,
rms 0.84 / 2.2 px. A marker displaced d cm: leave-one-out reads d cm, named
correctly (6 cm swap case in tests). Lone marker: angular spread 2.5 deg, IPPE
ratio median 1.5 (min 1.0) -- so the ambiguity ratio alone does NOT reliably
flag it; the count rule does.

**Finding added to the plan: layout-extent gate.** Two diagonal markers, or
three in a row, are fine on rms and ambiguity ratio yet roll about the
baseline is unconstrained: nadir error up to 30 cm (diag-2) / 81 cm (line-3)
at 0.3 px noise. Added `gates.layout_extent_ratio` (min/max singular value of
centred centres) with `MIN_LAYOUT_EXTENT_RATIO = 0.2` (L-shaped 3 markers
0.48, near-lines 0.05-0.11) and a `classify` cap at `low`. Ticket 006 should
feed `layout_extent_ratio` into `GateMetrics`. A tight cluster (20 cm across)
is caught by the angular-spread gate instead.

**Classification choices for 006:** rms (px or cm), scale and leave-one-out
failures return `fail` with a reason; weak geometry (angular spread, IPPE
ratio, collinear layout, ambiguous rotation) caps at `low` with notes.
