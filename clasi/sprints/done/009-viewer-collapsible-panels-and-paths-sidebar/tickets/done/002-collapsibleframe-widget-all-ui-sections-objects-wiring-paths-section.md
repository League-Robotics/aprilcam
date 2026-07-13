---
id: "002"
title: "CollapsibleFrame widget, all UI sections, Objects wiring, Paths section"
status: done
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004, SUC-005]
depends-on: ["001"]
github-issue: ""
issue: "aprilcam-viewer-collapsible-panels-and-paths-sidebar-section.md"
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CollapsibleFrame widget, all UI sections, Objects wiring, Paths section

## Description

Refactor `src/aprilcam/cli/view_cli.py` to:

1. Introduce a `CollapsibleFrame(tk.Frame)` helper class with a header row (triangle
   toggle + title) and a toggleable content sub-frame.
2. Replace the four existing `LabelFrame` sections (Camera Status, Mobile Tags,
   Stationary Tags, Objects) with `CollapsibleFrame` instances.
3. Remove the standalone `btn_objects` button and `_toggle_objects()` function. Wire
   the Objects section's expand/collapse callbacks directly to `_detect_objects`.
4. Add a `_show_paths` mutable container and check it in the render loop before
   calling `display.draw_paths()`.
5. Add a new collapsible Paths section below Objects. It polls `_load_paths()` each
   frame and renders one row per path: 16×16 symbol canvas, 20×4 line-color swatch,
   path name/id label, and a delete button.

This ticket depends on Ticket 001, which adds `Path.name` and the `create_path`
`name` parameter.

## Acceptance Criteria

- [x] `CollapsibleFrame` class exists in `view_cli.py` (at module level, before `main()`).
- [x] `CollapsibleFrame` header shows ▼ when expanded, ▶ when collapsed.
- [x] All four existing sections start expanded (▼) on launch.
- [x] Each section collapses to header-only on click; re-expands on second click.
- [x] No standalone "Objects: On/Off" button exists.
- [x] Collapsing Objects calls `_detect_objects.clear()`; objects list in panel clears.
- [x] Expanding Objects calls `_detect_objects.set()`; lazily inits `ColorClassifier`
      if not yet created.
- [x] `_show_paths[0]` defaults to `True`; render loop calls `draw_paths()` only when
      `_show_paths[0]` is `True`.
- [x] Collapsing Paths sets `_show_paths[0] = False`; paths disappear from video.
- [x] Expanding Paths sets `_show_paths[0] = True`; paths reappear in video.
- [x] Paths section shows one row per path in `paths.json`.
- [x] Each row shows: symbol canvas (16×16), line swatch (20×4), name/id label,
      delete button.
- [x] Symbol canvas draws the first waypoint's symbol in its `symbol_color`.
- [x] Line swatch fills the first waypoint's `line_color` as a solid rectangle.
- [x] Row label shows `path["name"]` if set, otherwise `path["path_id"]`.
- [x] Delete button writes to `paths.json` directly then refreshes the Paths panel.
- [x] `uv run pytest tests/ -q` passes with no regressions.

## Implementation Plan

### Approach

Work top to bottom through `view_cli.py`:

1. Add `CollapsibleFrame` class before `main()`.
2. Replace `LabelFrame` section construction with `CollapsibleFrame` calls.
3. Remove `btn_objects` / `_toggle_objects`.
4. Add `_show_paths = [True]` near `_detect_objects`.
5. Add `_show_paths[0]` guard in `_process_frame_and_tags`.
6. Add Paths section at the bottom of the right-panel build block.
7. Add `_refresh_paths()` and `_delete_path()` helpers.
8. Call `_refresh_paths()` in `_poll()`.

### Files to Modify

**`src/aprilcam/cli/view_cli.py`**

#### Step 1: Add `CollapsibleFrame` class (before `main()`, after the `_fmt_stat_row` function)

```python
import tkinter.ttk as ttk  # add to imports at top of file

class CollapsibleFrame(tk.Frame):
    """A tk.Frame with a clickable header that hides/shows its content sub-frame.

    Parameters
    ----------
    parent:
        Parent widget.
    title:
        Section title displayed in the header row.
    bg:
        Background color for the frame and header.
    header_fg:
        Foreground color for the title text.
    on_expand:
        Callable invoked (no args) when the section is expanded.
    on_collapse:
        Callable invoked (no args) when the section is collapsed.
    """

    def __init__(
        self,
        parent,
        title: str,
        bg: str = "#1e1e1e",
        header_fg: str = "#aaaaaa",
        on_expand=None,
        on_collapse=None,
        **kwargs,
    ):
        super().__init__(parent, bg=bg, **kwargs)
        self._expanded = True
        self._on_expand = on_expand
        self._on_collapse = on_collapse

        # Header row
        self._header = tk.Frame(self, bg=bg, cursor="hand2")
        self._header.grid(row=0, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)

        self._toggle_lbl = tk.Label(
            self._header, text="▼", bg=bg, fg=header_fg,
            font=("Helvetica", 10, "bold"),
        )
        self._toggle_lbl.pack(side=tk.LEFT, padx=(4, 2))

        self._title_lbl = tk.Label(
            self._header, text=title, bg=bg, fg=header_fg,
            font=("Helvetica", 10, "bold"), anchor="w",
        )
        self._title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Content frame — callers pack/grid their widgets into this
        self.content = tk.Frame(self, bg=bg)
        self.content.grid(row=1, column=0, sticky="nsew")
        self.rowconfigure(1, weight=1)

        # Bind click on both header widgets
        for w in (self._header, self._toggle_lbl, self._title_lbl):
            w.bind("<Button-1>", lambda _e: self.toggle())

    def toggle(self):
        if self._expanded:
            self.content.grid_remove()
            self._toggle_lbl.config(text="▶")
            self._expanded = False
            if self._on_collapse:
                self._on_collapse()
        else:
            self.content.grid()
            self._toggle_lbl.config(text="▼")
            self._expanded = True
            if self._on_expand:
                self._on_expand()
```

#### Step 2: Replace `LabelFrame` sections in `main()`

Replace the four `LabelFrame` / button constructions (roughly lines 457–555) with
`CollapsibleFrame` instances. The content widgets (Text, Scrollbar, kv rows) move
into `cf.content` instead of directly into `right_frame`.

**Camera Status** — replace `status_frame = tk.LabelFrame(...)` with:

```python
cf_status = CollapsibleFrame(right_frame, title="Camera Status", bg=PANEL_BG, header_fg="#aaaaaa")
cf_status.pack(fill=tk.X, padx=8, pady=(8, 4))
status_frame = cf_status.content  # kv rows attach here (unchanged)
```

**Mobile Tags** — replace `mob_frame = tk.LabelFrame(...)` with:

```python
cf_mob = CollapsibleFrame(right_frame, title="Mobile Tags", bg=PANEL_BG, header_fg=MOB_FG)
cf_mob.pack(fill=tk.X, padx=8, pady=(4, 2))
mob_frame = cf_mob.content  # Text + Scrollbar attach here (unchanged)
```

**Stationary Tags** — replace `stat_outer = tk.LabelFrame(...)` with:

```python
cf_stat = CollapsibleFrame(right_frame, title="Stationary Tags", bg=PANEL_BG, header_fg=STAT_FG)
cf_stat.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))
stat_outer = cf_stat.content  # Text + Scrollbar attach here (unchanged)
```

**Objects** — replace `btn_objects` + `obj_outer = tk.LabelFrame(...)` with:

```python
def _lazy_start_objects():
    if _classifier_holder[0] is None:
        from aprilcam.vision.color_classifier import ColorClassifier
        _classifier_holder[0] = ColorClassifier(min_area=400, max_area=8000)
    _detect_objects.set()

cf_obj = CollapsibleFrame(
    right_frame, title="Objects", bg=PANEL_BG, header_fg=OBJ_FG,
    on_collapse=lambda: (_detect_objects.clear(), _obj_lock.__class__ and _latest_objects.__setitem__(0, [])),
    on_expand=_lazy_start_objects,
)
cf_obj.pack(fill=tk.X, padx=8, pady=(4, 2))
obj_outer = cf_obj.content  # Text + Scrollbar attach here (unchanged)
```

Note: the `on_collapse` lambda should call `_detect_objects.clear()` and then
`_latest_objects[0] = []` (guarded by `_obj_lock`). Write it as a named function
for clarity:

```python
def _on_obj_collapse():
    _detect_objects.clear()
    with _obj_lock:
        _latest_objects[0] = []

cf_obj = CollapsibleFrame(
    right_frame, title="Objects", bg=PANEL_BG, header_fg=OBJ_FG,
    on_collapse=_on_obj_collapse,
    on_expand=_lazy_start_objects,
)
```

Remove `btn_objects`, `_toggle_objects()`, and the `btn_objects.pack(...)` call.

#### Step 3: Add `_show_paths` state variable

Near `_detect_objects = threading.Event()` (line ~279), add:

```python
_show_paths: list = [True]  # mutable container; render loop checks [0]
```

#### Step 4: Guard `draw_paths()` in the render loop

In `_process_frame_and_tags` (around line 322), change:

```python
paths = _load_paths(_paths_file)
if paths:
    display.draw_paths(disp, paths, boundary, homography)
```

to:

```python
paths = _load_paths(_paths_file)
if paths and _show_paths[0]:
    display.draw_paths(disp, paths, boundary, homography)
```

#### Step 5: Add Paths section below Objects in the right panel

After the Objects `CollapsibleFrame`, add:

```python
PATH_FG = "#88aaff"

_paths_rows_frame: list = [None]  # holds the inner scrollable frame for rows

def _on_paths_collapse():
    _show_paths[0] = False

def _on_paths_expand():
    _show_paths[0] = True

cf_paths = CollapsibleFrame(
    right_frame, title="Paths", bg=PANEL_BG, header_fg=PATH_FG,
    on_collapse=_on_paths_collapse,
    on_expand=_on_paths_expand,
)
cf_paths.pack(fill=tk.X, padx=8, pady=(4, 8))

# Inner scrollable container for path rows
paths_canvas = tk.Canvas(cf_paths.content, bg=PANEL_BG, highlightthickness=0, height=120)
paths_vsb = tk.Scrollbar(cf_paths.content, orient=tk.VERTICAL, command=paths_canvas.yview)
paths_canvas.configure(yscrollcommand=paths_vsb.set)
paths_vsb.pack(side=tk.RIGHT, fill=tk.Y)
paths_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

paths_inner = tk.Frame(paths_canvas, bg=PANEL_BG)
paths_canvas_window = paths_canvas.create_window((0, 0), window=paths_inner, anchor="nw")

def _on_paths_inner_configure(event):
    paths_canvas.configure(scrollregion=paths_canvas.bbox("all"))
    paths_canvas.itemconfig(paths_canvas_window, width=paths_canvas.winfo_width())

paths_inner.bind("<Configure>", _on_paths_inner_configure)
paths_canvas.bind("<Configure>", lambda e: paths_canvas.itemconfig(
    paths_canvas_window, width=e.width
))
```

#### Step 6: Add `_refresh_paths()` and `_delete_path()` helpers

Add these as closures inside `main()`, after the Paths section setup:

```python
def _rgb_to_hex(rgb) -> str:
    """Convert an RGB list/tuple to a Tkinter hex color string."""
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    return f"#{r:02x}{g:02x}{b:02x}"

def _draw_symbol_on_canvas(c: tk.Canvas, symbol: str, color_hex: str) -> None:
    """Draw the given symbol on a 16x16 canvas."""
    c.delete("all")
    pad = 2
    s = 16 - 2 * pad
    cx, cy = 8, 8
    if symbol == "square":
        c.create_rectangle(pad, pad, pad + s, pad + s, outline=color_hex, width=1)
    elif symbol == "filled_square":
        c.create_rectangle(pad, pad, pad + s, pad + s, fill=color_hex, outline="")
    elif symbol == "circle":
        c.create_oval(pad, pad, pad + s, pad + s, outline=color_hex, width=1)
    elif symbol == "filled_circle":
        c.create_oval(pad, pad, pad + s, pad + s, fill=color_hex, outline="")
    elif symbol in ("triangle", "filled_triangle"):
        pts = [cx, pad, pad, pad + s, pad + s, pad + s]
        if symbol == "triangle":
            c.create_polygon(pts, outline=color_hex, fill="", width=1)
        else:
            c.create_polygon(pts, fill=color_hex, outline="")
    elif symbol == "x":
        c.create_line(pad, pad, pad + s, pad + s, fill=color_hex, width=1)
        c.create_line(pad + s, pad, pad, pad + s, fill=color_hex, width=1)
    # symbol == "none": draw nothing

def _delete_path(path_id: str) -> None:
    """Call the delete_path MCP tool for path_id, then refresh the Paths panel."""
    import asyncio
    from aprilcam.server.mcp_server import delete_path as _mcp_delete_path
    try:
        asyncio.run(_mcp_delete_path(path_id))
    except Exception:
        pass
    _refresh_paths()

def _refresh_paths() -> None:
    """Rebuild the Paths panel rows from _load_paths()."""
    # Destroy all existing row widgets
    for w in paths_inner.winfo_children():
        w.destroy()

    paths = _load_paths(_paths_file)
    if not paths:
        tk.Label(
            paths_inner, text="(no paths)", bg=PANEL_BG, fg="#666666",
            font=("Helvetica", 9),
        ).pack(anchor="w", padx=6, pady=2)
        return

    for path_id, path_dict in sorted(paths.items()):
        wps = path_dict.get("waypoints", [])
        first_wp = wps[0] if wps else {}
        sym = first_wp.get("symbol", "none")
        sym_color = first_wp.get("symbol_color", [180, 180, 180])
        line_color = first_wp.get("line_color", [180, 180, 180])
        label_text = path_dict.get("name") or path_id

        row = tk.Frame(paths_inner, bg=PANEL_BG)
        row.pack(fill=tk.X, padx=4, pady=1)

        # Symbol preview canvas
        sym_canvas = tk.Canvas(row, width=16, height=16, bg=PANEL_BG,
                               highlightthickness=0)
        sym_canvas.pack(side=tk.LEFT, padx=(0, 3))
        _draw_symbol_on_canvas(sym_canvas, sym, _rgb_to_hex(sym_color))

        # Line color swatch
        swatch = tk.Canvas(row, width=20, height=4, bg=PANEL_BG,
                           highlightthickness=0)
        swatch.pack(side=tk.LEFT, padx=(0, 6))
        swatch.create_rectangle(0, 0, 20, 4, fill=_rgb_to_hex(line_color), outline="")

        # Name/id label
        tk.Label(
            row, text=label_text, bg=PANEL_BG, fg=PATH_FG,
            font=("Helvetica", 9), anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Delete button — use unicode trash or plain X fallback
        del_btn = tk.Button(
            row,
            text="\U0001f5d1",   # 🗑
            font=("Helvetica", 9),
            bg=PANEL_BG, fg="#cc4444",
            activebackground="#333", activeforeground="#ff6666",
            relief=tk.FLAT, padx=2, pady=0,
            command=lambda pid=path_id: _delete_path(pid),
        )
        del_btn.pack(side=tk.RIGHT)
```

**Important note on `_delete_path`**: `view_cli.py` runs in a Tkinter event loop
and the MCP server's `delete_path` function is an async coroutine. Check whether the
existing codebase has a synchronous wrapper for MCP tool calls from the view. If the
MCP server exposes a `delete_path` function that internally writes to `paths.json`
directly (i.e., it's not a network call but a local registry operation), use the
path registry's `delete()` method and save to `paths.json` directly instead of
`asyncio.run`. Inspect `mcp_server.py`'s `delete_path` implementation to determine
the right call pattern. The simplest approach: import and call the `PathRegistry`
instance's `.delete(path_id)` method, then write `paths.json` manually using the
same write pattern as `create_path`. This avoids async complexity entirely.

The actual pattern to use depends on how `mcp_server.py` exposes path deletion to
the view layer — check at implementation time and choose the simplest approach.

#### Step 7: Call `_refresh_paths()` in `_poll()`

At the end of the `_poll()` function (after `_update_tag_panel`), add:

```python
_refresh_paths()
```

This rebuilds the path rows each poll cycle (same cadence as tag tables).

### Files to Create

None.

### Testing Plan

- Run full test suite: `uv run pytest tests/ -q`
- Manual verification per the issue's verification checklist (all 8 steps).
- Focus areas:
  1. All sections start expanded with ▼.
  2. Each section collapses and re-expands correctly.
  3. Objects collapse stops detection; expand resumes it (including lazy init on
     first expand).
  4. Paths section shows correct rows after `create_path` MCP calls.
  5. Delete button removes the path from `paths.json` and from the panel.
  6. Collapsing Paths removes video overlays; expanding restores them.

### Documentation Updates

None required beyond inline code comments. The `CollapsibleFrame` class docstring
covers the widget's public API.
