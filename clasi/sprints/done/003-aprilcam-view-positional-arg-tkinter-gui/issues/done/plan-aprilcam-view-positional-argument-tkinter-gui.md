---
status: done
sprint: '003'
tickets:
- '002'
---

# Plan: aprilcam view — Positional Argument + tkinter GUI

## Context

`aprilcam view` currently requires `--camera <NAME_OR_INDEX>` as a named flag, which is awkward for the only required argument. The rendering loop is also pure OpenCV (`cv.imshow` + `cv.waitKey`), which gives no real GUI chrome — no window title controls, no status bar, no way to add buttons later. The goal is to make the camera argument positional and replace the OpenCV window with a proper tkinter window that has a live image canvas and a minimal status bar.

The existing OpenCV drawing pipeline (`display.py`: `draw_overlays`, `draw_paths`, `prepare_display`) stays intact — it produces a BGR numpy array which we convert to a tkinter PhotoImage. The status panel that was drawn onto the image (`draw_status_panel`) is replaced by real tkinter label widgets.

---

## Changes

### 1. `pyproject.toml`
Add `pillow>=10.0` to `dependencies`. Required to convert OpenCV BGR numpy arrays to tkinter-displayable `ImageTk.PhotoImage`.

### 2. `src/aprilcam/cli/view_cli.py` — full rewrite of `main()`

#### Argument change
Replace:
```python
parser.add_argument("--camera", required=True, metavar="NAME_OR_INDEX", ...)
```
With:
```python
parser.add_argument("camera", metavar="CAMERA", help="Camera name or integer index")
```
Callers change from `aprilcam view --camera 2` → `aprilcam view 2`.

#### Architecture: two-thread model

```
[Reader thread]                         [tkinter main thread]
  read_frame() from daemon socket   →   queue.Queue   →   root.after(33, poll)
  decode JPEG                                               pull frame from queue
  display.prepare_display()                                 convert BGR→RGB→PIL→PhotoImage
  draw_overlays()                                           canvas.itemconfig(image=...)
  draw_paths()                                              update status labels
  put(frame_bgr, status_dict) →
```

**Reader thread responsibilities:**
- Block on `read_frame()` from the daemon data socket
- Decode JPEG, update `PlayfieldDisplay`, run `draw_overlays()` and `draw_paths()`
- Build a small `status_dict`: `{fps, tag_count, calibrated, deskew_mode}`
- Put `(frame_bgr, status_dict)` into a `queue.Queue(maxsize=2)` (drop oldest if full, prevents lag buildup)
- Stop when a threading `Event` is set

**tkinter main thread:**
- Creates `tk.Tk()` window with title `"aprilcam view — <cam_name>"`
- `tk.Canvas` sized to the camera's frame dimensions (from first frame)
- Status bar: a `tk.Frame` below the canvas with `tk.Label` widgets for FPS, tag count, calibration state
- `root.after(33, _poll)` callback pulls latest frame from queue, converts to `ImageTk.PhotoImage`, updates canvas image item and status labels
- Window close (`WM_DELETE_WINDOW`) sets the stop event and calls `root.destroy()`
- Keyboard: bind `<q>` and `<Escape>` to close

**Image conversion (in poll callback):**
```python
import cv2 as cv
from PIL import Image, ImageTk

rgb = cv.cvtColor(frame_bgr, cv.COLOR_BGR2RGB)
pil_img = Image.fromarray(rgb)
photo = ImageTk.PhotoImage(pil_img)
canvas.itemconfig(img_item, image=photo)
canvas._photo_ref = photo  # prevent GC
```

**Status bar labels (below canvas):**
- `FPS: 28.4`
- `Tags: 3`
- `Calibrated: yes` / `Calibrated: no`
- `Deskew: on` / `Deskew: off`

All in a single row, separated by `|` spacers.

#### Remove
- `draw_status_panel()` call (info moves to status bar labels)
- All `cv.imshow()`, `cv.waitKey()`, `cv.destroyAllWindows()` calls

#### Keep unchanged
- `_tag_dict_to_aprilcam()` helper
- `_load_paths()` helper
- Daemon connection logic (control socket → get cam info → data socket)
- Paths file mtime-watching and reload
- All `PlayfieldDisplay` usage (`prepare_display`, `draw_overlays`, `draw_paths`)

---

## Files Modified

| File | Change |
|------|--------|
| `src/aprilcam/cli/view_cli.py` | Positional arg + tkinter loop replacing cv.imshow |
| `pyproject.toml` | Add `pillow>=10.0` dependency |

**Unchanged:**
- `src/aprilcam/ui/display.py` — all drawing code stays as-is
- `src/aprilcam/ui/tui.py` — untouched
- `src/aprilcam/daemon/` — untouched

---

## Verification

1. `uv run aprilcam view 2` — opens tkinter window showing Global Shutter Camera feed
2. `uv run aprilcam view "Arducam"` — name pattern still works
3. `uv run aprilcam view` (no arg) — argparse prints usage error
4. `uv run aprilcam view --help` — shows `CAMERA` as positional arg
5. Window close (×), `q`, `Escape` all exit cleanly
6. Status bar updates live: FPS ticks, tag count changes when tags appear/disappear
7. Overlays still render (tag boxes, velocity arrows, paths if any)
8. `uv run pytest` — no regressions (view_cli has no unit tests; existing tests unaffected)
