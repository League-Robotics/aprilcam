---
id: '005'
title: HOST-N in MCP tools via ConnectionManager, and docs
status: done
use-cases:
- SUC-003
depends-on:
- '003'
github-issue: ''
issue: host-qualified-camera-names-donald-4.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# HOST-N in MCP tools via ConnectionManager, and docs

## Description

Every MCP tool with a `camera` parameter (about 16, in `mcp/tools/`
`perception.py`, `configuration.py`, `intrinsics.py`, and others) accepts
`HOST-N`, routing to the right daemon through the existing per-call `daemon`
mechanism.

## Implementation Plan

- `mcp/connection.py`: add `ConnectionManager.call_camera(camera, fn, *,
  host=None)` that runs `client.addressing` with the effective target
  (per-call `host`, else `default_host`), then resolves/caches the daemon for
  the resolved host (cache key = resolved host, so other entries are
  untouched) and calls `fn(daemon, resolved_camera)`. Keep the evict-on-
  `ConnectionFailed` behaviour. Reuse the manager's lazily built Discovery;
  still no RPC at construction.
- Migrate each tool that takes `camera` from `connection.call(lambda d:
  d.X(camera), host=daemon)` to `call_camera`. `grep -n "camera: " src/aprilcam/mcp/tools/*.py`
  to find them all; tools whose camera is optional pass `None` straight through.
- Map `UnknownHost` / `ConflictingTarget` in `mcp/errors.py` to tool errors.
- Update tool docstrings / `get_guide` text where they describe the `camera`
  argument to mention `HOST-N`; update `src/aprilcam/AGENT_GUIDE.md` likewise.
- `src/aprilcam/mcp/DESIGN.md` and `src/aprilcam/client/DESIGN.md`: fold in
  the sprint design overlay edits (HOST-N, `DaemonInfo.name`, addressing).

## Acceptance Criteria

- [x] `get_tags(camera="donald-4")` calls Donald's daemon with camera `4`, no `daemon` argument
- [x] Every tool with a `camera` parameter uses `call_camera` (a test enumerates the registered tools)
- [x] Per-call `daemon` conflicting with HOST-N yields a tool error
- [x] Slug / number arguments unchanged; cache eviction behaviour unchanged
- [x] Guide and design docs updated

## Testing

- **Existing tests to run**: `tests/aprilcam/mcp`
- **New tests to write**: `ConnectionManager.call_camera` tests with a fake discovery factory (routing, conflict, cache keyed by resolved host, eviction); tool-level tests for a perception and a configuration tool; tool-enumeration test
- **Verification command**: `uv run pytest tests/aprilcam/mcp`
