---
status: done
sprint: '005'
tickets:
- '002'
- '003'
- '004'
- '005'
- '006'
- '007'
- 008
- 009
---

# Plan: Live Overlay via Tag Stream Socket

## Context

The robot needs to display its decision-making state (pure pursuit arc, lookahead point, heading arrow, intended path) updated 5-10 Hz in the live view. The view_cli already receives tag positions via a tag stream socket (Unix socket, length-prefixed protobuf). The right approach is to multiplex overlay messages on that same socket by adding a message type to the framing — any sender can push a new message type, the view switches on it and draws it.

**Architecture**: MCP `set_live_overlay` → gRPC `PublishOverlay` RPC → daemon `TagStreamProducer.publish_overlay()` → socket → view_cli tag reader thread → `display.draw_live_overlay()`.

## Wire protocol change

**Current framing**: `[4-byte length][TagFrame protobuf bytes]`

**New framing**: `[4-byte length][StreamMessage protobuf bytes]`

where `StreamMessage` is a new oneof wrapper. All existing senders/receivers are updated simultaneously (we control all the code).

## Files to create/modify

### 1. MODIFY: `proto/aprilcam.proto`

Add overlay element and frame messages, plus a `StreamMessage` wrapper:

```protobuf
message OverlayElement {
  string type      = 1;  // "arc", "arrow", "point", "polyline"
  repeated float params = 2;  // type-specific: see below
  repeated int32 color = 3;   // [R, G, B]
  int32 thickness  = 4;
  // arc:     params = [cx, cy, radius, start_deg, end_deg]
  // arrow:   params = [x1, y1, x2, y2]
  // point:   params = [x, y, radius_cm]
  // polyline: params = [x0,y0, x1,y1, ...]
}

message OverlayFrame {
  double timestamp          = 1;
  float  ttl                = 2;  // seconds; view drops if expired
  repeated OverlayElement elements = 3;
  string camera_id          = 4;
}

message StreamMessage {
  oneof payload {
    TagFrame     tag_frame = 1;
    OverlayFrame overlay   = 2;
  }
}
```

Coordinates in all elements are world cm. Colors are RGB [0-255].

### 2. MODIFY: `src/aprilcam/daemon/stream.py`

- Change `_frame_bytes(proto_msg)` to wrap in `StreamMessage`:
  - If given a `TagFrame`: wrap as `StreamMessage(tag_frame=msg)`
  - If given an `OverlayFrame`: wrap as `StreamMessage(overlay=msg)`
- Add `TagStreamProducer.publish_overlay(overlay_frame: OverlayFrame)` method:
  - Bypasses rate-limiting and change detection (overlays always published immediately)
  - Calls `self._publish_bytes(_frame_bytes(overlay_frame))`

### 3. MODIFY: `src/aprilcam/client/stream.py`

- Change `TagStreamConsumer.read()` to decode `StreamMessage` instead of bare `TagFrame`:
  ```python
  msg = aprilcam_pb2.StreamMessage()
  msg.ParseFromString(data)
  if msg.HasField("tag_frame"):
      return TagFrame.from_proto(msg.tag_frame)
  elif msg.HasField("overlay"):
      return msg.overlay   # return OverlayFrame proto directly
  ```
- Return type becomes `TagFrame | OverlayFrame` (or a discriminated union)
- Update `TagFrame.from_proto()` if needed

### 4. MODIFY: `proto/aprilcam.proto` service + `src/aprilcam/daemon/grpc_server.py`

Add new RPC to the service:
```protobuf
rpc PublishOverlay (PublishOverlayRequest) returns (StatusReply);

message PublishOverlayRequest {
  string cam_name     = 1;
  OverlayFrame overlay = 2;
}
```

In `AprilCamServicer.PublishOverlay()`:
- Look up the `TagStreamProducer` for `cam_name`
- Call `producer.publish_overlay(request.overlay)`
- Return `StatusReply(ok=True)`

### 5. MODIFY: `src/aprilcam/client/control.py`

Add `DaemonControl.publish_overlay(cam_name, elements, ttl=1.0)`:
- Builds `OverlayFrame` protobuf from element list
- Calls `stub.PublishOverlay(PublishOverlayRequest(...))`
- Returns bool success

### 6. MODIFY: `src/aprilcam/server/mcp_server.py`

New MCP tool: `set_live_overlay(camera_id, elements_json, ttl=1.0)`
- Parses `elements_json` (list of element dicts)
- Calls `daemon_client.publish_overlay(camera_id, elements, ttl)`
- Docstring: "Any process with DaemonControl access can also call publish_overlay() directly"
- Workflow: `open_camera → set_live_overlay` (no playfield needed)

New MCP tool: `clear_live_overlay(camera_id)`
- Calls `publish_overlay` with empty elements and `ttl=0`

### 7. MODIFY: `src/aprilcam/ui/display.py`

Add `draw_live_overlay(frame, overlay_frame, homography)`:
- No-op if `homography is None`
- Drops expired overlays: `time.time() - overlay.timestamp > overlay.ttl`
- Extract private `_world_to_disp_with_hinv(x, y, H_inv)` instance method (shared with `draw_paths` logic)
- Per element, dispatch on `type`:
  - **arc**: map center + radius points → `cv.ellipse()` with correct rx/ry/rotation (handles non-square scaling)
  - **arrow**: map tail + head → `cv.arrowedLine(tipLength=0.2)`
  - **point**: map center + radius → `cv.circle(FILLED)`
  - **polyline**: map all points → `cv.polylines(isClosed=False)`
  - Unknown types: silently skip
- Each element wrapped in try/except (matches `draw_paths` defensive style)

**Arc coordinate pipeline** (handles non-square homographies):
```
world(cx,cy) → H_inv → _map_to_display → disp_center
world(cx+r,cy) → same → disp_x;  rx = ||disp_x - disp_center||,  angle = atan2(rx_vec.y, rx_vec.x)
world(cx,cy+r) → same → disp_y;  ry = ||disp_y - disp_center||
cv.ellipse(center, axes=(rx,ry), angle=ellipse_angle, startAngle=start_deg, endAngle=end_deg)
```

### 8. MODIFY: `src/aprilcam/cli/view_cli.py`

- `_tag_reader_thread`: already calls `tag_consumer.read()` in a loop — handle both return types:
  ```python
  msg = tag_consumer.read()
  if isinstance(msg, TagFrame):
      with _tag_lock: _latest_tag_frame[0] = msg
  else:  # OverlayFrame
      with _overlay_lock: _latest_overlay[0] = msg
  ```
- Add `_latest_overlay: list = [None]` and `_overlay_lock`
- In `_process_frame_and_tags`, after `draw_paths()`:
  ```python
  with _overlay_lock:
      overlay = _latest_overlay[0]
  if overlay is not None:
      display.draw_live_overlay(disp, overlay, homography)
  ```
- **Remove** the file-based `_load_paths` call from the render loop — replace with the existing mtime-cached read (paths.json stays file-based; only live overlay moves to socket)

Note: `paths.json` persistent paths stay file-based (no change needed — they update infrequently).

## Robot direct-write API

```python
from aprilcam.client.control import DaemonControl
from aprilcam.config import Config

dc = DaemonControl.connect_default(Config.load())
# At ~10 Hz:
dc.publish_overlay("arducam-ov9782-usb-camera", [
    {"type": "arc",   "params": [robot_x, robot_y, lookahead_r, -90, 90], "color": [0,255,0], "thickness": 2},
    {"type": "point", "params": [target_x, target_y, 2.0],                "color": [255,0,0], "thickness": -1},
    {"type": "arrow", "params": [robot_x, robot_y, heading_x, heading_y], "color": [255,200,0], "thickness": 2},
], ttl=0.5)
```

## Performance

| | File-based (old plan) | Socket-based (this plan) |
|---|---|---|
| Latency write→display | 1-38ms | <5ms |
| File I/O per frame | 1 stat() call | None |
| Push vs poll | Poll (33ms window) | Push (immediate) |
| Multi-subscriber | File read per subscriber | Broadcast via existing fan-out |

## Implementation order

1. `proto/aprilcam.proto` — add messages + RPC (no code changes yet)
2. Regenerate protobuf Python bindings (`make proto` or equivalent)
3. `stream.py` — wrap in `StreamMessage`, add `publish_overlay()`
4. `client/stream.py` — decode `StreamMessage`, return union type
5. `grpc_server.py` + `control.py` — `PublishOverlay` RPC
6. `display.py` — `draw_live_overlay()` + `_world_to_disp_with_hinv()`
7. `view_cli.py` — handle overlay messages in tag reader thread
8. `mcp_server.py` — `set_live_overlay` + `clear_live_overlay` tools

## Verification

1. `uv run python -c "from aprilcam.server import mcp_server; print('ok')`
2. `uv run pytest tests/` — no regressions
3. Call `set_live_overlay` via MCP with an arc + arrow, confirm it appears in `aprilcam view 4` within one frame
4. Stop calling; confirm overlay disappears after TTL expires (~0.5s)
5. Write a 10 Hz test loop calling `dc.publish_overlay()` directly; confirm smooth real-time tracking in the view
