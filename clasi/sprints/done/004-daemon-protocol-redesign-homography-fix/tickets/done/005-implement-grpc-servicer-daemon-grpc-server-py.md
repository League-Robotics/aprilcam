---
id: '005'
title: Implement gRPC servicer (daemon/grpc_server.py)
status: done
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
  - SUC-006
depends-on:
  - '002'
  - '004'
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement gRPC servicer (daemon/grpc_server.py)

## Description

Create `src/aprilcam/daemon/grpc_server.py` containing `AprilCamServicer` — the
Python class that implements the `AprilCam` gRPC service. The servicer receives typed
gRPC requests, delegates camera management to a reference to `DaemonServer`'s camera
registry, and manages the lifecycle of `ImageStreamProducer` and `TagStreamProducer`
instances.

The servicer also enables gRPC Server Reflection at server construction time so that
`grpcurl` and other tools can introspect the service schema.

This ticket creates only the servicer class. The gRPC server object (binding to
Unix socket and/or TCP) is created by `DaemonServer` in ticket 006.

## Acceptance Criteria

- [ ] `AprilCamServicer` implements all 10 methods of the `AprilCam` service:
      `ListCameras`, `OpenCamera`, `CloseCamera`, `ReloadCalibration`, `Shutdown`,
      `GetCameraInfo`, `CaptureFrame`, `GetTags`, `GetImageStream`, `GetTagStream`.
- [ ] `GetImageStream` creates an `ImageStreamProducer` if one does not exist for the
      camera, starts it, stores the `StreamEndpoint`, and returns it.
- [ ] `GetTagStream` creates a `TagStreamProducer` if one does not exist, honouring
      `StreamRequest.max_hz`, and returns the `StreamEndpoint`.
- [ ] `GetCameraInfo` returns a `CameraInfoResponse` with correct fields populated
      from the open camera pipeline.
- [ ] `CaptureFrame` returns a `CaptureFrameResponse` with the raw JPEG bytes.
- [ ] `GetTags` returns a `TagFrameResponse` with current tag detections.
- [ ] gRPC Server Reflection is enabled (method `make_grpc_server(transports, config)`
      returns a `grpc.Server` with reflection registered).
- [ ] `uv run pytest` passes.

## Implementation Plan

### Approach

1. Create `src/aprilcam/daemon/grpc_server.py`.
2. Define `AprilCamServicer(aprilcam_pb2_grpc.AprilCamServicer)`:
   - Constructor accepts a reference to the camera registry dict and config.
   - Per-camera producer instances stored in `self._image_producers` and
     `self._tag_producers` (dict keyed by cam_name).
   - `ListCameras`: return `ListCamerasResponse(cameras=list(registry.keys()))`.
   - `OpenCamera`: delegate to existing `_rpc_open_camera` logic (moved from server.py).
   - `CloseCamera`: stop producers for the camera, then stop the pipeline.
   - `GetImageStream`: if no producer yet, create `ImageStreamProducer(cam_name, config)`,
     call `start()`, store endpoint. Return `StreamEndpoint`.
   - `GetTagStream`: same pattern for `TagStreamProducer`, honour `max_hz`.
   - `GetCameraInfo`: read from the pipeline or info.json, return `CameraInfoResponse`.
   - `CaptureFrame`: call `pipeline.capture_frame()`, return bytes in response.
   - `GetTags`: call `pipeline.get_current_tags()`, serialize to `TagFrameResponse`.
3. Define `make_grpc_server(transports, servicer)` helper that:
   - Creates a `grpc.server(futures.ThreadPoolExecutor(...))`.
   - Adds the servicer via `aprilcam_pb2_grpc.add_AprilCamServicer_to_server`.
   - Enables reflection via `reflection.enable_server_reflection(...)`.
   - Returns the configured server (not yet started; `DaemonServer` calls `start()`).

### Files to Create / Modify

- `src/aprilcam/daemon/grpc_server.py` — new
- `src/aprilcam/daemon/camera_pipeline.py` — add `get_current_tags()` method if
  not already present (returns latest `TagFrame`-compatible data)

### Testing Plan

- `tests/test_grpc_servicer.py`:
  - Create a mock camera registry and `AprilCamServicer`.
  - Call `ListCameras` with an empty registry; verify response is empty list.
  - Call `GetImageStream` twice for the same camera; verify the same
    `StreamEndpoint` is returned (idempotent).
  - Verify `make_grpc_server` returns a `grpc.Server` instance without error.
- Run `uv run pytest tests/test_grpc_servicer.py`.
