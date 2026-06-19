---
id: 014-005
title: "MCP server \u2014 wire file-proxy RPCs and remove local disk access"
status: done
use-cases:
- SUC-005
- SUC-003
depends-on:
- 014-003
- 014-004
---

# 014-005: MCP server — wire file-proxy RPCs and remove local disk access

## Description

Update `server/mcp_server.py` (and `server/web_server.py` where applicable) to:
1. Call the file-proxy RPCs from ticket 004 instead of reading/writing local
   filesystem paths.
2. Add `parse-from-dict` companion functions to the calibration and camera-config
   modules so the MCP side can deserialize RPC JSON blobs without re-reading files.
3. Load playfield definitions via `ListPlayfields` RPC on daemon connect.
4. Remove all `_cam_info["camera_dir"]` and `paths_file` local-path uses.

## Acceptance Criteria

- [x] `calibration/calibration.py` has a new `parse_calibration_from_dict(d: dict)`
      function (pure, no I/O) that accepts the JSON blob dict and returns a
      `CameraCalibration` object. It reuses the existing `CameraCalibration.from_dict`.
- [x] `camera/camera_config.py` has a new `parse_camera_config(d: dict) -> dict`
      function (trivially returns the dict; validates required keys).
- [x] `_handle_open_camera` in `mcp_server.py` calls `GetCalibration` and
      `GetCameraConfig` RPCs instead of using `_cam_info["camera_dir"]` to
      read local files.
- [x] `calibrate_playfield` tool calls `SetCalibration` RPC to persist the
      calibration result instead of writing a local file.
- [x] `set_camera_playfield` tool calls `SetCameraConfig` RPC.
- [x] Path tools (`create_path`, `list_paths`, `delete_path`, `clear_paths`)
      call `GetPaths`/`SetPaths` RPCs.
- [x] On daemon connect (inside `_ensure_daemon_client()` or a new
      `_on_daemon_connect()` hook): call `ListPlayfields` RPC and populate
      the `playfield_def_registry` module-level object. This replaces
      `playfield_def_registry.load_all(config.playfields_dir)` (which assumed
      local disk access to the daemon's files).
- [x] `grep -n "camera_dir\|paths_file" src/aprilcam/server/mcp_server.py`
      returns zero references that assume a local-disk path to daemon files.
- [x] `uv run pytest` passes.
- [x] (Optional, if time permits) `web_server.py` updated consistently (no local-disk refs found).

## Implementation Plan

### Approach

Work through `mcp_server.py` tool by tool, replacing each local I/O call
with the appropriate RPC. Start with `_handle_open_camera` (highest value),
then calibrate, paths, playfields.

### Parse-from-dict helpers

Add to `calibration/calibration.py`:
```python
def parse_calibration_from_dict(d: dict) -> CameraCalibration:
    """Deserialize a calibration dict (from GetCalibration JSON blob)."""
    return CameraCalibration.from_dict(d)
```

Add to `camera/camera_config.py`:
```python
def parse_camera_config(d: dict) -> dict:
    """Validate and return a camera config dict (from GetCameraConfig JSON blob)."""
    # minimal validation: check expected keys
    return d
```

### `_handle_open_camera` changes

Replace:
```python
camera_dir = _cam_info[camera_id]["camera_dir"]
calibration = load_calibration_from_camera_dir(camera_dir)
config_dict = load_camera_config(camera_dir)
```
With:
```python
client = _ensure_daemon_client()
cal_reply = client.get_calibration(cam_name)
cal = parse_calibration_from_dict(json.loads(cal_reply.json_blob)) if cal_reply.present else None
cfg_reply = client.get_camera_config(cam_name)
cfg = parse_camera_config(json.loads(cfg_reply.json_blob)) if cfg_reply.present else {}
```

### Playfield loading on connect

In `_ensure_daemon_client()`, after a successful `ListCameras` probe, call:
```python
pf_reply = client.list_playfields()
playfield_def_registry.clear()
for entry in pf_reply.playfields:
    playfield_def_registry.add_from_dict(entry.name, json.loads(entry.json_blob))
```
(Add `clear()` and `add_from_dict()` to `PlayfieldDefinitionRegistry` in
`core/playfield_def.py` if they don't exist.)

### `connect_daemon` MCP tool (also in this ticket)

Implement the `connect_daemon(host=None, port=5280, local=False)` MCP tool:
- Tear down session state: iterate detection loops and stop them; stop live views;
  call `registry.close_all()` (camera registry); clear `_cam_info`,
  `_frame_registry`, `_composite_registry`, `_path_registry`.
- Close old `_daemon_client` gRPC channel.
- Determine new target: if `host is None and not local`, call
  `resolve_daemon_target(config)` (mDNS); if `local`, use unix socket; else
  use `(host, port)`.
- Reconnect; probe; call `_on_daemon_connect()` (load playfields, etc.).
- Return `{"target": "<host>:<port>", "cameras": [...]}`.

### `get_version` tool change

Extend the return dict with `"active_daemon_host"` and `"active_daemon_port"`
from the current `_daemon_client` connection state.

### Files to Create/Modify

- `src/aprilcam/calibration/calibration.py` — add `parse_calibration_from_dict`.
- `src/aprilcam/camera/camera_config.py` — add `parse_camera_config`.
- `src/aprilcam/core/playfield_def.py` — add `clear()` and `add_from_dict()`
  to `PlayfieldDefinitionRegistry`.
- `src/aprilcam/server/mcp_server.py` — update all file I/O paths; add
  `connect_daemon` tool; update `get_version`; add `_on_daemon_connect()`.

### Testing Plan

- Unit test `parse_calibration_from_dict` with a sample dict.
- Unit test `connect_daemon` session teardown (mock client, mock registries).
- `uv run pytest` full suite.
- Manual: call `calibrate_playfield` against a local daemon over TCP — confirm
  the calibration file is written on the daemon side.

### Documentation Updates

- Update MCP tool list in `AGENT_GUIDE.md` to include `connect_daemon`.
- Update `get_version` tool description in the guide.
