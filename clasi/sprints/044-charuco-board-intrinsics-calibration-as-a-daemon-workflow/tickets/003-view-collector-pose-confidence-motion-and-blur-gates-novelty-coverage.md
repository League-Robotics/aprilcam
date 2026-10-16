---
id: '003'
title: 'View collector: pose confidence, motion and blur gates, novelty, coverage'
status: done
use-cases:
- SUC-001
- SUC-006
depends-on:
- '002'
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# View collector: pose confidence, motion and blur gates, novelty, coverage

## Description

Implement the view collector: given detector output per frame, decide which boards become kept *views* and tally coverage. Behaviour per the knowledge entry, gotchas 4-6: distinct by corner displacement, steady by motion vs previous frame, sharp relative to the camera's running best, confident pose by IPPE two-solution ambiguity. Fresh state per session.

## Acceptance Criteria

- [x] `ViewCollector.offer(gray, boards, ts)` returns per-board verdicts with a machine-readable reject reason (`moving`, `blurry`, `not_novel`, `ambiguous_pose`, `too_few_corners`) and keeps accepted views (corner/id arrays plus tilt, azimuth, extent), never frames.
- [x] Motion gate: mean corner displacement vs the previous frame > 12 px marks moving (two boards matched by nearest centroid). Blur gate: Laplacian variance over the board ROI below 45 % of the camera's running best marks blurry.
- [x] Novelty: mean corner shift vs every kept view >= 25 px or tilt change >= 10 degrees; MIN_CORNERS = 12.
- [x] Pose confidence: tilt/azimuth are trusted only with >= 18 corners and runner-up IPPE error > 1.3x the best; a flat board must not tick tilt items.
- [x] Coverage is by board extent (any detected corner reaching a region) over outer 40 % bands: a 3x3 region count grid and N/E/S/W tilt-direction counts plus tilted_count.
- [x] Collector holds no module-level state; two instances are independent (fresh-session semantics).

## Implementation Plan

Create `src/aprilcam/intrinsics/collector.py`. Constants as named module constants with the knowledge-entry rationale in comments. Reuse `cv2` only for IPPE/Laplacian; no I/O.

## Testing

New `tests/aprilcam/intrinsics/test_collector.py`: synthetic rendered/warped board sequences: pose-confidence rejection of a flat board, accepted steep tilt, relative blur gate (same board blurred with a Gaussian vs the camera's best), motion gate on a fast-moving sequence versus slow sweep, novelty, coverage by extent (a large board near centre covers outer cells), two collectors independent. Run `uv run pytest tests/aprilcam/intrinsics`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
