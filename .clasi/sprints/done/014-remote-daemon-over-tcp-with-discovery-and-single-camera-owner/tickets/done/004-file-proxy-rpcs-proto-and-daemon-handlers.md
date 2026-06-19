---
id: 014-004
title: "File-proxy RPCs \u2014 proto additions, regen, and daemon handlers"
status: done
use-cases:
- SUC-005
depends-on:
- 014-001
---

# 014-004: File-proxy RPCs — proto additions, regen, and daemon handlers

## Description

Add the seven file-proxy RPCs to `proto/aprilcam.proto`, regenerate the
Python bindings, and implement the daemon-side handlers in
`daemon/grpc_server.py`. After this ticket the daemon can serve calibration,
config, paths, and playfield data over gRPC; the MCP wiring happens in
ticket 005.

The RPCs use opaque JSON blobs (UTF-8 strings) rather than structured proto
fields so that existing `to_dict`/`from_dict` helpers are reused unchanged.

## Acceptance Criteria

- [x] `proto/aprilcam.proto` contains:
  - `message CameraRequest { string cam_name = 1; }`
  - `message JsonBlobReply { string json_blob = 1; bool present = 2; }`
  - `message CameraJsonRequest { string cam_name = 1; string json_blob = 2; }`
  - `message PlayfieldEntry { string name = 1; string json_blob = 2; }`
  - `message ListPlayfieldsResponse { repeated PlayfieldEntry playfields = 1; }`
  - `rpc GetCameraConfig(CameraRequest) returns (JsonBlobReply)`
  - `rpc SetCameraConfig(CameraJsonRequest) returns (StatusReply)`
  - `rpc GetCalibration(CameraRequest) returns (JsonBlobReply)`
  - `rpc SetCalibration(CameraJsonRequest) returns (StatusReply)`
  - `rpc GetPaths(CameraRequest) returns (JsonBlobReply)`
  - `rpc SetPaths(CameraJsonRequest) returns (StatusReply)`
  - `rpc ListPlayfields(Empty) returns (ListPlayfieldsResponse)`
  - (Note: `StatusReply` may already exist; reuse it or add it.)
- [x] `src/aprilcam/proto/aprilcam_pb2*.py` regenerated and committed.
- [x] `daemon/grpc_server.py` implements all seven handlers:
  - `GetCameraConfig`: read `<cameras_dir>/<cam_name>/config.json` using
    `camera_config.load_camera_config(camera_dir)`, serialize to JSON string.
    Returns `present=False` if file absent.
  - `SetCameraConfig`: atomically write `config.json` using
    `camera_config.save_camera_config(camera_dir, dict)`.
  - `GetCalibration`: read `calibration.json` using
    `calibration.load_calibration_from_camera_dir(camera_dir)`, serialize to
    JSON string. Returns `present=False` if absent.
  - `SetCalibration`: atomically write, then call pipeline reload
    (`reload_calibration` on the live pipeline for that camera).
  - `GetPaths`: read `paths.json`.
  - `SetPaths`: atomically write `paths.json`.
  - `ListPlayfields`: scan `config.playfields_dir / "*.json"` and return each
    as a `PlayfieldEntry`.
- [x] Each handler resolves `camera_dir` from `cam_name` using the camera
      registry (same pattern as `OpenCamera` handler).
- [x] Handlers use atomic write: write to `.tmp` then `os.replace`.
- [x] `client/control.py` has stub methods for all seven RPCs returning
      raw `JsonBlobReply` / `ListPlayfieldsResponse` (MCP parsing happens in 005).
- [x] `uv run pytest` passes.

## Implementation Plan

### Approach

Edit proto, regen, then implement handlers one RPC at a time. Reuse the
existing helpers:
- `camera/camera_config.py`: `load_camera_config`, `save_camera_config`.
- `calibration/calibration.py`: `load_calibration_from_camera_dir`,
  `save_calibration_to_camera_dir` (confirm this function exists; if not,
  extract from the existing save logic).
- `core/playfield_def.py`: scan playfields dir (Sprint 012 introduced this).

### Files to Create/Modify

- `proto/aprilcam.proto` — add messages and RPCs listed above.
- `src/aprilcam/proto/aprilcam_pb2*.py` — regenerated.
- `src/aprilcam/daemon/grpc_server.py` — add seven handler methods.
- `src/aprilcam/client/control.py` — add seven client stub methods.

### `SetCalibration` handler note

After writing the new `calibration.json`, call:
```python
pipeline = self._cameras.get(cam_name)
if pipeline is not None:
    pipeline.reload_calibration()
```
The `reload_calibration` method should already exist on `CameraPipeline`
(from the existing `ReloadCalibration` RPC if present, or add it).

### `ListPlayfields` handler note

```python
import json
from pathlib import Path

playfields_dir = self._config.playfields_dir
entries = []
for p in sorted(playfields_dir.glob("*.json")):
    entries.append(aprilcam_pb2.PlayfieldEntry(
        name=p.stem,
        json_blob=p.read_text(),
    ))
return aprilcam_pb2.ListPlayfieldsResponse(playfields=entries)
```

### Testing Plan

- Unit test each handler with a temp directory: write a known JSON file,
  call `GetX`, assert the blob matches; call `SetX` with a new blob, assert
  the file was updated atomically.
- Test `SetCalibration` calls `reload_calibration` (mock the pipeline).
- `uv run pytest` full suite passes.

### Documentation Updates

None beyond code comments documenting each handler.
