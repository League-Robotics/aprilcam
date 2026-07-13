---
id: '007'
title: Implement DaemonControl, ImageStreamConsumer, TagStreamConsumer (client/)
status: done
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
  - SUC-005
depends-on:
  - '003'
  - '006'
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement DaemonControl, ImageStreamConsumer, TagStreamConsumer (client/)

## Description

Create the complete client-side library:

- `src/aprilcam/client/control.py` — `DaemonControl`: typed gRPC stub wrapper.
  Returns Pydantic models, not raw proto objects or dicts. `get_image_stream()` and
  `get_tag_stream()` call the respective gRPC methods, construct and connect the
  consumer, and return it ready to iterate.

- `src/aprilcam/client/stream.py` — `ImageStreamConsumer` and `TagStreamConsumer`.
  Each consumer owns its client socket (Unix or TCP, chosen from `StreamEndpoint`),
  reads length-prefixed protobuf messages, and converts them to Pydantic models.
  Both expose `read()`, `close()`, and `__iter__`.

This ticket delivers the full typed client API that `daemon_cli` and `view_cli`
will use in ticket 008.

## Acceptance Criteria

- [ ] `DaemonControl(endpoint)` accepts a Unix socket path string or a TCP port int.
- [ ] `DaemonControl.connect()` establishes the gRPC channel; `close()` shuts it down.
- [ ] `DaemonControl.list_cameras()` returns `list[str]`.
- [ ] `DaemonControl.open_camera(index)` returns `str` (cam_name).
- [ ] `DaemonControl.close_camera(cam_name)` succeeds without exception.
- [ ] `DaemonControl.get_camera_info(cam_name)` returns a `CameraInfo` Pydantic model.
- [ ] `DaemonControl.capture_frame(cam_name)` returns a decoded `np.ndarray` (BGR).
- [ ] `DaemonControl.get_tags(cam_name)` returns a `TagFrame` Pydantic model.
- [ ] `DaemonControl.get_image_stream(cam_name, max_hz=0)` returns a connected
      `ImageStreamConsumer`.
- [ ] `DaemonControl.get_tag_stream(cam_name, max_hz=20)` returns a connected
      `TagStreamConsumer`.
- [ ] `DaemonControl.shutdown()` sends the Shutdown RPC.
- [ ] `DaemonControl` is a context manager: `with DaemonControl(...) as dc:`.
- [ ] `ImageStreamConsumer.read()` returns `np.ndarray`; `read_raw()` returns
      `(frame_id: int, jpeg: bytes)`.
- [ ] `ImageStreamConsumer` is iterable: `for frame in consumer`.
- [ ] `TagStreamConsumer.read()` returns a `TagFrame` Pydantic model.
- [ ] `TagStreamConsumer` is iterable: `for frame in consumer`.
- [ ] Both consumers accept a `StreamEndpoint` and prefer Unix socket if available,
      falling back to TCP.
- [ ] `uv run pytest` passes.

## Implementation Plan

### Approach

#### `DaemonControl` (control.py)

1. Constructor: store endpoint. `connect()` creates a `grpc.insecure_channel`
   (with `unix:` prefix for socket path, or `localhost:N` for TCP port).
2. Create stub: `aprilcam_pb2_grpc.AprilCamStub(channel)`.
3. Each method calls the corresponding stub method, checks for gRPC errors, and
   converts proto response to Pydantic model via a private `_to_*` converter.
4. `capture_frame()`: call `CaptureFrame` RPC, then
   `cv2.imdecode(np.frombuffer(resp.jpeg, np.uint8), cv2.IMREAD_COLOR)`.
5. `get_image_stream()`: call `GetImageStream` RPC, receive `StreamEndpoint`,
   construct `ImageStreamConsumer(endpoint)`, call `consumer.connect()`, return.
6. `get_tag_stream()`: same pattern for `TagStreamConsumer`.
7. `connect_default(config)`: try Unix socket path from config first, then
   `APRILCAM_HOST`/`APRILCAM_PORT` env vars, then TCP port 5280.
   Call `ensure_running(config)` to spawn daemon if not running.

#### `ImageStreamConsumer` (stream.py)

1. Constructor: store `StreamEndpoint`.
2. `connect()`: prefer `endpoint.socket_path` (Unix), fall back to `endpoint.tcp_port`
   (TCP). Create `socket.socket`, `sock.connect(...)`. Store connected socket.
3. `read_raw()`: read 4-byte length prefix, read that many bytes, return
   `(frame.frame_id, frame.jpeg)` after parsing `ImageFrame` protobuf.
4. `read()`: call `read_raw()`, decode JPEG via numpy/cv2.
5. `__iter__`: loop calling `read()` until connection closes.
6. `close()`: close socket.

#### `TagStreamConsumer` (stream.py)

Same structure as `ImageStreamConsumer` but parses `TagFrame` protobuf and converts
to Pydantic `TagFrame` via a `_proto_tag_frame_to_pydantic()` adapter function.

### Files to Create / Modify

- `src/aprilcam/client/control.py` — new
- `src/aprilcam/client/stream.py` — new

### Testing Plan

- `tests/test_daemon_control.py` (integration, requires running daemon):
  - Start daemon in a subprocess (Unix-only, non-default socket path to avoid
    conflicts); construct `DaemonControl`; call `list_cameras()`; verify empty list.
  - Verify `DaemonControl` as context manager cleans up without error.
- `tests/test_stream_consumers.py` (unit, mocked sockets):
  - Feed a pre-built `ImageFrame` protobuf (with length prefix) into a mock socket.
  - Call `ImageStreamConsumer.read_raw()`; verify frame_id and jpeg bytes match.
  - Feed a `TagFrame` protobuf into a mock socket.
  - Call `TagStreamConsumer.read()`; verify `TagFrame.fps` field present.
- Run `uv run pytest`.
