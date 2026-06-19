---
id: '002'
title: Delete MCP image-processing tools
status: done
use-cases:
- SUC-001
- SUC-006
depends-on: []
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Delete MCP image-processing tools

## Description

Remove the seven image-processing MCP tools from `mcp_server.py` — their tool
definitions, `_handle_*` functions, imports, and any helper machinery that exists
only to serve them. Delete their tests. This is a stakeholder-approved breaking
change: consumers that need pixel processing call `get_frame`/`capture_frame` and
run their own CV outside the MCP.

The seven tools to delete: `detect_lines`, `detect_circles`, `detect_contours`,
`detect_motion`, `detect_qr_codes`, `crop_region`, `apply_transform`.

## Acceptance Criteria

- [x] `mcp_server.py` contains no `@server.tool()` definition for any of:
  `detect_lines`, `detect_circles`, `detect_contours`, `detect_motion`,
  `detect_qr_codes`, `crop_region`, `apply_transform`.
- [x] `mcp_server.py` imports no symbols from `aprilcam.vision.image_processing`
  at module level or via inline import in any remaining function.
- [x] `mcp_server.py` contains no `_FrameEntry` class and no `_frame_registry`
  dict **unless** `create_composite` / `get_composite_frame` genuinely uses them
  (implementer must check; if so, leave the registry but remove only the 7 tools).
- [x] All tests that test the deleted tools are removed or replaced with
  "tool is not present" assertions.
- [x] `uv run pytest` green after deletions.

## Implementation Plan

### What to delete in `mcp_server.py`

**Tool definitions and handlers** (async `@server.tool()` functions, grep for each name):
- `async def detect_lines(...)` and its `@server.tool()` decorator
- `async def detect_circles(...)` and its `@server.tool()` decorator
- `async def detect_contours(...)` and its `@server.tool()` decorator
- `async def detect_motion(...)` and its `@server.tool()` decorator
- `async def detect_qr_codes(...)` and its `@server.tool()` decorator
- `async def crop_region(...)` and its `@server.tool()` decorator
- `async def apply_transform(...)` and its `@server.tool()` decorator

**Top-level imports** (lines ~60-63):
```python
from aprilcam.vision.image_processing import (
    process_detect_circles,
    process_detect_contours,
    process_detect_lines,
    process_detect_qr_codes,
)
```

**`_get_composite_frame` / `create_composite` registry check**: before deleting
`_FrameEntry` and `_frame_registry`, grep `mcp_server.py` for all references.
If `create_composite` and `get_composite_frame` reference these, **keep** the
registry data structures but still delete the seven tool functions. If the registry
is exclusively used by the seven tools, delete it too.

**Operation name constants** (around line 3827): remove
`"detect_lines"`, `"detect_circles"`, `"detect_contours"`, `"detect_qr"` from
any list/dict that enumerates valid operations (used in `_apply_operations` or
similar).

**`_apply_operations` / `get_composite_frame` operation dispatch** (around lines
3936-3942): remove the `elif op == "detect_lines"`, `elif op == "detect_circles"`,
`elif op == "detect_contours"` branches.

**`_draw_object_overlay`** (~line 1520) and `resolve_source` (~line 429): leave
these — they may be used by `get_frame` rendering path. Verify before deleting.

### Test files to delete or update

Search `tests/` for files referencing the deleted tools:
```bash
grep -rl "detect_lines\|detect_circles\|detect_contours\|detect_motion\|detect_qr\|crop_region\|apply_transform" tests/
```
Delete those test files (or remove the relevant test functions if the file also
tests other things that are kept).

### Files to modify/delete

- `src/aprilcam/server/mcp_server.py` — remove tool functions, imports, registry (if applicable)
- Any test files found by the grep above — delete or trim
- `src/aprilcam/server/web_server.py` — remove endpoints for the 7 deleted tools from
  `_TOOL_SPECS` (ticket 004 will fully rewrite this file, but if 002 runs first, remove
  the entries to avoid test failures)

### Testing

- Run `uv run pytest` after deletions; fix any cascade failures from removed imports.
- Confirm no `ModuleNotFoundError` from any remaining import of `image_processing`.
- Confirm `get_frame`, `capture_frame`, `get_tags`, `get_objects`, and `where` still pass.
