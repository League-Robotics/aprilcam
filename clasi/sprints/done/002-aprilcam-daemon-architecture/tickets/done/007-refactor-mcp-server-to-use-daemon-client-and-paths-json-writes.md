---
id: '007'
title: Refactor MCP server to use daemon client and paths.json writes
status: done
use-cases:
- SUC-002
- SUC-004
- SUC-006
- SUC-009
depends-on:
- '005'
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Refactor MCP server to use daemon client and paths.json writes

## Description

Refactor `src/aprilcam/server/mcp_server.py` to replace direct
`cv.VideoCapture` camera management with daemon RPC calls, and replace the
OS-pipe `send_command` side effect in path tools with atomic `paths.json`
file writes.

This ticket does NOT delete `liveview.py` — that is T008. This ticket removes
the imports and call sites that use `liveview.py` within `mcp_server.py`.

The MCP path tool API is unchanged (same inputs, same outputs). Only the
internal side effect of path mutations changes.

## Acceptance Criteria

- [x] `mcp_server.py` calls `ensure_running(config)` on startup; stores the
  `ControlClient` for camera RPCs.
- [x] `_handle_open_camera`: issues `open_camera(index)` RPC to daemon;
  stores returned `cam_name` and reads `info.json` to get `paths_file`.
  Does not call `cv.VideoCapture` directly.
- [x] Path tools (`create_path`, `delete_path`, `list_paths`, `clear_paths`):
  - `PathRegistry` logic and validation unchanged.
  - Side effect: after every mutation (`create`, `delete`, `clear_for`), read
    `paths_file` path from the camera's `info.json`, then atomically rewrite
    `paths.json` with `json.dumps(path_registry.list_for(pf_id))` (write to a
    temp file in the same directory, then `os.replace()`).
  - `liveview.LiveViewProcess.send_command()` calls removed entirely.
- [x] `_handle_start_live_view`: changed to `subprocess.Popen` of
  `aprilcam view --camera <cam_name>` (no pipe plumbing; the viewer subscribes
  to the daemon directly).
- [x] Sprint-001 pipe plumbing removed:
  - `set_initial_paths()` call removed from `_handle_start_live_view`.
  - `_cmd_write_fd` and related attributes removed from `LiveViewProcess`
    integration code.
  - `liveview` import removed from `mcp_server.py`.
- [x] `_handle_get_tags`: connects briefly to the camera's data socket
  (one frame) or reads the `info.json` snapshot to return current tags.
  (Note: existing `_handle_get_tags` reads from the ring buffer of a running
  detection loop; this remains correct — no change required since it does not
  call cv.VideoCapture directly.)
- [x] `uv run pytest` passes — existing path tool tests pass; no regressions.
- [x] New test: `test_path_tools_write_paths_json` — after `create_path`,
  assert that `paths.json` exists and contains valid JSON matching the
  registry state.

## Implementation Plan

### Approach

This is primarily a search-and-replace of call sites, not a structural
rewrite. Map each existing camera-open call to the equivalent RPC. Map
each `send_command` call to a file write. Remove dead code.

### Files to Modify

- `src/aprilcam/server/mcp_server.py` — see description above.

### Files to Create

None.

### Notes

- `info.json` is cached in the camera registry entry after `open_camera`;
  path tools read `paths_file` from that cached dict (no file stat per call).
- Atomic write: `tmpfile = paths_file.with_suffix(".tmp")`;
  `tmpfile.write_text(json.dumps(...))`;
  `os.replace(tmpfile, paths_file)`.
- `list_paths` does not mutate the registry; no file write needed for it.
- Open question 2 from the architecture (path-registry bootstrap on MCP server
  restart): this sprint's answer is "start empty; agents re-submit paths".
  Do not add bootstrap logic here unless the team-lead explicitly instructs.

### Testing Plan

- Run `uv run pytest` — all existing path tool tests must pass.
- New test `tests/test_mcp_path_tools.py::test_path_tools_write_paths_json`:
  mock `ensure_running` and `info.json`; call `create_path` handler; assert
  `paths.json` content matches registry state.

### Documentation Updates

Update module-level docstring in `mcp_server.py` to remove mention of
`LiveViewProcess` pipe IPC.
