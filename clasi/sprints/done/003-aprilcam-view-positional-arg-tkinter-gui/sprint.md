---
id: '003'
title: "aprilcam view \u2014 Positional Arg + tkinter GUI"
status: done
branch: sprint/003-aprilcam-view-positional-arg-tkinter-gui
use-cases:
- SUC-001
- SUC-002
issues:
- plan-aprilcam-view-positional-argument-tkinter-gui.md
---

# Sprint 003: aprilcam view — Positional Arg + tkinter GUI

## Goals

Replace the OpenCV-based live view window in `aprilcam view` with a proper
tkinter GUI. Change the `--camera` named argument to a positional `camera`
argument. Add the Pillow dependency required for BGR→PhotoImage conversion.

## Problem

`aprilcam view` currently requires `--camera <NAME_OR_INDEX>` as a named
flag — awkward UX for the only required argument. The rendering loop is pure
OpenCV (`cv.imshow` + `cv.waitKey`), which provides no real GUI chrome: no
native window controls, no status bar, no path for adding buttons. The status
information (FPS, tag count, calibration state) is drawn onto the video frame
as pixel overlay rather than displayed in real widget labels.

## Solution

Two targeted changes to `view_cli.py` and one dependency addition:

1. **Positional argument**: Change `--camera` (named, required) to `camera`
   (positional). Callers move from `aprilcam view --camera 2` to
   `aprilcam view 2`.

2. **tkinter two-thread rendering model**: Replace the single-threaded
   `cv.imshow`/`cv.waitKey` loop with a two-thread design:
   - Reader thread: reads frames from the daemon data socket, runs the
     existing display pipeline (`prepare_display`, `draw_overlays`,
     `draw_paths`), puts `(frame_bgr, status_dict)` into a capped
     `queue.Queue(maxsize=2)`.
   - tkinter main thread: `root.after(33, _poll)` pulls the latest frame,
     converts BGR→RGB→PIL→PhotoImage, updates the canvas, and updates status
     bar labels.
   - Window close, `q`, and `Escape` set a `threading.Event` to stop both
     threads cleanly.
   - `draw_status_panel()` call removed; info moves to tkinter label widgets.

3. **Pillow dependency**: Add `pillow>=10.0` to `pyproject.toml`. Required
   for `ImageTk.PhotoImage` conversion.

All existing logic (daemon connection, `PlayfieldDisplay` usage, paths file
mtime-watching) is kept unchanged.

## Success Criteria

- `uv run aprilcam view 2` opens a tkinter window showing the camera feed.
- `uv run aprilcam view "Arducam"` (name pattern) still works.
- `uv run aprilcam view` (no arg) prints argparse usage error.
- `uv run aprilcam view --help` shows `CAMERA` as positional arg.
- Window close (×), `q`, `Escape` all exit cleanly with no orphan threads.
- Status bar updates live: FPS, tag count, calibration state, deskew mode.
- Overlays render correctly (tag boxes, velocity arrows, paths if any).
- `uv run pytest` passes with no regressions.

## Scope

### In Scope

- `src/aprilcam/cli/view_cli.py`: positional arg + tkinter two-thread loop.
- `pyproject.toml`: add `pillow>=10.0`.

### Out of Scope

- `src/aprilcam/ui/display.py` — all drawing code stays as-is.
- `src/aprilcam/ui/tui.py` — untouched.
- `src/aprilcam/daemon/` — untouched.
- Adding buttons, menus, or other GUI chrome beyond the status bar.
- Streamable HTTP transport or any network changes.

## Test Strategy

`view_cli.py` has no existing unit tests and the GUI loop requires a live
camera. Manual verification covers the full acceptance list above. The
existing pytest suite (`test_paths.py`, `test_mcp_path_tools.py`,
`test_draw_paths.py`) must continue to pass unchanged — these test unrelated
modules and serve as regression guards.

## Architecture Notes

- tkinter must own the main thread. The reader loop moves to a daemon thread.
- `queue.Queue(maxsize=2)` with drop-on-full keeps the UI responsive when
  the reader is faster than the poll interval.
- `canvas._photo_ref = photo` prevents Python GC from collecting the current
  `PhotoImage` before the canvas renders it.
- Pillow is needed only in the poll callback; the reader thread stays pure
  OpenCV/NumPy.

## GitHub Issues

(None linked.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| T001 | Add pillow>=10.0 dependency to pyproject.toml | — |
| T002 | Rewrite view_cli.py — positional arg + tkinter two-thread loop | T001 |

Tickets execute serially in the order listed.
