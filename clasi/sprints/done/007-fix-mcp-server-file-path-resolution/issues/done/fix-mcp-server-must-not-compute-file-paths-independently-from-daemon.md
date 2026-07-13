---
status: done
sprint: '007'
tickets:
- 007-001
---

# Fix: MCP server must not compute file paths independently from daemon

## Context

The MCP server and daemon can be started from different working directories.
`Config.load()` defaults `data_dir` to `./data/aprilcam/` — **relative to CWD**.
Any place the MCP server independently computes a file path using `Config.load()`
will silently use the wrong directory, causing:

- `paths.json` written somewhere the daemon never reads → paths don't appear in live view
- calibration data saved/loaded from wrong dir → playfield calibration breaks
- `_get_paths_file()` fallback reads `info.json` from wrong dir → paths_file lookup fails

**The MCP server should never compute data file paths on its own.**
The daemon is the authority on where its files live. The MCP server should ask
the daemon once (at `open_camera` time) and cache the result.

## Four problem sites in `mcp_server.py`

| Line | What it does | Problem |
|------|-------------|---------|
| 475–477 | `open_camera` — computes `paths_file` locally | writes paths.json to MCP server's CWD |
| 248–249 | `_get_paths_file()` fallback — reads `info.json` from `config.cameras_dir` | looks in wrong dir if `_cam_info` miss |
| 669–678 | `create_playfield` — loads calibration from `cfg.cameras_dir` | reads from wrong dir |
| 822–833 | `calibrate_playfield` — saves calibration to `cfg.calibration_dir` | writes to wrong dir; daemon's `ReloadCalibration` then reads from its own (correct) dir and finds nothing |

## Fix: return `camera_dir` from the daemon in `OpenCameraResponse`

One new proto field covers all four cases. The daemon returns its authoritative
`cameras_dir / cam_name` path; the MCP server stores it in `_cam_info` and uses
it everywhere instead of calling `Config.load()`.

### 1. `proto/aprilcam.proto`

```protobuf
message OpenCameraResponse {
  string cam_name   = 1;
  string camera_dir = 2;   // absolute path to daemon's per-camera data directory
}
```

### 2. `src/aprilcam/daemon/grpc_server.py`

In `OpenCamera()`, populate `camera_dir` in both the normal return (line 126)
and the already-open early-return (line 102):

```python
camera_dir = str(self._config.cameras_dir / cam_name)
return aprilcam_pb2.OpenCameraResponse(cam_name=cam_name, camera_dir=camera_dir)
```

### 3. `src/aprilcam/client/control.py`

Return both fields from `open_camera()`:

```python
def open_camera(self, index: int) -> tuple[str, str]:
    """Open camera; return (cam_name, camera_dir)."""
    resp = stub.OpenCamera(aprilcam_pb2.OpenCameraRequest(index=index))
    return str(resp.cam_name), str(resp.camera_dir)
```

### 4. `src/aprilcam/server/mcp_server.py` — four edits

**`open_camera` (lines 470–501)**: use daemon-returned `camera_dir`; remove
local `Config.load()` call:
```python
cam_name, camera_dir = client.open_camera(idx)
handle = cam_name
info: dict = {
    "camera_dir": camera_dir,
    "paths_file": str(Path(camera_dir) / "paths.json"),
}
```

**`_get_paths_file()` fallback (lines 246–254)**: replace `Config.load()` +
`info.json` read with a lookup in `_cam_info["camera_dir"]` — if `camera_dir`
is present, derive `paths_file` directly; no disk read needed.

**`create_playfield` (lines 664–678)**: replace `Config.load()` + local
`cam_dir` with `Path(_cam_info[camera_id]["camera_dir"])`.

**`calibrate_playfield` (lines 817–833)**: replace `Config.load()` +
`cfg.calibration_dir` with `Path(_cam_info[cam_name]["camera_dir"])` as the
save target, then call `reload_calibration` on the daemon as before.

### 5. Regenerate protobuf

```bash
cd /Volumes/Proj/proj/RobotProjects/AprilTags
python -m grpc_tools.protoc -I proto \
  --python_out=src/aprilcam/proto \
  --grpc_python_out=src/aprilcam/proto \
  proto/aprilcam.proto
```

Fix bare import in `aprilcam_pb2_grpc.py`:
```python
# Change:
import aprilcam_pb2 as aprilcam__pb2
# To:
from aprilcam.proto import aprilcam_pb2 as aprilcam__pb2
```

## Critical files

- `proto/aprilcam.proto` — add `camera_dir` to `OpenCameraResponse`
- `src/aprilcam/daemon/grpc_server.py:86–126` — populate `camera_dir` in response
- `src/aprilcam/client/control.py:206–212` — return `(cam_name, camera_dir)` tuple
- `src/aprilcam/server/mcp_server.py:248,475,664,817` — use `_cam_info["camera_dir"]` everywhere
- `src/aprilcam/proto/aprilcam_pb2.py` — regenerated
- `src/aprilcam/proto/aprilcam_pb2_grpc.py` — regenerated + import fix

## Verification

1. Start daemon from directory A.
2. Start MCP server (Claude Code session) from directory B.
3. `open_camera` → `create_playfield` → `create_path` → confirm path appears in live view.
4. `calibrate_playfield` → restart MCP server → `create_path` → confirm calibration persists.
5. Check `paths.json` is in the daemon's data dir (`data/aprilcam/cameras/<cam>/`), not CWD.
