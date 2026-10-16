---
id: '004'
title: Solver, good-enough metric, next hint, and status record
status: open
use-cases: [SUC-001, SUC-003]
depends-on: ["003"]
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Solver, good-enough metric, next hint, and status record

## Description

Implement the solver and the good-enough evaluation, plus the single status record every renderer uses (sprint.md Decision 6). Solver: `cv2.calibrateCamera` with k3 fixed. Metric: max residual, after a homography, between the undistortion map of the full solve and of random half-sample solves over the central 85 % of the frame; also report rms and the size of the correction itself vs no lens model. Behaviour reference: `mapping_residual_px` and `next_hint` in `charuco_gui.py` (prototype; reproduce behaviour, not code).

## Acceptance Criteria

- [ ] `LensSolver.solve(views, image_size)` returns a result with camera_matrix, dist_coeffs, rms_px, fx, hfov_deg, and None until >= 6 usable views.
- [ ] `evaluate(views)` returns residual_px (None below MIN_VIEWS = 12), correction_px, and `good` = rms < 0.6 px and residual < 4 px; half-sample draws are seeded deterministically.
- [ ] `next_hint(coverage, tilt_dirs, tilted, view_count)` reproduces the prototype rules: <4 views -> hold board high/tilted over centre; missing corner regions first (with a tilt direction), then missing tilt directions, then edges, then more tilted views, else keep sweeping.
- [ ] `IntrinsicsStatus` (frozen kw_only dataclass in `intrinsics/interfaces.py`, cv2-free) carries every field listed in Architecture Decision 6, including `banner` (`show_board`/`keep_going`/`good_enough`) derived in one place; `IntrinsicsFinishResult` carries installed flag, reason, final status and lens summary.
- [ ] A plausibility gate (focal length vs frame width, loose band, not the single-view |k1| bounds) rejects an implausible full solve.

## Implementation Plan

Create `src/aprilcam/intrinsics/solver.py`; extend `intrinsics/interfaces.py`. Pure functions on view arrays plus a small result type; no threading here. Constants (GOOD_RESIDUAL_PX=4, GOOD_RMS_PX=0.6, FIELD_FRACTION=0.85, MIN_VIEWS=12) as named module constants.

## Testing

New `tests/aprilcam/intrinsics/test_solver.py`: synthesize views by projecting the board with known K/D (no images needed): residual small and `good` true for a well-spread set; residual large / not good for a few clustered near-flat views; correction_px near zero for a rectilinear lens and large for a barrel lens; recovered fx within tolerance; hint cases for each rule branch; banner derivation; plausibility rejection. Run `uv run pytest tests/aprilcam/intrinsics`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
