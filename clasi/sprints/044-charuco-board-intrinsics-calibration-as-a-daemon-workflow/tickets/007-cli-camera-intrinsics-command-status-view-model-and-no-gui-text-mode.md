---
id: '007'
title: CLI camera intrinsics command, status view-model, and --no-gui text mode
status: open
use-cases: [SUC-001, SUC-002]
depends-on: ["006"]
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI camera intrinsics command, status view-model, and --no-gui text mode

## Description

Add `aprilcam camera intrinsics <camera...> [--board SPEC] [--install] [--no-gui] [-d HOST]`, the pure status-to-view-model function, and the `--no-gui` text mode. This ticket delivers a complete SSH-capable workflow; the Tk GUI (ticket 008) renders the same view-model. Thin client: only polls `get_intrinsics_status`; no cv2.

## Acceptance Criteria

- [ ] `cli/intrinsics.py` registered from `cli/camera.py`; starts one session per named camera (fresh), polls status at ~2-4 Hz, and on exit always aborts any session it still owns (Esc/Ctrl-C/error) so no camera stays pinned.
- [ ] `view/intrinsics_model.py` (pure, Tk-free, cv2-free): `status -> view model` (banner text and colour class, per-camera 'error now' and 'could still change by' strings, 3x3 tile states, four tilt tile states, one NEXT line, rejection note, errors) and `status -> text line(s)` for text mode.
- [ ] `--no-gui` prints those lines and finishes with Enter on stdin (install), Ctrl-C aborts; `--install` is the only way to install without confirmation and still only when GOOD ENOUGH. Never auto-quits on green.
- [ ] `--board` is validated client-side with `BoardSpec.parse` before any RPC; malformed spec exits non-zero with a clear message.
- [ ] Without `--no-gui`, the command calls the GUI entry point from ticket 008 when a display is available and falls back to text mode with a notice when not (`_headless_reason` precedent in `view/viewer.py`); until 008 lands the GUI hook is a clearly named stub that selects text mode.

## Implementation Plan

Create `src/aprilcam/cli/intrinsics.py`, `src/aprilcam/view/intrinsics_model.py`; modify `cli/camera.py` to register the subcommand. Follow `cli/flatfield.py`/`cli/calibrate.py` for `discovery_factory`, `report_error`, `add_daemon_option`. Multi-camera: positional `camera` with nargs='+'.

## Testing

New `tests/aprilcam/cli/test_intrinsics_cli.py` and `tests/aprilcam/view/test_intrinsics_model.py` with a fake daemon: banner/tile/hint rendering for show-board, keep-going, good-enough statuses; text lines contain the same fields; abort on exit; board spec validation; two cameras; headless fallback. Run `uv run pytest tests/aprilcam/cli tests/aprilcam/view tests/test_no_cv2_in_client_modules.py`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
