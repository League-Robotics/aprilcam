---
id: '006'
title: 'Optional: set_camera_playfield MCP tool'
status: done
use-cases:
- SUC-001
depends-on:
- '001'
- '002'
- '005'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Optional: set_camera_playfield MCP tool

## Description

**This ticket is optional and may be dropped if sprint scope tightens.**

Adds a new MCP tool `set_camera_playfield(camera_id, playfield)` that lets an
agent write `config.json` for an open camera without hand-editing files. This
resolves the "unconfigured camera" state that `calibrate_playfield` and
`open_camera` check for.

After `set_camera_playfield`, the agent can call `calibrate_playfield` or
`open_camera` (with rehydration if calibration exists) without further manual
setup.

### Tool signature

```python
@server.tool()
async def set_camera_playfield(
    camera_id: str,
    playfield: str,
) -> list[TextContent]:
    """Link a camera to a named playfield definition.

    Writes data/aprilcam/cameras/<slug>/config.json with {"playfield": "<name>"}.
    The named playfield must exist in the registry.

    This must be called before calibrate_playfield when the camera has no
    existing config.json, or to switch a camera to a different playfield.

    Does not trigger recalibration. The existing calibration (if any) becomes
    stale and will be flagged on the next open_camera.

    Args:
        camera_id: The camera_id from open_camera.
        playfield: The playfield name (slug) to link. Must be a name returned
            by list_playfields (not yet implemented) or known to the operator.

    Returns:
        {"camera_id": ..., "playfield": ..., "config_path": ...} on success.
        {"error": ...} on failure.
    """
```

### Implementation

1. Validate `camera_id` is in `registry._cameras`.
2. Validate `playfield` name exists in `playfield_def_registry` (raises
   friendly error listing available names if not).
3. Get `camera_dir` from `_cam_info[camera_id]["camera_dir"]`.
4. Call `save_camera_config(camera_dir, {"playfield": playfield})`.
5. Return `{"camera_id": camera_id, "playfield": playfield,
   "config_path": str(camera_dir / "config.json")}`.

## Acceptance Criteria

- [x] `set_camera_playfield(camera_id, "main-playfield")` writes a valid
      `config.json` and returns the config path.
- [x] `set_camera_playfield(camera_id, "nonexistent")` returns
      `{"error": "Playfield 'nonexistent' not found. Available: [...]"}`.
- [x] `set_camera_playfield` on an unknown `camera_id` returns an error.
- [x] After `set_camera_playfield`, `load_camera_config(camera_dir)` returns
      `{"playfield": "main-playfield"}`.
- [x] `uv run pytest` passes.

## Implementation Plan

### Files to modify

- `src/aprilcam/server/mcp_server.py`
  - Add `set_camera_playfield` as a `@server.tool()` function near the other
    camera management tools.

### Testing plan

Add 2-3 unit tests in `tests/test_mcp_path_tools.py` or a new small test file:
- `test_set_camera_playfield_writes_config(monkeypatch, tmp_path)`
- `test_set_camera_playfield_unknown_camera(monkeypatch)`
- `test_set_camera_playfield_unknown_playfield(monkeypatch, tmp_path)`

### Documentation updates

Update `ROBOT_API_GUIDE.md` with `set_camera_playfield` if this ticket is
executed (handled in ticket 007).
