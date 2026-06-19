---
id: '001'
title: Add GetObjects RPC to proto and daemon
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: thin-mcp-web-view-clients-move-all-vision-into-the-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add GetObjects RPC to proto and daemon

## Description

Add a `GetObjects` RPC to `proto/aprilcam.proto`, regenerate the Python stubs,
implement the handler in `daemon/grpc_server.py`, and add a `get_objects()` method
to `client/control.py`. This moves object detection (HSV color classification +
polygon filter) from the MCP server into the daemon — the only component that runs
cv2.

The MCP `get_objects` tool currently runs `ColorClassifier` + `cv2.pointPolygonTest`
inside `_handle_get_objects` in `mcp_server.py` (~lines 1291–1371). After this ticket,
the MCP tool (ticket 003) will call `client.get_objects()` instead. This ticket
delivers the daemon side first so ticket 003 has a complete stub to call.

## Acceptance Criteria

- [x] `proto/aprilcam.proto` contains `ObjectRecord` message, `GetObjectsResponse`
  message, and `rpc GetObjects(CameraRequest) returns (GetObjectsResponse)`.
- [x] `uv run python scripts/compile_proto.py` succeeds and the regenerated
  `src/aprilcam/proto/aprilcam_pb2*.py` files contain `GetObjects`, `ObjectRecord`,
  and `GetObjectsResponse` symbols.
- [x] `daemon/grpc_server.py` has a `GetObjects(self, request, context)` handler on
  the servicer class that:
  - Looks up the camera pipeline by `request.cam_name`; returns gRPC NOT_FOUND
    status if unknown.
  - Grabs the latest frame from the pipeline.
  - Runs `ColorClassifier(min_area=600, max_area=30000).classify(frame, homography=H)`
    (where H comes from the camera's calibration if available, else None).
  - Applies polygon-containment filter (inset 60 px) using `cv.pointPolygonTest`.
  - Applies aspect ratio / minimum-dimension filter (aspect > 2.0 or min dim < 15 → skip).
  - Applies A1 coord shift (subtract `origin_x`, `origin_y`) to world_xy.
  - Returns `GetObjectsResponse` with `repeated ObjectRecord`.
- [x] `client/control.py` has a `get_objects(cam_name: str) -> GetObjectsResponse` method.
- [x] `tests/test_015_001_get_objects_rpc.py` passes with `uv run pytest
  tests/test_015_001_get_objects_rpc.py`.
- [x] `uv run pytest` (full suite) green.

## Implementation Plan

### Proto additions (`proto/aprilcam.proto`)

Add after the `TagFrameResponse` message:

```proto
message ObjectRecord {
  float  cx_px       = 1;
  float  cy_px       = 2;
  float  wx          = 3;   // world X cm (A1-centred); 0 = uncalibrated
  float  wy          = 4;   // world Y cm (A1-centred); 0 = uncalibrated
  string color       = 5;
  int32  x_bbox      = 6;
  int32  y_bbox      = 7;
  int32  w_bbox      = 8;
  int32  h_bbox      = 9;
  float  area_px     = 10;
  string object_type = 11;
  float  confidence  = 12;
}

message GetObjectsResponse {
  string               cam_name = 1;
  repeated ObjectRecord objects  = 2;
}
```

Add to `service AprilCam` alongside the other one-shot queries:
```proto
rpc GetObjects (CameraRequest) returns (GetObjectsResponse);
```

### Stub regeneration

```bash
uv run python scripts/compile_proto.py
```

Confirm `aprilcam_pb2.py` contains `ObjectRecord` and `aprilcam_pb2_grpc.py`
contains `GetObjects` in the stub.

### Daemon handler (`daemon/grpc_server.py`)

Port from `mcp_server._handle_get_objects` (lines 1291–1371). Key steps:
1. `camera_name = request.cam_name` — look up in the daemon's camera registry.
   On miss: `context.set_code(grpc.StatusCode.NOT_FOUND)` and return empty response.
2. Fetch `frame = pipeline.last_frame` (or equivalent accessor on the pipeline).
   If None: return `GetObjectsResponse(cam_name=camera_name, objects=[])`.
3. Retrieve homography and playfield polygon from the pipeline's calibration state.
4. Run `ColorClassifier(min_area=600, max_area=30000).classify(frame, homography=H)`.
5. Compute `shrunk_poly` (60 px inset) with the same numpy logic as in `_handle_get_objects`.
6. Filter objects; compute A1-shifted world_xy.
7. Build and return `GetObjectsResponse`.

Import at top of handler file: `from aprilcam.vision.color_classifier import ColorClassifier`
(cv2 is available in the daemon).

### Client method (`client/control.py`)

```python
def get_objects(self, cam_name: str):
    """Return GetObjectsResponse from the daemon."""
    from aprilcam.proto.aprilcam_pb2 import CameraRequest
    return self._stub.GetObjects(CameraRequest(cam_name=cam_name))
```

### Testing (`tests/test_015_001_get_objects_rpc.py`)

- Fixture: construct a fake camera pipeline object with `last_frame` set to a
  synthetic 640x480 BGR numpy array containing a colored rectangle of known size
  and position (patch with `np.zeros` + `cv2.rectangle`).
- Fixture: mock calibration state returning no homography (uncalibrated path).
- Test 1: handler returns at least one `ObjectRecord` with a non-empty `color`.
- Test 2: handler with an unknown `cam_name` sets `context.code` to `NOT_FOUND`.
- Test 3: handler with `last_frame=None` returns empty objects list.

### Files to modify/create

- `proto/aprilcam.proto` — add messages and RPC
- `src/aprilcam/proto/aprilcam_pb2.py` — regenerated (do not edit manually)
- `src/aprilcam/proto/aprilcam_pb2_grpc.py` — regenerated (do not edit manually)
- `src/aprilcam/daemon/grpc_server.py` — add `GetObjects` handler
- `src/aprilcam/client/control.py` — add `get_objects()` method
- `tests/test_015_001_get_objects_rpc.py` — new test file
