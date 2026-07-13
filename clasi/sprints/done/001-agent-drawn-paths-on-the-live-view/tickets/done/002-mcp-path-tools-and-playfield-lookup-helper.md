---
id: '002'
title: MCP path tools and playfield lookup helper
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: agent-drawn-paths-on-the-aprilcam-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# T002: MCP path tools and playfield lookup helper

## Description

Add four MCP tools to `src/aprilcam/server/mcp_server.py`:
`create_path`, `delete_path`, `list_paths`, `clear_paths`. Add the
`_live_view_for_playfield(playfield_id)` helper. Instantiate
`path_registry = PathRegistry()` next to the existing `playfield_registry`.

At this stage the tools perform full input validation and registry operations
but do NOT yet forward commands to the live view — that wiring comes in T003.
All four tools are unit-testable without a running camera or child process.

## Acceptance Criteria

- [x] `path_registry = PathRegistry()` is instantiated in `mcp_server.py` at
      module level alongside `playfield_registry`.
- [x] `from aprilcam.server import paths` (or equivalent) is imported.
- [x] `_live_view_for_playfield(playfield_id) -> Optional[LiveViewProcess]`
      looks up `PlayfieldEntry.camera_id` from `playfield_registry`, constructs
      `view_id = f"live_{camera_id}"`, returns the `LiveViewProcess` if present
      in the live-views dict, else `None`.
- [x] `create_path` validates in order:
      1. `playfield_id` in `playfield_registry` → error if unknown.
      2. `json.loads(waypoints_json)` succeeds and result is a non-empty list →
         error if parse fails or list is empty.
      3. Each waypoint dict has all required keys: `x`, `y`, `size_cm`,
         `symbol`, `symbol_color`, `line_color` → error if missing.
      4. `x`, `y`, `size_cm` are finite floats; `size_cm > 0` → specific error
         message `"size_cm must be positive"` for non-positive values.
      5. `symbol` is one of the 8 valid values (use `paths.VALID_SYMBOLS`) →
         error `"Invalid symbol '<value>'"`.
      6. `symbol_color` and `line_color` are lists/tuples of 3 ints each in
         `[0, 255]` → error if wrong length or out of range.
      First failure returns immediately with `{"error": "<message>"}`.
- [x] On validation success, `create_path` calls `path_registry.create(...)`,
      calls `_live_view_for_playfield(playfield_id)` (result may be None — no
      crash), and returns `{"path_id": "path_NNN"}`.
- [x] `delete_path(path_id)` looks up in registry; if found deletes and returns
      `{"deleted": true, "path_id": "..."}`. If not found returns
      `{"error": "Unknown path_id '<id>'"}`.
- [x] `list_paths(playfield_id)` validates playfield exists; returns
      `{"playfield_id": "...", "paths": [path.to_dict(), ...]}`.
- [x] `clear_paths(playfield_id)` validates playfield exists; clears registry;
      returns `{"cleared": ["path_000", ...]}`.
- [x] All four tools are registered with `@server.tool()` matching the shape
      of existing tools (return `list[TextContent]` with `json.dumps(result)`).
- [x] Unit tests pass without live camera hardware.

## Implementation Plan

### Approach

Follow the existing handler pattern in `mcp_server.py`:
- A private `_handle_*` function performs the work and returns a dict.
- A `@server.tool()` wrapper calls the handler and wraps the result in
  `[TextContent(type="text", text=json.dumps(result))]`.

For `_live_view_for_playfield`: use the `playfield_registry` to get
`PlayfieldEntry.camera_id`, form `view_id = f"live_{camera_id}"`, look
up the `_live_views` dict (or equivalent in mcp_server.py) to find the
`LiveViewProcess`. Return `None` if any step fails (playfield unknown,
no live view running).

### Files to Modify

**`src/aprilcam/server/mcp_server.py`** — four insertion points:

1. Near existing imports (~line 34): add `from aprilcam.server import paths`
   (or `from aprilcam.server.paths import PathRegistry, VALID_SYMBOLS, ...`).

2. Near `playfield_registry` instantiation (~line 154): add
   `path_registry = PathRegistry()`.

3. After `stop_live_view` tool block (~line 2647): add
   `_live_view_for_playfield()` helper, then the four `_handle_*` functions
   and four `@server.tool()` wrappers.

4. Inside `_handle_start_live_view`, before `proc.start()` (~line 1242):
   leave a `# TODO T003: set_initial_paths` comment placeholder (do not
   implement — T003 owns this).

### Files to Create

**`tests/test_mcp_path_tools.py`** — unit tests using mocked registries:
- `test_create_path_unknown_playfield` — playfield_registry mock returns None; expect error.
- `test_create_path_invalid_json` — `waypoints_json="not json"` → error.
- `test_create_path_empty_list` — `waypoints_json="[]"` → error.
- `test_create_path_invalid_symbol` → `{"error": "Invalid symbol 'hexagon'"}`.
- `test_create_path_negative_size_cm` → `{"error": "size_cm must be positive"}`.
- `test_create_path_color_out_of_range` → error.
- `test_create_path_success` — valid input → `{"path_id": "path_000"}`.
- `test_delete_path_known` → `{"deleted": true, "path_id": "path_000"}`.
- `test_delete_path_unknown` → `{"error": "Unknown path_id 'path_999'"}`.
- `test_list_paths_empty` → `{"playfield_id": "pf_cam_0", "paths": []}`.
- `test_list_paths_with_entries` → paths list populated.
- `test_clear_paths` → `{"cleared": [...]}`.

### Testing Plan

- **Existing tests to run**: `uv run pytest` (full suite — ensure no regressions
  to existing MCP tools).
- **New tests**: `tests/test_mcp_path_tools.py` (see above).
- **Verification command**: `uv run pytest tests/test_paths.py tests/test_mcp_path_tools.py -v`

### Documentation Updates

None. The tool signatures and error messages are self-documenting via the MCP
tool description strings (add brief descriptions to each `@server.tool()`
decorator matching the style of existing tools).
