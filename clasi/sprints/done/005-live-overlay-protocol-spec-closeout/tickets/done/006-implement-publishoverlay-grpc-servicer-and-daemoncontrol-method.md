---
id: '006'
title: Implement PublishOverlay gRPC servicer and DaemonControl method
status: done
use-cases:
  - SUC-002
  - SUC-003
depends-on:
  - '005'
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement PublishOverlay gRPC servicer and DaemonControl method

## Description

Wire up the `PublishOverlay` RPC on both the daemon and client sides:

1. `AprilCamServicer.PublishOverlay()` in `daemon/grpc_server.py` — receives the
   gRPC request, looks up the `TagStreamProducer` for the named camera, and calls
   `producer.publish_overlay(request.overlay)`.
2. `DaemonControl.publish_overlay()` in `client/control.py` — builds a
   `PublishOverlayRequest` from a Python element list, calls the gRPC stub, and
   returns bool success.

After this ticket, any code with a `DaemonControl` instance can push an overlay
to the live view.

## Acceptance Criteria

- [x] `AprilCamServicer` in `daemon/grpc_server.py` has a `PublishOverlay` method
      that looks up `TagStreamProducer` by `cam_name` and calls
      `producer.publish_overlay(request.overlay)`.
- [x] Returns `StatusReply(ok=True)` on success; `StatusReply(ok=False)` with
      error context if the camera is not found.
- [x] `DaemonControl.publish_overlay(cam_name, elements, ttl=1.0)` exists in
      `client/control.py`.
- [x] `publish_overlay` builds `OverlayFrame` from `elements` (list of dicts with
      keys: `type`, `params`, `color`, `thickness`) and calls
      `stub.PublishOverlay(PublishOverlayRequest(...))`.
- [x] Returns `True` if `StatusReply.ok`, `False` otherwise.
- [x] Import smokes pass:
      `uv run python -c "from aprilcam.daemon.grpc_server import AprilCamServicer; print('ok')"`
      `uv run python -c "from aprilcam.client.control import DaemonControl; print('ok')"`
- [x] `uv run pytest tests/` passes.

## Implementation Plan

### Approach

**daemon/grpc_server.py**:
1. Read the file to find where stream producers are stored (likely a dict keyed
   by `cam_name` on the servicer or server instance).
2. Add `PublishOverlay(self, request, context)` method:
   ```python
   def PublishOverlay(self, request, context):
       producer = self._tag_producers.get(request.cam_name)
       if producer is None:
           context.set_code(grpc.StatusCode.NOT_FOUND)
           return aprilcam_pb2.StatusReply(ok=False)
       producer.publish_overlay(request.overlay)
       return aprilcam_pb2.StatusReply(ok=True)
   ```

**client/control.py**:
1. Read the file to understand how other methods build proto requests.
2. Add `publish_overlay(cam_name, elements, ttl=1.0)`:
   ```python
   def publish_overlay(self, cam_name: str, elements: list[dict], ttl: float = 1.0) -> bool:
       import time
       overlay_elements = [
           aprilcam_pb2.OverlayElement(
               type=e["type"],
               params=e.get("params", []),
               color=e.get("color", [255, 255, 255]),
               thickness=e.get("thickness", 2),
           )
           for e in elements
       ]
       overlay = aprilcam_pb2.OverlayFrame(
           timestamp=time.time(),
           ttl=ttl,
           elements=overlay_elements,
           camera_id=cam_name,
       )
       reply = self._stub.PublishOverlay(
           aprilcam_pb2.PublishOverlayRequest(cam_name=cam_name, overlay=overlay)
       )
       return reply.ok
   ```

### Files to Modify

- `src/aprilcam/daemon/grpc_server.py`
- `src/aprilcam/client/control.py`

### Testing Plan

- Import smokes (see acceptance criteria).
- `uv run pytest tests/`
- End-to-end verification in ticket 008 (manual, requires live daemon).

### Documentation Updates

None — `DaemonControl.publish_overlay()` docstring should note that any process
with DaemonControl access can call it directly (not only via MCP).
