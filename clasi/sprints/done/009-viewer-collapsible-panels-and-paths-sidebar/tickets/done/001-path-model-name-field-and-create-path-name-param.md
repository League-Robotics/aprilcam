---
id: '001'
title: Path model name field and create_path name param
status: done
use-cases: [SUC-003]
depends-on: []
github-issue: ''
issue: aprilcam-viewer-collapsible-panels-and-paths-sidebar-section.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Path model name field and create_path name param

## Description

Add an optional `name: str = ""` field to the `Path` dataclass in
`src/aprilcam/server/paths.py`. Update `to_dict()` to include `name`, and update
`PathRegistry.create()` to accept and pass through `name`. Add a corresponding `name`
parameter to the `create_path` MCP tool in `src/aprilcam/server/mcp_server.py`.

This is purely a backend change — no UI code is touched in this ticket. Ticket 002
consumes the new field to render the Paths sidebar panel.

## Acceptance Criteria

- [ ] `Path` dataclass has `name: str = ""` as a fourth field (after `waypoints`).
- [ ] `Path.to_dict()` includes `"name": self.name` in its output dict.
- [ ] `PathRegistry.create()` accepts `name: str = ""` and stores it on the `Path`.
- [ ] `create_path` MCP tool accepts `name: str = ""` as an optional parameter.
- [ ] Calling `create_path` with `name="Robot path"` stores that name in `paths.json`.
- [ ] Calling `create_path` without `name` stores `""` and is backward compatible.
- [ ] Existing `paths.json` files without a `name` key are read correctly by
      `_load_paths()` in `view_cli.py` (it uses `item.get("name", "")` — no code
      change needed in `_load_paths()` itself, but verify the dict pass-through).
- [ ] `uv run pytest tests/ -q` passes with no regressions.

## Implementation Plan

### Approach

All changes are additive. `name` defaults to `""` everywhere, so no existing call
sites require modification.

### Files to Modify

**`src/aprilcam/server/paths.py`**

1. Add `name: str = ""` to the `Path` dataclass after the `waypoints` field:

   ```python
   @dataclass
   class Path:
       path_id: str
       playfield_id: str
       waypoints: List[Waypoint]
       name: str = ""
   ```

2. Update `to_dict()` to include `name` (add it alongside `path_id` and
   `playfield_id`):

   ```python
   def to_dict(self) -> dict:
       return {
           "path_id": self.path_id,
           "playfield_id": self.playfield_id,
           "name": self.name,
           "waypoints": [
               {
                   "x": wp.x,
                   "y": wp.y,
                   "size_cm": wp.size_cm,
                   "symbol": wp.symbol,
                   "symbol_color": list(wp.symbol_color),
                   "line_color": list(wp.line_color),
               }
               for wp in self.waypoints
           ],
       }
   ```

3. Update `PathRegistry.create()` to accept and forward `name`:

   ```python
   def create(
       self, playfield_id: str, waypoints: List[Waypoint], name: str = ""
   ) -> Path:
       with self._lock:
           path_id = f"path_{self._counter:03d}"
           self._counter += 1
           path = Path(
               path_id=path_id,
               playfield_id=playfield_id,
               waypoints=list(waypoints),
               name=name,
           )
           self._paths[path_id] = path
           return path
   ```

4. Add `name` to the `Path` class docstring:

   ```
   name:
       Optional human-readable display label. Defaults to ``""``; the viewer
       falls back to ``path_id`` when blank.
   ```

**`src/aprilcam/server/mcp_server.py`**

Locate the `create_path` async function. Add `name: str = ""` as the third parameter
and pass it to `path_registry.create()`:

```python
async def create_path(
    playfield_id: str,
    waypoints_json: str,
    name: str = "",
) -> list[TextContent]:
    # ... existing validation ...
    path = path_registry.create(playfield_id, waypoints, name=name)
    # ... existing response building ...
```

The MCP tool docstring should note the new parameter:

```
name: Optional display label for the path (shown in the viewer panel).
      Defaults to "" (viewer falls back to path_id).
```

### Files to Create

None.

### Testing Plan

- Run full test suite: `uv run pytest tests/ -q`
- Confirm no existing test fails (`name` defaults to `""` so all existing fixtures
  that omit `name` continue to work).
- If there are existing tests for `PathRegistry.create()` or `create_path`, verify
  they pass unchanged.
- Add a test or manual check that:
  1. `PathRegistry.create("pf_0", waypoints, name="Test")` returns a `Path` with
     `name == "Test"`.
  2. `Path(..., name="Test").to_dict()["name"] == "Test"`.
  3. `Path(...).to_dict()["name"] == ""` (default).

### Documentation Updates

Update `Path` class docstring to mention `name` field as described above.
