---
id: '011'
title: ChArUco target observer and Calibrator target path
status: done
use-cases:
- SUC-006
depends-on:
- '006'
- '010'
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# ChArUco target observer and Calibrator target path

## Description

Implement the target path. `intrinsics/target.py` (cv2) provides
`ChArUcoTargetObserver`, a callable matching the `TargetObserver` signature
defined in `calibration/pose.py` (ticket 004): given a grayscale frame and a
`CalibrationTarget`, build the `BoardSpec`, run `BoardDetector` (reused from
sprint 044, its 2x upscale and layout lock included), and return field-frame
correspondences `[(pixel_xy, field_xy_cm)]` by mapping each ChArUco corner id
through `collector.object_points` (board metres -> cm) and then the target
placement (centre, rotation, and the board-y-down to field-y-north flip --
the single y flip, pinned by test). It imports `config.interfaces` but nothing
from `calibration` (sprint.md Decision 6); `DaemonService` constructs it and
passes it to `Calibrator` (one added keyword argument).

In `Calibrator`: when the definition has a `calibration_target` and the
observer is supplied, run it over the burst (average corners per id across
frames), feed the correspondences to the same `solve_planar_pose`, and apply
the same gates (spread and reprojection; scale agreement is N/A, noted).
Policy for definitions with both: markers and target observations are pooled
when both are visible; the label names what contributed (`pose:
markers+target`). A target that cannot be read (fewer corners than the minimum,
board not found) fails with a clear reason when the definition has no markers,
and falls back to the marker solve with a note when it does. Needs a measured
lens (same rule as markers). The target path never persists the board
anywhere; the board is removed after calibration by the operator.

## Acceptance Criteria

- [x] Synthetic ChArUco board (use `tests/aprilcam/intrinsics/boards.py`
      renderer) laid at the origin of a marker-free definition, measured
      lens: calibration succeeds with `method: pose`, 24+ corners.
- [x] **Agreement:** on the same synthetic camera, the board-based
      calibration and a four-marker calibration map a grid of field points
      (including points where the board was NOT) to within 0.5 cm.
- [x] Orientation/centre convention verified: moving `centre_x_cm` /
      `rotation_deg` in the definition shifts/rotates the resulting field
      frame accordingly (north-up, no mirror).
- [x] Board not found / too few corners: clear failure reason, previous
      calibration untouched.
- [x] No `calibration -> intrinsics` import anywhere (add an import-graph
      test); `Calibrator` works unchanged when no observer is supplied.

## Implementation Plan

**Files to create:** `src/aprilcam/intrinsics/target.py`,
`tests/aprilcam/intrinsics/test_target.py`.
**Files to modify:** `src/aprilcam/calibration/calibration.py` (target path),
`src/aprilcam/daemon/service.py` (construct + inject; wiring only).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/intrinsics tests/aprilcam/calibration -x`
- **New tests to write**: as listed; reuse `boards.render` with known
  pose/distortion.
- **Verification command**: `uv run pytest tests/aprilcam/intrinsics tests/aprilcam/calibration tests/aprilcam/daemon`

## Documentation Updates

`intrinsics/DESIGN.md` (new module, new edge `intrinsics -> config`);
ticket 012 for the rest.
