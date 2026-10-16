---
id: '002'
title: ChArUco board spec and multi-board detector (clustering, upscale, layout auto-detect)
status: done
use-cases:
- SUC-001
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# ChArUco board spec and multi-board detector (clustering, upscale, layout auto-detect)

## Description

Create the `intrinsics` package's detection layer: `BoardSpec` parsing/validation (cv2-free, in `intrinsics/interfaces.py`) and the daemon-side `BoardDetector` that finds every physical ChArUco board in a frame. Every constant and gotcha is in `docs/knowledge/2026-10-15-charuco-intrinsics-calibration-lessons.md` (detection gotchas 1-3). The prototype under `docs/knowledge/assets/charuco-prototype/` (`charuco_cal.py`: `marker_clusters`, `detect_boards`, `_tuned_params`) is evidence only: write fresh, with layout-lock state per detector instance rather than a module global.

## Acceptance Criteria

- [x] `BoardSpec.parse('5x7:37:28:DICT_5X5_100')` yields cols, rows, square_mm, marker_mm, dict_name; malformed specs raise a clear `ValueError`; default constant is `5x7:37:28:DICT_5X5_100`; resolution order request > env `APRILCAM_CHARUCO_BOARD` > default is implemented in one helper.
- [x] `BoardDetector.detect(gray)` returns one record per board cluster (charuco corners, ids, marker count, layout); markers detected on a 2x upscale with the tuned parameters, corners interpolated on the native image so coordinates stay native.
- [x] Marker clustering (single-linkage, 2.5x marker side, no duplicate ids per cluster, >= 4 markers) isolates the board from playfield markers sharing its dictionary and separates two boards in view.
- [x] Current vs legacy layout is auto-detected the first time markers are seen but no corners interpolate, then locked for that detector instance; the lock is exposed for status.
- [x] `interfaces.py` imports no cv2; the detector module is daemon-only.

## Implementation Plan

Create `src/aprilcam/intrinsics/__init__.py`, `interfaces.py` (BoardSpec, layout enum; real classes, no `typing.Protocol`), `detector.py`. Dictionary-name -> cv2 constant resolution happens in `detector.py` only. Keep the detector stateless apart from the layout lock; frame in, records out.

## Testing

New `tests/aprilcam/intrinsics/test_detector.py`, hardware-free, using `cv2.aruco.CharucoBoard.generateImage` plus a perspective warp onto a canvas: marker clustering with field-marker collisions (same dictionary, overlapping ids drawn on the canvas), two boards in one frame yielding two records, legacy vs current layout auto-detect and lock, upscale path finding a board at small marker size, BoardSpec parse/refuse cases and env precedence. Run `uv run pytest tests/aprilcam/intrinsics`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
