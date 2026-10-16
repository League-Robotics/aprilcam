---
id: '005'
title: Intrinsics session, manager, and DaemonService integration (install without
  restart)
status: open
use-cases: [SUC-001, SUC-003, SUC-004, SUC-006]
depends-on: ["001", "004"]
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Intrinsics session, manager, and DaemonService integration (install without restart)

## Description

Wire detector, collector and solver into running sessions inside the daemon and integrate with `DaemonService` (Decisions 4 and 5). `IntrinsicsSession` runs a capture/detect thread and a solver thread and publishes an immutable status snapshot; `IntrinsicsManager` keeps at most one session per camera, auto-aborts when status polling stops, and aborts everything on shutdown. `finish(install)` runs a final synchronous solve and writes the `LensStore`, so the next `calibrate` in the running daemon uses it with no restart.

## Acceptance Criteria

- [ ] `DaemonService` implements `start_intrinsics(camera, board=None)`, `get_intrinsics_status(camera)`, `finish_intrinsics(camera, install, force=False)`, `abort_intrinsics(camera)` and the `Daemon` base class declares them (NotImplementedError defaults, docstrings per the sprint 037 pattern). Camera tokens resolve like `calibrate` (number or slug).
- [ ] Frame source is the resolved camera's `Camera.read()` at a capped rate (~8 fps); capture and solver run on separate threads; the status snapshot is swapped under a lock so polling never blocks on a solve.
- [ ] Starting a session on a camera with an active session raises an explicit error; after finish or abort a new start begins at zero views (fresh-session semantics).
- [ ] `finish(install=True)` refuses a not-good or plausibility-failing result unless `force`, runs a final full solve over all views, writes `intrinsics.json` atomically with provenance (board spec, views, rms, residual, correction, timestamp, source 'measured'), and reports 'run calibrate to apply'.
- [ ] A mid-session resolution change sets an error in status and blocks install; an unreadable camera sets an error rather than hanging.
- [ ] No status polling for 120 s aborts the session and releases the camera; `DaemonService.close()` aborts all sessions.
- [ ] An immediate `DaemonService.calibrate` after an install uses the new lens model without constructing a new service.

## Implementation Plan

Create `src/aprilcam/intrinsics/session.py`, `manager.py`; modify `daemon/service.py` (construct manager with injected frame-source/clock/install callbacks; keep logic out of the service), `daemon/interfaces.py`. Reuse `_camera_for`/`_resolve_slug_if_known`. Inject the clock and a frame-source callable so tests need no threads sleeping on real time where avoidable.

## Testing

New `tests/aprilcam/intrinsics/test_session.py` and `tests/aprilcam/daemon/test_service_intrinsics.py` with a fake camera yielding rendered, warped synthetic board frames (hardware-free): session reaches `good` on a generated spread of views; fresh-session semantics; double start refused; abort installs nothing; finish with install writes store and the next `calibrate` carries it; idle auto-abort with an injected clock; resolution change error; close() aborts. Run `uv run pytest tests/aprilcam/intrinsics tests/aprilcam/daemon`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
