---
id: 008
title: Handle overlay messages in view_cli tag reader and render loop
status: done
use-cases:
- SUC-002
- SUC-004
depends-on:
- '006'
- '007'
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Handle overlay messages in view_cli tag reader and render loop

## Description

Update `src/aprilcam/cli/view_cli.py` to:
1. Branch in the tag reader thread on the return type of `tag_consumer.read()`,
   storing `OverlayFrame` messages in a new `_latest_overlay` variable.
2. In the render loop, after `draw_paths()`, call
   `display.draw_live_overlay(disp, overlay, homography)` if an overlay is stored.

This is the final integration point that makes overlays visible in the live view.

## Acceptance Criteria

- [x] `_latest_overlay: list = [None]` and `_overlay_lock` added at module level
      (parallel to existing `_latest_tag_frame` pattern).
- [x] Tag reader thread branches on `isinstance(msg, TagFrame)`:
      - `TagFrame` → stored in `_latest_tag_frame` (existing behavior, unchanged).
      - `OverlayFrame` → stored in `_latest_overlay`.
- [x] Render loop reads `_latest_overlay` under `_overlay_lock` and calls
      `display.draw_live_overlay(disp, overlay, homography)` after `draw_paths()`.
- [x] `draw_live_overlay` is called only when `overlay is not None` and
      `homography is not None`.
- [x] Import smoke: `uv run python -c "from aprilcam.cli.view_cli import main; print('ok')"`.
- [x] `uv run pytest tests/` passes.
- [ ] Manual verification: run `aprilcam view <camera>`, call
      `DaemonControl.publish_overlay()` from a Python shell, confirm arc/arrow
      appears in the live view within one frame and disappears after TTL.

## Implementation Plan

### Approach

1. Read `src/aprilcam/cli/view_cli.py` to find the tag reader thread function
   and the render loop (likely `_process_frame_and_tags` or similar).
2. Add `_latest_overlay: list = [None]` and `_overlay_lock = threading.Lock()`
   near the existing `_latest_tag_frame` declarations.
3. In the tag reader thread loop, after `msg = tag_consumer.read()`:
   ```python
   if isinstance(msg, TagFrame):
       with _tag_lock:
           _latest_tag_frame[0] = msg
   else:  # OverlayFrame proto
       with _overlay_lock:
           _latest_overlay[0] = msg
   ```
4. In the render function, after the `draw_paths()` call:
   ```python
   with _overlay_lock:
       overlay = _latest_overlay[0]
   if overlay is not None and homography is not None:
       display.draw_live_overlay(disp, overlay, homography)
   ```
5. Import `draw_live_overlay` from `aprilcam.ui.display` if not already imported.

### Files to Modify

- `src/aprilcam/cli/view_cli.py`

### Testing Plan

- Import smoke.
- `uv run pytest tests/`
- Manual: live camera test (described in acceptance criteria).

### Documentation Updates

None.
