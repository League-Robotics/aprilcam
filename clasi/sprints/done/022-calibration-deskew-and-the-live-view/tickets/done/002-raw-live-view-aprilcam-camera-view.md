---
id: '002'
title: 'Raw live view: aprilcam camera view'
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Raw live view: aprilcam camera view

## Description

Implements SUC-002 (parent UC-16): `aprilcam camera view <camera>` —
the raw, unprocessed live video window, per `sprint.md` Architecture
Decision 2 (both view commands ship, under the design's existing
spelling). This needs zero new daemon capability — raw `get_frame` has
existed since sprint 020 — so it has no dependency on tickets 001 or
003-005 and can be built and verified standalone. It is also the tool
the developer uses to physically aim camera 3 so all required ArUco
markers are visible before ticket 007's calibration verification.

Builds:

- `view/viewer.py` (new subsystem) — the poll-driven window core:
  connects via the client library, polls `get_frame(deskewed=False)`
  at a modest fixed rate (starting ~2 Hz per Architecture Decision 3 —
  this ticket only needs the raw half; ticket 006 adds the
  deskewed+tags half to the same core), displays the returned JPEG
  bytes directly with **no drawn overlays of any kind**, even if the
  frame happens to carry an embedded `TagFrame` (UC-16's "raw means
  raw" contract — this is a display-time rule, not a query-time one:
  do not special-case what gets requested, special-case what gets
  drawn). Detects a missing display (no `$DISPLAY`/headless) and fails
  with a clear message pointing at `camera image` instead of a toolkit
  backtrace.
- `cli/camera.py` extended — `view` subcommand wired to
  `view/viewer.py`, refuses `--all`.

**Archive pointers**: `archive/src/aprilcam/cli/` may be worth a skim
for window-lifecycle handling (open/close/signal handling), but v1's
viewer draws overlays and deskews locally with cv2 — none of that logic
transfers. This ticket's viewer is deliberately much smaller: raw bytes
only, no cv2, no drawing.

## Acceptance Criteria

- [x] `aprilcam camera view arducam-ov9782-usb-camera` opens a window
      showing live raw frames with camera settings applied
      (exposure/gain/WB as currently configured) and **no deskew, no
      tag boundaries/hats, no annotations** — verified even when the
      same camera has an active, recently-queried ring buffer
      (detection running) (UC-16 "raw means raw," UC-16.S-1).
- [x] Closing the window stops polling; the daemon returns to idle
      capture for that camera once no other subscriber remains.
- [x] Run with no display available, the command exits with a clear
      error naming `camera image` as the alternative, not a
      GUI-toolkit stack trace (UC-16.S-3).
- [x] `aprilcam camera view --all` is refused, explaining that viewing
      is single-target (UC-16 fan-out rule, matching `camera
      calibrate`'s `--all` refusal in ticket 003).
- [x] Frames too dark to see anything are still displayed, with the
      advisory `frame_dark` flag surfaced in the window/output rather
      than left for the developer to guess.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/` —
  confirm `camera list`/`probe`/`image`/`tags` are untouched by the new
  `view` subcommand.
- **New tests to write**: `tests/aprilcam/view/test_viewer.py` —
  headless: poll loop against a fake/stub client (frame delivery
  cadence, stop-on-close, headless-detection branch triggering the
  correct error) with no real GUI toolkit instantiated;
  `tests/aprilcam/cli/test_camera_cli.py` additions for `--all`
  refusal and argument parsing. No `@pytest.mark.camera` needed — the
  poll-loop and error-path logic is fully fakeable; whether it actually
  looks right on screen is a manual/stakeholder check, not an automated
  test.
- **Verification command**: `uv run pytest && uv run mypy` clean; zero
  `typing.Protocol`; baseline 388 passing, additive only.
