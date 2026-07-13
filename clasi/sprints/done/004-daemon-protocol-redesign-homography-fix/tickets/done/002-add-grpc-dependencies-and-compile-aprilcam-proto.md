---
id: '002'
title: Add gRPC dependencies and compile aprilcam.proto
status: done
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
  - SUC-006
depends-on: []
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add gRPC dependencies and compile aprilcam.proto

## Description

This ticket lays the foundation for all gRPC work in Sprint 004. It has two parts:

1. Add `grpcio`, `grpcio-tools`, `grpcio-reflection`, `protobuf`, `zeroconf`, and
   `pydantic` as project dependencies in `pyproject.toml`.

2. Write `proto/aprilcam.proto` with the full `AprilCam` gRPC service definition and
   all message types as specified in the architecture. Compile it to generate
   `src/aprilcam/proto/aprilcam_pb2.py` and `src/aprilcam/proto/aprilcam_pb2_grpc.py`.
   Add a `scripts/compile_proto.py` helper so the compilation step can be re-run.

Generated `*_pb2` files are committed to the repository so the package is usable
without a build step.

## Acceptance Criteria

- [x] `pyproject.toml` lists `grpcio`, `grpcio-tools`, `grpcio-reflection`, `protobuf`,
      `zeroconf`, `pydantic` as dependencies.
- [x] `proto/aprilcam.proto` exists and contains the full `AprilCam` service with all
      messages: `Empty`, `ListCamerasResponse`, `OpenCameraRequest`, `OpenCameraResponse`,
      `CameraRequest`, `CaptureFrameResponse`, `CameraInfoResponse`, `TagFrameResponse`,
      `TagMsg`, `ImageFrame`, `TagFrame`, `StreamRequest`, `StreamEndpoint`.
- [x] `src/aprilcam/proto/__init__.py` exists (makes it a package).
- [x] `src/aprilcam/proto/aprilcam_pb2.py` and `src/aprilcam/proto/aprilcam_pb2_grpc.py`
      are generated and committed.
- [x] `scripts/compile_proto.py` (or `Makefile` target) exists and produces the same
      files when re-run.
- [x] `from aprilcam.proto import aprilcam_pb2` succeeds after `uv sync`.
- [x] `uv run pytest` passes (no import errors from new deps).

## Implementation Plan

### Approach

1. Update `pyproject.toml` `[project.dependencies]` with the new packages.
2. Run `uv sync` to lock the new dependencies.
3. Create `proto/aprilcam.proto` at the project root with the full service definition
   (see architecture-update.md Section 2.2 for the sketch; flesh out `TagMsg`,
   `ImageFrame`, `TagFrame` from Sections 2.3 and 2.4).
4. Create `src/aprilcam/proto/` directory with `__init__.py`.
5. Write `scripts/compile_proto.py` that runs:
   ```
   python -m grpc_tools.protoc \
     -I proto \
     --python_out=src/aprilcam/proto \
     --grpc_python_out=src/aprilcam/proto \
     proto/aprilcam.proto
   ```
6. Run the compile script; commit the generated files.

### Files to Create / Modify

- `pyproject.toml` — add dependencies
- `proto/aprilcam.proto` — new proto file
- `src/aprilcam/proto/__init__.py` — new package init
- `src/aprilcam/proto/aprilcam_pb2.py` — generated (committed)
- `src/aprilcam/proto/aprilcam_pb2_grpc.py` — generated (committed)
- `scripts/compile_proto.py` — compile helper
- `uv.lock` — updated lockfile

### Proto Message Summary

```protobuf
service AprilCam {
  rpc ListCameras(Empty) returns (ListCamerasResponse);
  rpc OpenCamera(OpenCameraRequest) returns (OpenCameraResponse);
  rpc CloseCamera(CameraRequest) returns (Empty);
  rpc ReloadCalibration(CameraRequest) returns (Empty);
  rpc Shutdown(Empty) returns (Empty);
  rpc GetCameraInfo(CameraRequest) returns (CameraInfoResponse);
  rpc CaptureFrame(CameraRequest) returns (CaptureFrameResponse);
  rpc GetTags(CameraRequest) returns (TagFrameResponse);
  rpc GetImageStream(StreamRequest) returns (StreamEndpoint);
  rpc GetTagStream(StreamRequest) returns (StreamEndpoint);
}
```

### Testing Plan

- No new behavioural tests needed for this ticket.
- Verify `from aprilcam.proto import aprilcam_pb2` does not raise.
- Run `uv run pytest` to ensure no import regressions.
