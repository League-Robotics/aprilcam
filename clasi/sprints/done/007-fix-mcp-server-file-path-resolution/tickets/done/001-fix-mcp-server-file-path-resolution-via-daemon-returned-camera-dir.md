---
id: '001'
title: Fix MCP server file path resolution via daemon-returned camera_dir
status: done
use-cases:
  - SUC-001
depends-on: []
github-issue: ''
issue: fix-mcp-server-must-not-compute-file-paths-independently-from-daemon.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix MCP server file path resolution via daemon-returned camera_dir

## Description

The MCP server currently calls `Config.load()` in four places to compute data file paths independently of the daemon. `Config.load()` defaults `data_dir` to `./data/aprilcam/` relative to CWD. When the MCP server and daemon run from different directories, these paths diverge silently — `paths.json` lands in the wrong place, calibration is written where the daemon will never find it, and the `_get_paths_file()` fallback reads from the wrong directory.

The fix adds a `camera_dir` field to `OpenCameraResponse` in the proto. The daemon returns its authoritative `cameras_dir / cam_name` path at `open_camera` time. The MCP server stores it in `_cam_info` and uses it at all four problem sites.

## Acceptance Criteria

- [ ] `proto/aprilcam.proto`: `OpenCameraResponse` has `string camera_dir = 2`
- [ ] `daemon/grpc_server.py`: `OpenCamera()` populates `camera_dir` at both return points (normal return ~line 126 and already-open early-return ~line 102)
- [ ] `client/control.py`: `open_camera()` returns `tuple[str, str]` — `(cam_name, camera_dir)`
- [ ] `server/mcp_server.py` `open_camera` (~line 475): uses daemon-returned `camera_dir`; stores in `_cam_info`; no `Config.load()` call
- [ ] `server/mcp_server.py` `_get_paths_file()` fallback (~line 248): uses `_cam_info[camera_id]["camera_dir"]`; no `Config.load()`, no `info.json` disk read
- [ ] `server/mcp_server.py` `create_playfield` (~line 669): uses `Path(_cam_info[camera_id]["camera_dir"])`; no `Config.load()`
- [ ] `server/mcp_server.py` `calibrate_playfield` (~line 822): saves to `Path(_cam_info[cam_name]["camera_dir"])`; no `Config.load()`
- [ ] Protobuf bindings regenerated (`aprilcam_pb2.py`, `aprilcam_pb2_grpc.py`)
- [ ] `aprilcam_pb2_grpc.py` uses package-relative import: `from aprilcam.proto import aprilcam_pb2 as aprilcam__pb2`
- [ ] `uv run pytest` passes
- [ ] Manual verification: start daemon from dir A, MCP session from dir B; `open_camera` → `create_path` → path appears in live view; `paths.json` is in the daemon's data dir

## Implementation Plan

### Approach

Work top-down through the stack: proto → daemon → client → MCP server → regenerate bindings. Each step is a prerequisite for the next.

### Step 1: Update `proto/aprilcam.proto`

File: `proto/aprilcam.proto`

Add `string camera_dir = 2;` to `OpenCameraResponse`:

```protobuf
message OpenCameraResponse {
  string cam_name   = 1;
  string camera_dir = 2;   // absolute path to daemon's per-camera data directory
}
```

### Step 2: Update `daemon/grpc_server.py`

File: `src/aprilcam/daemon/grpc_server.py`, lines 86–126

In `OpenCamera()`, compute `camera_dir` and include it in both return statements:

```python
camera_dir = str(self._config.cameras_dir / cam_name)
return aprilcam_pb2.OpenCameraResponse(cam_name=cam_name, camera_dir=camera_dir)
```

Apply this to:
- The already-open early-return (~line 102)
- The normal return (~line 126)

### Step 3: Update `client/control.py`

File: `src/aprilcam/client/control.py`, lines 206–212

Change `open_camera()` to unpack and return both fields:

```python
def open_camera(self, index: int) -> tuple[str, str]:
    """Open camera; return (cam_name, camera_dir)."""
    resp = stub.OpenCamera(aprilcam_pb2.OpenCameraRequest(index=index))
    return str(resp.cam_name), str(resp.camera_dir)
```

### Step 4: Update `server/mcp_server.py` — four sites

File: `src/aprilcam/server/mcp_server.py`

**Site 1 — `open_camera` (~lines 475–501)**

Replace:
```python
cam_name = client.open_camera(idx)
handle = cam_name
cfg = Config.load()
info: dict = {
    "paths_file": str(cfg.data_dir / "cameras" / cam_name / "paths.json"),
}
```
With:
```python
cam_name, camera_dir = client.open_camera(idx)
handle = cam_name
info: dict = {
    "camera_dir": camera_dir,
    "paths_file": str(Path(camera_dir) / "paths.json"),
}
```
Remove the `Config.load()` import use and any unused `cfg` variable.

**Site 2 — `_get_paths_file()` fallback (~lines 246–254)**

The fallback fires when `camera_id` is not in `_cam_info` or `paths_file` is missing. Replace the `Config.load()` + `info.json` disk-read path with a direct derivation from `_cam_info["camera_dir"]`:

```python
if camera_id in self._cam_info and "camera_dir" in self._cam_info[camera_id]:
    return str(Path(self._cam_info[camera_id]["camera_dir"]) / "paths.json")
# If camera_id is not in _cam_info, raise an appropriate error
raise ValueError(f"Camera {camera_id} not open or camera_dir not available")
```

**Site 3 — `create_playfield` (~lines 664–678)**

Replace:
```python
cfg = Config.load()
cam_dir = cfg.cameras_dir / cam_name
```
With:
```python
cam_dir = Path(self._cam_info[camera_id]["camera_dir"])
```

**Site 4 — `calibrate_playfield` (~lines 817–833)**

Replace:
```python
cfg = Config.load()
save_path = cfg.calibration_dir / ...
```
With:
```python
save_path = Path(self._cam_info[cam_name]["camera_dir"]) / ...
```
Then call `reload_calibration` on the daemon as before.

After all four edits, verify no remaining `Config.load()` calls exist in `mcp_server.py` for path computation.

### Step 5: Regenerate protobuf bindings

```bash
cd /Volumes/Proj/proj/RobotProjects/AprilTags
python -m grpc_tools.protoc -I proto \
  --python_out=src/aprilcam/proto \
  --grpc_python_out=src/aprilcam/proto \
  proto/aprilcam.proto
```

Then fix the bare import in `src/aprilcam/proto/aprilcam_pb2_grpc.py`:

```python
# Change:
import aprilcam_pb2 as aprilcam__pb2
# To:
from aprilcam.proto import aprilcam_pb2 as aprilcam__pb2
```

### Files to Create

None.

### Files to Modify

- `proto/aprilcam.proto`
- `src/aprilcam/daemon/grpc_server.py`
- `src/aprilcam/client/control.py`
- `src/aprilcam/server/mcp_server.py`
- `src/aprilcam/proto/aprilcam_pb2.py` (regenerated)
- `src/aprilcam/proto/aprilcam_pb2_grpc.py` (regenerated + import fix)

### Testing Plan

- Run `uv run pytest` — all existing tests must pass.
- Manual verification per SUC-001 acceptance criteria:
  1. Start daemon from directory A.
  2. Open a new Claude Code MCP session from directory B.
  3. Call `open_camera` → `create_playfield` → `create_path`.
  4. Confirm path appears in live view.
  5. Check `paths.json` is in the daemon's `data/aprilcam/cameras/<cam>/` directory, not in directory B.
  6. Call `calibrate_playfield`, restart MCP session, call `create_path` — confirm calibration persists.

### Documentation Updates

None required. This is an internal plumbing fix with no user-visible API change.
