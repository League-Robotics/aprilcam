---
id: 009
title: Add set_live_overlay and clear_live_overlay MCP tools
status: done
use-cases:
  - SUC-003
depends-on:
  - '006'
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add set_live_overlay and clear_live_overlay MCP tools

## Description

Add two new MCP tools to `src/aprilcam/server/mcp_server.py`:

- `set_live_overlay(camera_id, elements_json, ttl=1.0)` — parses a JSON list of
  element dicts and calls `DaemonControl.publish_overlay()`.
- `clear_live_overlay(camera_id)` — immediately removes the overlay by publishing
  an empty `OverlayFrame` with `ttl=0`.

These are thin wrappers; all logic lives in `DaemonControl.publish_overlay()`.

## Acceptance Criteria

- [x] `set_live_overlay` tool registered in the MCP server with parameters:
      `camera_id` (str), `elements_json` (str, JSON array), `ttl` (float, default 1.0).
- [x] `set_live_overlay` parses `elements_json`; on JSON parse error returns a
      descriptive error string, not an exception.
- [x] `set_live_overlay` calls `daemon_client.publish_overlay(camera_id, elements, ttl)`.
- [x] `clear_live_overlay` tool registered with parameter: `camera_id` (str).
- [x] `clear_live_overlay` calls `publish_overlay(camera_id, [], ttl=0)`.
- [x] Both tools include docstrings noting that robot code can also call
      `DaemonControl.publish_overlay()` directly.
- [x] Import smoke: `uv run python -c "from aprilcam.server import mcp_server; print('ok')"`.
- [x] `uv run pytest tests/` passes.

## Implementation Plan

### Approach

1. Read `src/aprilcam/server/mcp_server.py` to understand tool registration
   pattern (likely `@mcp.tool()` decorator or FastMCP equivalent).
2. Add `set_live_overlay`:
   ```python
   @mcp.tool()
   def set_live_overlay(camera_id: str, elements_json: str, ttl: float = 1.0) -> str:
       """Push graphical overlay elements to the live view.

       elements_json: JSON array of element dicts, each with keys:
         type (str): "arc", "arrow", "point", or "polyline"
         params (list[float]): type-specific coordinates in world cm
         color (list[int]): [R, G, B] 0-255
         thickness (int): line thickness in pixels

       Any process with DaemonControl access can also call
       DaemonControl.publish_overlay() directly.
       """
       import json
       try:
           elements = json.loads(elements_json)
       except json.JSONDecodeError as e:
           return f"Error: invalid JSON: {e}"
       ok = daemon_client.publish_overlay(camera_id, elements, ttl)
       return "ok" if ok else "error: overlay not published"
   ```
3. Add `clear_live_overlay`:
   ```python
   @mcp.tool()
   def clear_live_overlay(camera_id: str) -> str:
       """Immediately remove the live overlay from the view."""
       ok = daemon_client.publish_overlay(camera_id, [], ttl=0)
       return "ok" if ok else "error: could not clear overlay"
   ```

### Files to Modify

- `src/aprilcam/server/mcp_server.py`

### Testing Plan

- Import smoke.
- `uv run pytest tests/`
- Manual: call `set_live_overlay` via MCP client with a test arc; verify in live
  view (requires tickets 007, 008 to be complete).

### Documentation Updates

Docstrings on both tools serve as documentation.
