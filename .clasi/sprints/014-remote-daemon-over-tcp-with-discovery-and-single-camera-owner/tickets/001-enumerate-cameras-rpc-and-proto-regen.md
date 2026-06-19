---
id: '014-001'
title: EnumerateCameras RPC — proto addition and regeneration
status: open
use-cases:
  - SUC-001
  - SUC-002
depends-on: []
---

# 014-001: EnumerateCameras RPC — proto addition and regeneration

## Description

Add the `EnumerateCameras` RPC and `CameraDevice` message to
`proto/aprilcam.proto`, then regenerate the Python bindings. This is a
foundation ticket: all subsequent tickets that call `enumerate_cameras()`
depend on the generated stubs being available.

The existing `ListCameras` RPC ("currently-open cameras") is unchanged.
`EnumerateCameras` is new: the daemon queries available (not necessarily open)
hardware devices and returns them.

## Acceptance Criteria

- [ ] `proto/aprilcam.proto` contains `CameraDevice` message with fields
      `index` (int32), `name` (string), `slug` (string).
- [ ] `proto/aprilcam.proto` contains `EnumerateCamerasResponse` message
      with `repeated CameraDevice cameras`.
- [ ] `proto/aprilcam.proto` contains `rpc EnumerateCameras(Empty) returns
      (EnumerateCamerasResponse)` in the `AprilCam` service.
- [ ] `src/aprilcam/proto/aprilcam_pb2.py` and `aprilcam_pb2_grpc.py` are
      regenerated and committed.
- [ ] `client/control.py` has a new `enumerate_cameras()` method that calls
      the stub and returns `list[CameraDevice]` (using the existing
      `CameraInfo`-style Pydantic model or a new `CameraDevice` model from
      `client/models.py`).
- [ ] `uv run pytest` passes (no regressions from proto regen).

## Implementation Plan

### Approach

Edit `proto/aprilcam.proto` to add the new message and RPC. Regenerate with
`grpcio-tools` (available in the `dev` extra). Add the client stub method.

### Files to Create/Modify

- `proto/aprilcam.proto` — add `CameraDevice`, `EnumerateCamerasResponse`,
  and `rpc EnumerateCameras`.
- `src/aprilcam/proto/aprilcam_pb2.py` — regenerated.
- `src/aprilcam/proto/aprilcam_pb2_grpc.py` — regenerated.
- `src/aprilcam/client/models.py` — add `CameraDevice` Pydantic model if
  one does not already exist.
- `src/aprilcam/client/control.py` — add `enumerate_cameras()` method that
  calls `self._stub.EnumerateCameras(aprilcam_pb2.Empty())` and converts
  the response to a list of `CameraDevice` models.

### Regeneration command

```bash
python -m grpc_tools.protoc \
    -I proto \
    --python_out=src/aprilcam/proto \
    --grpc_python_out=src/aprilcam/proto \
    proto/aprilcam.proto
```

Run from the repo root. Fix any import path issues in generated files
(`from aprilcam.proto import aprilcam_pb2` style).

### Testing Plan

- Run `uv run pytest` to confirm no regressions.
- Smoke-test: `python -c "from aprilcam.proto import aprilcam_pb2; print(aprilcam_pb2.EnumerateCamerasResponse)"` must not raise.
- Smoke-test: `python -c "from aprilcam.client.control import DaemonControl; print(DaemonControl.enumerate_cameras)"` must not raise.

### Documentation Updates

None required for this ticket. The RPC is documented in the architecture update.
