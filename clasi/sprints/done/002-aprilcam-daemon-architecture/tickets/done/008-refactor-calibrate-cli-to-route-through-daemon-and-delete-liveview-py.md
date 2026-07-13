---
id: 008
title: Refactor calibrate CLI to route through daemon and delete liveview.py
status: done
use-cases:
- SUC-008
depends-on:
- '007'
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Refactor calibrate CLI to route through daemon and delete liveview.py

## Description

Two changes in this ticket:

1. Refactor `src/aprilcam/cli/calibrate_cli.py` to route all camera access
   through the daemon. Instead of calling `cv.VideoCapture` directly, it calls
   `ensure_running(config)` and then issues control RPCs (`open_camera`,
   `capture_frame`, `get_calibration_save_path`, `reload_calibration`). The
   homography math is unchanged.

2. Delete `src/aprilcam/ui/liveview.py` and the associated test file
   `tests/test_live_view_ipc.py`. After T007 removed the `liveview` import
   from `mcp_server.py`, no remaining code in the project imports `liveview`.

This ticket completes the removal of all direct camera access from client code.

## Acceptance Criteria

- [x] `calibrate_cli.py` calls `ensure_running(config)` at startup.
- [x] `calibrate_cli.py` issues `open_camera(index)` RPC instead of opening
  `cv.VideoCapture` directly.
- [x] For each calibration frame, `calibrate_cli.py` issues `capture_frame(cam_name)`
  RPC and uses the returned frame bytes — homography computation logic unchanged.
- [x] `calibrate_cli.py` issues `get_calibration_save_path()` RPC and writes
  the calibration JSON to the returned path.
- [x] `calibrate_cli.py` issues `reload_calibration(cam_name)` RPC after
  saving the calibration.
- [x] `src/aprilcam/ui/liveview.py` deleted.
- [x] `tests/test_live_view_ipc.py` deleted.
- [x] No remaining `import liveview` or `from aprilcam.ui import liveview`
  anywhere in the codebase.
- [x] `uv run pytest` passes (no test references the deleted test file).
- [x] `aprilcam calibrate` still runs (even without camera hardware — help
  and argument parsing work).

## Implementation Plan

### Approach

`calibrate_cli.py` changes are surgical: replace the `cv.VideoCapture` open
with `ensure_running(config).rpc("open_camera", index=index)`, and replace
each `cap.read()` with `ensure_running(...)` client `rpc("capture_frame",
cam_name=cam_name)`. Delete `liveview.py` and its test file with `git rm`.

### Files to Modify

- `src/aprilcam/cli/calibrate_cli.py` — replace direct camera access with
  daemon RPCs.

### Files to Delete

- `src/aprilcam/ui/liveview.py`
- `tests/test_live_view_ipc.py`

### Notes

- The `ControlClient` returned by `ensure_running` should be stored and reused
  across the calibration session (not re-connected per frame).
- The `capture_frame` RPC returns raw frame bytes (not JPEG) so the calibration
  code can work with them directly as a numpy array via `cv.imdecode` or
  `np.frombuffer`.
- If `liveview.py` does not import `calibrate_cli.py` (verify with grep),
  deleting it is safe after T007.

### Testing Plan

- `uv run pytest` — all remaining tests pass with `liveview.py` gone.
- Verify `from aprilcam.ui import liveview` fails with ImportError (file is
  gone). A simple grep for `liveview` in `*.py` files should return zero
  results.

### Documentation Updates

None required. The calibration flow is an implementation detail not documented
externally.
