---
id: 008
title: Fullscreen Tk intrinsics GUI
status: done
use-cases:
- SUC-001
depends-on:
- '007'
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fullscreen Tk intrinsics GUI

## Description

The fullscreen Tk GUI the stakeholder accepted on 2026-10-15 (`docs/knowledge/assets/charuco-prototype/charuco_gui.py` is the visual reference): banner (SHOW ME THE BOARD / KEEP GOING / GOOD ENOUGH), per-camera 'error now' and 'could still change by' in very large type, 3x3 region tiles, tilt tiles, one NEXT line. Pure rendering of `intrinsics_model` output; follow `view/viewer.py` conventions (`TkWindow`, `_ensure_tk_library_env`, `_headless_reason`, `NoDisplayAvailable`).

## Acceptance Criteria

- [x] `view/intrinsics_gui.py` renders the ticket-007 view-model fullscreen; fonts scale from screen height (banner about H/9, readable from 10 feet); several cameras side by side.
- [x] Enter finishes and installs (calls finish with install, shows the result including 'run calibrate to apply'); Esc aborts; never auto-quits on green; closing the window aborts sessions.
- [x] Polling runs off the Tk thread's blocking path (Tk `after` or a poller thread feeding a single-slot cell, as `StreamLoop` does); a dropped daemon connection shows an error banner instead of freezing.
- [x] No cv2 import; display-less hosts raise `NoDisplayAvailable` and the CLI falls back to text mode.
- [x] Layout/colour logic stays in `intrinsics_model.py` so it is testable without a display.

## Implementation Plan

Create `src/aprilcam/view/intrinsics_gui.py`; replace the GUI stub in `cli/intrinsics.py`. Keep widget code a thin shell over the view-model.

## Testing

Tests without a display: view-model scaling function (font sizes from screen height), key-handling logic as plain functions (Enter at good vs not good, Esc), `NoDisplayAvailable` fallback path, no-cv2 import check. Manual visual check is part of ticket 012. Run `uv run pytest tests/aprilcam/view tests/aprilcam/cli tests/test_no_cv2_in_client_modules.py`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
