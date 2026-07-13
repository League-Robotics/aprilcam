---
status: in-progress
sprint: 009
tickets:
- 009-001
---

# AprilCam viewer: collapsible panels and paths sidebar section

## Context

The right-side info panel in `aprilcam view` (`src/aprilcam/cli/view_cli.py`) has four
fixed Tkinter frames — Camera Status, Mobile Tags, Stationary Tags, Objects — and no
paths panel. Users want to collapse sections they don't need, have the Objects section
control detection state directly (open = on, closed = off), and see a named path list
with a visual preview of each path's first symbol and line color.

## Changes

### 1. Collapsible section widget — `view_cli.py`

Replace the four plain `LabelFrame` sections with a reusable collapsible widget. Each
section gets a header row with:
- A triangle toggle (▶ collapsed / ▼ expanded) implemented as a `ttk.Label` or
  small `tk.Button` that toggles `grid_remove()` / `grid()` on the content frame
- The section title

Implementation: a small `CollapsibleFrame(tk.Frame)` helper class — header with
triangle label on the left, content frame hidden/shown on toggle. Start all sections
expanded. Fits naturally into the existing `tk.Frame` / `pack` layout in the right
panel.

### 2. Objects section: open/close drives detection state — `view_cli.py`

When the Objects section is **collapsed**, call `_detect_objects.clear()` (stop
detection). When **expanded**, call `_detect_objects.set()` (start detection, lazily
initializing `ColorClassifier` as now). Remove the separate "Objects: On/Off" toggle
button — the triangle header IS the toggle.

### 3. Add `name` field to `Path` model — `src/aprilcam/server/paths.py`

```python
@dataclass
class Path:
    path_id: str
    playfield_id: str
    waypoints: List[Waypoint]
    name: str = ""              # optional display name; defaults to path_id if blank
```

Update `to_dict()` to include `name`. Update `from_dict()` / deserialization to read
it (default `""`). No proto change needed — name is UI metadata, not streamed.

### 4. Add `name` parameter to `create_path` MCP tool — `src/aprilcam/server/mcp_server.py`

```python
async def create_path(
    playfield_id: str,
    waypoints_json: str,
    name: str = "",
) -> list[TextContent]:
```

Store the name on the `Path` object; write it to `paths.json` via `to_dict()`.

### 5. New Paths section in the right panel — `view_cli.py`

Add a collapsible **Paths** section below Objects. Collapsing the section **hides all
paths from the video display** (same pattern as Objects driving `_detect_objects`):
maintain a `_show_paths` boolean that the render loop checks before calling
`draw_paths()`; toggle it in the Paths `CollapsibleFrame` on-toggle callback.

When expanded, it shows one row per path:

```
▸ [symbol preview] [line swatch]  path name (or path_id)   [🗑]
```

- **Symbol preview**: a tiny `tk.Canvas` (16×16 px) drawing the first waypoint's
  symbol in its `symbol_color` — uses the same 8 symbol types as `draw_paths()`.
- **Line swatch**: a 20×4 px filled rectangle in the first waypoint's `line_color`.
- **Label**: `path.name` if set, otherwise `path.path_id`.
- **Delete button**: a small `tk.Button` with a 🗑 label (or "✕" as a plain-text
  fallback if the font doesn't support it) at the right of the row. Clicking it calls
  the existing `delete_path` MCP tool for that `path_id`, then immediately re-reads
  `paths.json` to refresh the list.

The paths list refreshes on the same polling interval as the tag tables (currently
every frame). Reads from `_paths_file` via the existing `_load_paths()` helper
(`view_cli.py:110-124`).

## Critical files

- `src/aprilcam/cli/view_cli.py` — collapsible widget, Objects wiring, Paths section (show/hide + delete)
- `src/aprilcam/server/paths.py` — add `name` field to `Path` and `Waypoint` ser/deser
- `src/aprilcam/server/mcp_server.py` — add `name` param to `create_path`

## Verification

1. `aprilcam view 4` — confirm all sections start expanded with ▼ triangles.
2. Click each triangle — section collapses to header only; click again to re-expand.
3. Collapse Objects — detection stops (objects list clears). Expand — detection resumes.
4. Collapse Paths — paths disappear from the video display. Expand — paths reappear.
5. Call `create_path` with `name="Robot path"` — Paths section shows the row with
   correct symbol preview, line color swatch, name, and 🗑 button.
6. Call `create_path` without `name` — row shows `path_NNN` as fallback label.
7. Click 🗑 on a path row — path is deleted from `paths.json` and row disappears.
8. `uv run pytest tests/ -q` — all tests pass.
