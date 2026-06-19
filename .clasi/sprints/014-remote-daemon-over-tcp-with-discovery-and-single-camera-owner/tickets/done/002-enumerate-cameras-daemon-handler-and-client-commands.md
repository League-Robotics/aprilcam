---
id: 014-002
title: EnumerateCameras daemon handler and convert CLI/MCP to use it
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- 014-001
---

# 014-002: EnumerateCameras daemon handler and convert CLI/MCP to use it

## Description

Implement the `EnumerateCameras` gRPC handler in `daemon/grpc_server.py`.
Then replace all local `camutil.list_cameras()` probes in the MCP server and
CLI with calls to the `EnumerateCameras` RPC. After this ticket, no client-side
code probes camera hardware directly for enumeration.

## Acceptance Criteria

- [x] `daemon/grpc_server.py` has an `EnumerateCameras` handler that calls
      `camutil.list_cameras()` and returns a `EnumerateCamerasResponse`.
- [x] `server/mcp_server.py` `_handle_list_cameras()` (around line 499) calls
      `client.enumerate_cameras()` RPC instead of `camutil.list_cameras()`.
      The function must still return the same dict structure as before.
- [x] `cli/cameras_cli.py` replaces the local `list_cameras(...)` call
      (~line 155) with a `DaemonControl` connection + `enumerate_cameras()` RPC.
      The command connects via shared `cli/_daemon.py` helper (see ticket 003).
      For this ticket, a temporary direct connection is acceptable if ticket 003
      is not yet merged; refactor to use the shared helper when 003 lands.
- [x] `cli/calibrate_cli.py` camera selection (~line 159) calls
      `enumerate_cameras()` RPC instead of local `list_cameras()`.
- [x] `grep -n "camutil.list_cameras\|from.*camutil.*import.*list_cameras"
      src/aprilcam/server/mcp_server.py src/aprilcam/cli/cameras_cli.py
      src/aprilcam/cli/calibrate_cli.py` returns zero matches.
- [x] `uv run pytest` passes.

## Implementation Plan

### Approach

1. Add the daemon handler: `EnumerateCameras` in `daemon/grpc_server.py`
   using the existing `camutil.list_cameras(max_index=10, quiet=True,
   detailed_names=True)` call pattern. Wrap in a lock.
2. Update `_handle_list_cameras` in `mcp_server.py`: replace the
   `from aprilcam.camera.camutil import list_cameras as _list_cameras`
   block (around line 501-537) with `_ensure_daemon_client().enumerate_cameras()`.
   Map the result to the same dict format (fields: `index`, `name`, `slug`).
3. Update `cameras_cli.py`: replace local probe with
   `DaemonControl(...)` connection and `enumerate_cameras()` call. Print
   the same output format.
4. Update `calibrate_cli.py`: the `list_cameras()` call at ~159 is used to
   let the user pick a camera index. Replace with `enumerate_cameras()` and
   convert `CameraDevice.index` to the same selection interface.

### Files to Create/Modify

- `src/aprilcam/daemon/grpc_server.py` — add `EnumerateCameras` method to
  the servicer class.
- `src/aprilcam/server/mcp_server.py` — update `_handle_list_cameras`.
- `src/aprilcam/cli/cameras_cli.py` — replace local probe.
- `src/aprilcam/cli/calibrate_cli.py` — replace camera selection probe.

### Testing Plan

- `uv run pytest` (no regressions).
- Manual smoke: with daemon running locally (`aprilcam daemon start`),
  run `aprilcam cameras` — it should return camera info without probing
  locally.
- Check that `list_cameras` MCP tool returns the same output as before
  (dict with `index`, `name` etc.).

### Documentation Updates

None beyond code comments.
