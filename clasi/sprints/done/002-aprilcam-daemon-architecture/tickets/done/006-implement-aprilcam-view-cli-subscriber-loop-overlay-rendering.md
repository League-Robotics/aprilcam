---
id: '006'
title: Implement aprilcam view CLI (subscriber loop, overlay rendering)
status: done
use-cases:
- SUC-003
- SUC-005
- SUC-006
- SUC-009
depends-on:
- '005'
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement aprilcam view CLI (subscriber loop, overlay rendering)

## Description

Create `src/aprilcam/cli/view_cli.py` — the replacement for `liveview.py`'s
live-view capability. This is a stateless subscriber: it calls `ensure_running`,
connects to the camera's data socket, decodes frame messages, stats/reloads
`paths.json` on mtime change, and renders overlays using the existing
`PlayfieldDisplay.draw_overlays` and `draw_paths` methods.

This viewer runs `cv.imshow` on the main thread (macOS requirement). It never
opens a camera directly.

Also register the `view` subcommand in `src/aprilcam/cli/__init__.py`.

## Acceptance Criteria

- [x] `src/aprilcam/cli/view_cli.py` created with `main(argv)` entry point.
- [x] `aprilcam view --camera <name-or-index>` accepted:
  - If `<name-or-index>` is an integer, call `open_camera(index)` RPC to
    open the camera, get `cam_name` back.
  - If string, call `get_camera_info(name)` RPC to look up an already-open
    camera.
- [x] Viewer reads `info.json` to find the data socket path and paths file path.
- [x] Viewer connects to the data socket and enters the main display loop.
- [x] Per frame received:
  - JPEG-decodes `frame_jpeg`.
  - Stats `paths_file`; if mtime changed, reloads the JSON and updates the
    in-memory paths dict.
  - Calls `PlayfieldDisplay.draw_overlays()` for tag boxes, IDs, velocity.
  - Calls `draw_paths()` for agent-drawn paths.
  - Calls `cv.imshow()`.
  - Calls `cv.waitKey(1)`; exits on `q` or `Esc`.
- [x] If `paths_file` does not exist, viewer starts with empty paths dict
  (no crash).
- [x] If `homography` in the frame is null, `draw_paths` is a no-op
  (no crash).
- [x] On startup, loads `paths_file` immediately if it exists (paths visible
  on the very first frame).
- [x] `src/aprilcam/cli/__init__.py` updated to register `view` subcommand
  pointing to `aprilcam.cli.view_cli`.
- [x] The `live` subcommand is DELETED entirely per stakeholder decision
  (not deprecated — removed from `SUBCOMMANDS` dict completely).

## Implementation Plan

### Approach

~150 lines. The main loop is a simple `while True` with `read_frame(sock)`
(from `daemon.protocol`) and `cv.waitKey(1)`. Paths reload uses
`os.stat(paths_file).st_mtime_ns` compared to a stored value; reload only
on change. No threading needed — all I/O is on the main thread.

### Files to Create

- `src/aprilcam/cli/view_cli.py`

### Files to Modify

- `src/aprilcam/cli/__init__.py` — add `"view"` entry; update `"live"` entry
  to point to a deprecation stub or handle inline.

### Notes

- `PlayfieldDisplay` is constructed with the frame size from the first
  received frame.
- `draw_overlays` needs a `PlayfieldBoundary` object. Reconstruct it from
  `playfield_corners` in the frame message (or pass `None` if empty — check
  what `draw_overlays` does with a null playfield).
- The data socket path comes from `info.json`; the viewer must read
  `info.json` before connecting to the data socket.

### Testing Plan

No unit tests for this ticket — the render loop requires a display. Manual
verification in T010 covers end-to-end correctness. Existing `uv run pytest`
must still pass (no regressions in existing tests).

### Documentation Updates

Help text in `view_cli.py` `argparse` parser. Update `cli/__init__.py` help
string for `view` subcommand.
