---
id: '002'
title: "Rewrite view_cli.py \u2014 positional arg + tkinter two-thread loop"
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: plan-aprilcam-view-positional-argument-tkinter-gui.md
completes_issue: true
---

# Rewrite view_cli.py — positional arg + tkinter two-thread loop

## Description

Rewrite `src/aprilcam/cli/view_cli.py` to replace the OpenCV rendering loop
with a tkinter two-thread model and change the `--camera` named argument to a
positional `camera` argument.

The full specification is in:
`.clasi/issues/plan-aprilcam-view-positional-argument-tkinter-gui.md`

**Summary of changes:**

1. **Positional argument**: Replace `parser.add_argument("--camera", required=True, ...)` with `parser.add_argument("camera", metavar="CAMERA", ...)`.

2. **Reader thread**: A `threading.Thread(daemon=True)` runs the blocking
   `read_frame()` loop. For each frame it: decodes JPEG, updates
   `PlayfieldDisplay`, runs `draw_overlays()` and `draw_paths()`, builds a
   `status_dict` (`{fps, tag_count, calibrated, deskew_mode}`), and puts
   `(frame_bgr, status_dict)` into a `queue.Queue(maxsize=2)` using
   `put_nowait()` with silent drop on `queue.Full`.

3. **tkinter main thread**: Creates `tk.Tk()` window titled
   `"aprilcam view — <cam_name>"`. Canvas sized to frame dimensions from first
   frame (bootstrap: read one frame synchronously before starting the thread,
   or resize on first poll). Status bar: `tk.Frame` below canvas with four
   `tk.Label` widgets for FPS, tag count, calibrated, and deskew. Uses
   `root.after(33, _poll)` to pull frames from the queue, convert with Pillow,
   update canvas image item and labels.

4. **Image conversion** (in `_poll`):
   ```python
   rgb = cv.cvtColor(frame_bgr, cv.COLOR_BGR2RGB)
   pil_img = Image.fromarray(rgb)
   photo = ImageTk.PhotoImage(pil_img)
   canvas.itemconfig(img_item, image=photo)
   canvas._photo_ref = photo  # prevent GC
   ```

5. **Window lifecycle**: Bind `WM_DELETE_WINDOW`, `<q>`, `<Escape>` to a
   `_on_close()` handler that sets `stop_event` and calls `root.destroy()`.

6. **Remove**: `draw_status_panel()` call, all `cv.imshow()`, `cv.waitKey()`,
   `cv.destroyAllWindows()` calls.

7. **Keep unchanged**: `_tag_dict_to_aprilcam()`, `_load_paths()`, daemon
   connection logic, paths file mtime-watching, all `PlayfieldDisplay` usage.

8. **Check `mcp_server.py`**: Find the `start_live_view` handler's
   `subprocess.Popen` call. If it passes `--camera NAME`, update it to pass
   the name as a positional argument instead.

## Acceptance Criteria

- [x] `parser.add_argument("camera", ...)` — no `--` prefix, no `required=True`.
- [x] `uv run aprilcam view 2` opens a tkinter window (not OpenCV).
- [x] `uv run aprilcam view "Arducam"` resolves by name and opens the window.
- [x] `uv run aprilcam view` (no arg) prints argparse usage error, exits non-zero.
- [x] `uv run aprilcam view --help` shows `CAMERA` as positional argument.
- [x] Window close (×), `q`, `Escape` all exit cleanly, no orphan threads.
- [x] Status bar shows four labels: FPS, Tags, Calibrated, Deskew — all update.
- [x] Tag overlays and paths render on the canvas (same as before).
- [x] `draw_status_panel()` is not called anywhere in the new code.
- [x] No `cv.imshow()`, `cv.waitKey()`, or `cv.destroyAllWindows()` in file.
- [x] `mcp_server.py` `start_live_view` Popen call passes camera name positionally.
- [x] `uv run pytest` passes with no regressions.

## Implementation Plan

### Approach

Rewrite `main()` in `view_cli.py`. Keep the module's helpers unchanged
(`_tag_dict_to_aprilcam`, `_load_paths`). Extract the display loop into a
`_reader_thread` function and a `_poll` callback. Add a `_build_window` helper.

**Bootstrap strategy**: The tkinter canvas must be sized to the frame
dimensions before the first frame arrives. Read the first frame synchronously
on the main thread (before `tk.Tk()` is created), record `(frame_w, frame_h)`,
then build the window and start the reader thread. Put that first frame into
the queue immediately so the poll callback has something to show right away.

### Files to Modify

| File | Change |
|------|--------|
| `src/aprilcam/cli/view_cli.py` | Full rewrite of `main()` and supporting functions |
| `src/aprilcam/server/mcp_server.py` | Update `start_live_view` Popen call if it uses `--camera` |

### Testing Plan

- Manual verification per the Acceptance Criteria list above.
- `uv run pytest` — no new unit tests (the GUI loop cannot be unit-tested
  without a display); existing suite is the regression guard.
- For CI/headless environments: the Popen call in `mcp_server.py` is the only
  structural change; it can be grep-verified.

### Documentation Updates

None required. The argument change is self-documenting via `--help`.
