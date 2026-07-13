---
id: '003'
title: Live-view command pipe IPC
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-004
- SUC-005
- SUC-006
depends-on:
- '001'
- '002'
github-issue: ''
issue: agent-drawn-paths-on-the-aprilcam-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# T003: Live-view command pipe IPC

## Description

Add a third OS pipe (command pipe, parent→child) to the live-view subprocess
system. The parent `LiveViewProcess` writes line-delimited JSON commands
(`add`, `remove`, `clear`); the child drains and applies them each frame.

This ticket also wires the T002 MCP tool handlers to `send_command()` so that
`create_path`, `delete_path`, and `clear_paths` forward mutations to any
running live view. It handles initial-state push so paths created before the
live view starts appear on the first frame.

## Acceptance Criteria

- [x] `LiveViewProcess.__init__` initializes `_cmd_write_fd: Optional[int] = None`
      and `_initial_paths: list[dict] = []`.
- [x] `LiveViewProcess.set_initial_paths(paths: list[dict])` stores the list
      (called before `start()`).
- [x] `LiveViewProcess.send_command(msg: dict)` writes `json.dumps(msg) + "\n"`
      to `_cmd_write_fd`; is a no-op if fd is None or the view is not running.
- [x] `LiveViewProcess.start()` creates `cmd_r, cmd_w = os.pipe()`, stores
      `_cmd_write_fd = cmd_w`, passes `cmd_r` and
      `json.dumps(self._initial_paths)` as additional arguments to `_child_main`,
      and closes `cmd_r` in the parent after `proc.start()`.
- [x] `LiveViewProcess.stop()` closes `_cmd_write_fd` and sets it to None.
- [x] `_child_main` signature gains `cmd_fd: int` and
      `initial_paths_json: str = "[]"` parameters.
- [x] Child opens `cmd_in = os.fdopen(cmd_fd, "r", buffering=1)`.
- [x] Child seeds `paths: dict[str, dict] = {}` from
      `json.loads(initial_paths_json)` keyed by `path_id`.
- [x] `_drain_commands()` replaces `_should_stop()` inside `_child_main`:
      - Uses `select.select([stop_in, cmd_in], [], [], 0)` (non-blocking).
      - If `stop_in` is readable: returns `True`.
      - For each readable line from `cmd_in`:
        - `op == "add"`: `paths[msg["path"]["path_id"]] = msg["path"]`.
        - `op == "remove"`: `paths.pop(msg["path_id"], None)`.
        - `op == "clear"`: `paths.clear()`.
      - Returns `False` after draining.
- [x] The in-loop call changes from `if _should_stop():` to
      `if _drain_commands():`.
- [x] In the `finally` block: `cmd_in.close()` is called.
- [x] `_handle_start_live_view` in `mcp_server.py`: before `proc.start()`,
      calls `proc.set_initial_paths([p.to_dict() for p in path_registry.list_for(pf_id)])`.
      (Remove the `# TODO T003` placeholder from T002.)
- [x] `_handle_create_path` in `mcp_server.py`: after creating in registry,
      calls `lv.send_command({"op": "add", "path": path.to_dict()})` if
      `lv = _live_view_for_playfield(playfield_id)` is not None.
- [x] `_handle_delete_path` in `mcp_server.py`: after deleting from registry,
      sends `{"op": "remove", "path_id": path_id}` if live view is running.
- [x] `_handle_clear_paths` in `mcp_server.py`: after clearing registry,
      sends `{"op": "clear"}` if live view is running.
- [x] Live view does not crash when started with no paths (empty initial list).

## Implementation Plan

### Approach

The existing two-pipe pattern in `liveview.py` is the template. The stop pipe
uses `os.write(stop_write_fd, b"stop\n")` on the parent side and
`select.select([stop_in], ...)` on the child side. The command pipe follows
the identical pattern but carries JSON lines. The child reads lines in a
non-blocking drain loop (same non-blocking `select` timeout=0).

Key implementation note: `select.select` in the child must select on
**both** `stop_in` and `cmd_in` simultaneously in a single call. Do not
loop on `cmd_in` separately — that would block the stop signal.

### Files to Modify

**`src/aprilcam/ui/liveview.py`**

Changes at 6 locations (reference line numbers from current code):

1. `_child_main` signature (line 30): add `cmd_fd: int` and
   `initial_paths_json: str = "[]"` after `stop_fd`.

2. After `stop_in = os.fdopen(stop_fd, "r")` (~line 55): open
   `cmd_in = os.fdopen(cmd_fd, "r", buffering=1)` and seed `paths` dict.

3. Replace the nested `_should_stop()` function (~lines 101-105) with
   `_drain_commands()` that selects on both pipes and mutates `paths`.

4. Change the loop call at ~line 121 from `_should_stop()` to
   `_drain_commands()`.

5. After `display.draw_overlays(...)` (~line 167): add
   `display.draw_paths(disp, paths, cam.playfield, cam.homography)`.
   (The actual draw_paths method body is implemented in T004 — this call
   site must exist now; draw_paths may be a no-op stub until T004.)

6. `finally` block (after line 222): add `cmd_in.close()`.

7. `LiveViewProcess.__init__` (~lines 257-262): add `_cmd_write_fd = None`
   and `_initial_paths: list = []`.

8. New methods `set_initial_paths` and `send_command` on `LiveViewProcess`.

9. `start()` (~line 283): create cmd pipe, pass to child, close read end.

10. `stop()` (~line 318): close `_cmd_write_fd`.

**`src/aprilcam/server/mcp_server.py`**

- Remove `# TODO T003` placeholder.
- Add initial-state push in `_handle_start_live_view`.
- Wire `send_command()` calls in `_handle_create_path`, `_handle_delete_path`,
  `_handle_clear_paths`.

### Files to Create

None (no new files).

### Testing Plan

The IPC layer requires a child process and OS pipes — not easily unit-testable
in isolation. Testing is via the end-to-end manual verification in T005.

However, a smoke test can verify the pipe wiring without a camera:
- `test_live_view_process_set_initial_paths` — instantiate `LiveViewProcess`,
  call `set_initial_paths([...])`, verify `_initial_paths` is set (no process
  spawned).
- `test_send_command_noop_when_not_running` — call `send_command(...)` before
  `start()` — must not raise.

- **Existing tests to run**: `uv run pytest` (full suite — especially any
  tests touching `LiveViewProcess`).
- **Verification command**: `uv run pytest -v`

### Documentation Updates

Update the module docstring in `liveview.py` to mention the three-pipe
architecture (data: child→parent, stop: parent→child, command: parent→child).
