---
id: '004'
title: 'Planar pose solve and gates: calibration/pose.py and gates.py'
status: open
use-cases:
- SUC-001
- SUC-003
- SUC-005
depends-on: ['001', '003']
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

- [ ] Synthetic ground truth: a camera at 144 cm over nadir (3.47, -2.85)
      (the camera-2 numbers), tilt a few degrees, looking at four corner
      markers of the secondary field, with a realistic distortion; the solve
      recovers height within 1 cm and nadir within 1 cm without noise, and
      within 2 cm with 0.3 px corner noise.
- [ ] Derived homography reproduces the true pixel -> world mapping for
      arbitrary points (north-up, no mirror; east = +x), checked at points
      off the markers.
- [ ] 16 corners from four markers; two-marker diagonal (8) and one-marker
      (4) solves still return a pose (flagged by ambiguity ratio where
      applicable).
- [ ] Orientation detection: a synthetic scene with markers at all four
      rotations (0, +90, 180 i.e. upside down, -90) solves to the same pose
      (height, nadir, homography) as the same scene with every marker
      upright, within noise tolerance; the detected quarter-turn of each
      marker equals the one used to render it. A mixed layout matching the
      main field (1, 2, 3 upright, 4 +90, 8 -90, 5, 6, 7, 9, 10 upside down)
      is one of the cases.
- [ ] Quarter-turn convention pinned: with the wrong rotation forced, the
      residual is large (so the detector's choice is meaningful), and the
      winner/runner-up ratio is reported.
- [ ] Leave-one-out residual: for each marker, fit the pose to the others and
      report that marker's centre error in cm; synthetic test displaces one
      marker's surveyed position (the swapped-9/10 case: two markers
      exchanged) and the worst id is named correctly; clean data stays below
      the threshold. Worked reference: 0.75 cm worst on the corrected main
      field, 30 cm rms before the 9/10 correction.
- [ ] Gate functions unit-tested for every branch of the degradation ladder
      and for threshold edges; threshold defaults justified by a noise sweep
      recorded in the ticket's implementation notes.
- [ ] `gates.py` imports no cv2 (`tests/test_no_cv2_in_client_modules.py`
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
