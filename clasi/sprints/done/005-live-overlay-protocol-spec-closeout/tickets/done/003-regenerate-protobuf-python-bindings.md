---
id: '003'
title: Regenerate protobuf Python bindings
status: done
use-cases:
  - SUC-002
  - SUC-003
  - SUC-004
depends-on:
  - '002'
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Regenerate protobuf Python bindings

## Description

After the proto changes in ticket 002, the generated Python bindings
(`src/aprilcam/proto/aprilcam_pb2.py` and `aprilcam_pb2_grpc.py`) must be
regenerated. All subsequent tickets depend on the new `StreamMessage`,
`OverlayFrame`, and `PublishOverlay` stubs being available at import time.

## Acceptance Criteria

- [ ] `src/aprilcam/proto/aprilcam_pb2.py` is regenerated and contains
      `StreamMessage`, `OverlayFrame`, `OverlayElement`, `PublishOverlayRequest`.
- [ ] `src/aprilcam/proto/aprilcam_pb2_grpc.py` is regenerated and contains
      `PublishOverlay` stub and servicer method.
- [ ] `uv run python -c "from aprilcam.proto import aprilcam_pb2; print(aprilcam_pb2.StreamMessage())"` succeeds.
- [ ] `uv run pytest tests/` passes (no regressions from regenerated bindings).

## Implementation Plan

### Approach

Run the grpc_tools.protoc command to regenerate the Python bindings from the
updated proto file. Commit the generated files (they are already committed in
this repo).

### Regeneration Command

```bash
cd /Volumes/Proj/proj/RobotProjects/AprilTags && \
uv run python -m grpc_tools.protoc \
  -I proto \
  --python_out=src/aprilcam/proto \
  --grpc_python_out=src/aprilcam/proto \
  proto/aprilcam.proto
```

### Files to Modify

- `src/aprilcam/proto/aprilcam_pb2.py` — regenerated (overwrite)
- `src/aprilcam/proto/aprilcam_pb2_grpc.py` — regenerated (overwrite)

### Testing Plan

- Import smoke test: `uv run python -c "from aprilcam.proto import aprilcam_pb2; aprilcam_pb2.StreamMessage()"`
- `uv run pytest tests/` — no regressions

### Documentation Updates

None.
