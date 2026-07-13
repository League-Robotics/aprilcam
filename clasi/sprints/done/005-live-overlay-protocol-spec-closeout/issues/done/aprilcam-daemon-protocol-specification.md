---
status: done
sprint: '005'
tickets:
- '001'
---

# AprilCam Daemon Protocol Specification

---

## Part 1 — Current State (As-Is)

### Overview

The daemon (`aprilcam.daemon`) runs as a background process and exposes two UNIX
domain socket endpoints:

| Socket | Path | Purpose |
|--------|------|---------|
| Control | `<socket_dir>/control.sock` | One-shot JSON RPC (shared, not per-camera) |
| Data | `<socket_dir>/<cam_name>/data.sock` | Streaming frames + tags (one per open camera) |

---

### 1.1 Control Socket

**Type:** `AF_UNIX SOCK_STREAM`  
**Model:** One request per connection. Client sends one JSON line, server sends one
JSON line, connection closes.  
**Framing:** Newline-delimited UTF-8 JSON  
**Timeout:** 10 s per connection

**Request wire format:**
```
{"cmd": "<name>", <fields>}\n
```

**Response wire format:**
```
{"ok": true, <fields>}\n
{"ok": false, "error": "<message>"}\n
```

**Commands:**

| cmd | Request fields | Response fields |
|-----|----------------|-----------------|
| `list_cameras` | — | `cameras: list[str]` |
| `open_camera` | `index: int` | `cam_name: str`, `info_json_path: str` |
| `close_camera` | `cam_name: str` | — |
| `reload_calibration` | `cam_name: str` | — |
| `get_camera_info` | `cam_name: str` | `info: dict` (contents of info.json) |
| `capture_frame` | `cam_name: str` | `frame_b64: str` (JPEG, base64) |
| `get_calibration_save_path` | — | `path: str` |
| `shutdown` | — | — |

Server: `src/aprilcam/daemon/server.py`  
Client: `src/aprilcam/daemon/client.py` — `ControlClient.rpc(cmd, **kwargs) → dict`

---

### 1.2 Data Socket (per camera)

**Type:** `AF_UNIX SOCK_STREAM`  
**Model:** Publish-subscribe. Multiple concurrent readers. Each subscriber gets a
queue (maxsize=2); frames are dropped silently if the subscriber lags.  
**Framing:** 4-byte big-endian `uint32` length prefix + msgpack payload

**Wire format:**
```
[ uint32 BE length (4 bytes) ][ msgpack bytes ]
```

**FrameMessage fields** (`src/aprilcam/daemon/protocol.py`):

| Field | Type | Description |
|-------|------|-------------|
| `schema` | `int` | Always `1` |
| `frame_id` | `int` | Monotonically increasing counter |
| `ts_mono_ns` | `int` | Monotonic timestamp, nanoseconds |
| `ts_wall_ms` | `int` | Wall-clock timestamp, milliseconds |
| `frame_jpeg` | `bytes` | JPEG-encoded frame |
| `frame_w`, `frame_h` | `int` | Frame dimensions |
| `tags` | `list[dict]` | Tag records (see below) |
| `homography` | `list[list[float]] \| None` | 3×3 perspective matrix; None if uncalibrated |
| `playfield_corners` | `list[list[float]]` | 4-point polygon `[[x,y],…]` |
| `paths_file` | `str` | Path to paths.json |
| `fps` | `float` | Rolling 30-frame FPS |

**Tag dict keys** (from `TagRecord.to_dict()`):
`id`, `center_px`, `corners_px`, `orientation_yaw`, `world_xy`, `in_playfield`,
`vel_px`, `speed_px`, `vel_world`, `speed_world`, `heading_rad`, `timestamp`,
`frame_index`, `age`

---

### 1.3 Problems with the Current Implementation

1. **Raw socket code in `view_cli.py`.** `socket.socket()`, `sock.connect()`, and
   `read_frame()` called directly at lines 186–188, 204, 291.
2. **`ControlClient.rpc()` is untyped.** Accepts `cmd: str, **kwargs`, returns `dict`.
   Typos fail silently; callers get unvalidated dicts.
3. **No schema for messages.** Request/response shapes exist only in comments.
4. **`FrameMessage` is a plain dataclass; tags are `list[dict]`.** No validation.
5. **Frame and tags bundled together.** A consumer that wants only tags must receive
   and discard every JPEG frame.
6. **No TCP option.** Clients on other machines cannot connect.

---

## Part 2 — Proposed Architecture (To-Be)

### 2.0 Guiding Principles

- **gRPC for all control/RPC operations.** One gRPC endpoint (Unix socket or TCP),
  replacing the JSON control socket.
- **Two separate binary streams per camera** — image stream and tag stream — so
  consumers can subscribe to either or both independently.
- **Streams are discovered via gRPC, not at well-known paths.** The daemon allocates
  stream endpoints on demand; clients call a gRPC method to get the socket path or
  TCP port.
- **Dual transport by default.** The daemon always binds both a Unix socket and a
  TCP port at startup. Either transport can be disabled via startup flags. This
  means any client — local or remote — can connect without reconfiguration, and
  TCP also serves as a discovery mechanism (mDNS advertises the TCP port).
- **Daemon-side producer classes / client-side consumer classes.** The only code
  that creates or reads a socket lives in the producer or consumer class for that
  socket type. Daemon code creates producers; client code creates consumers.
- **Protobuf on the wire, Pydantic in user code.** gRPC messages are defined in
  `.proto`; what application code touches (tag records, path objects, etc.) is Pydantic.
  A thin mapping layer converts between the two.

---

### 2.1 Endpoints

The daemon binds **both** transports at startup by default.

| Endpoint | Unix path (default) | TCP port (default) | Purpose |
|----------|--------------------|--------------------|---------|
| gRPC control | `/tmp/aprilcam/control.sock` | 5280 | All RPC calls |
| Image stream | `/tmp/aprilcam/<cam_name>/images-<uuid>.sock` | dynamic (returned by gRPC) | Per-camera JPEG stream |
| Tag stream | `/tmp/aprilcam/<cam_name>/tags-<uuid>.sock` | dynamic (returned by gRPC) | Per-camera tag/homography stream |

Stream endpoints are allocated on demand: the daemon creates the socket when a
client calls `GetImageStream` or `GetTagStream`, and returns the path/port in the
`StreamEndpoint` response.

---

### 2.1a Startup Transport Flags

| Flag | Default | Effect |
|------|---------|--------|
| `--unix` / `--no-unix` | `--unix` | Enable/disable the Unix socket transport |
| `--tcp` / `--no-tcp` | `--tcp` | Enable/disable the TCP transport |
| `--tcp-port N` | `5280` | TCP port for the gRPC control endpoint |
| `--unix-path PATH` | `/tmp/aprilcam/control.sock` | Path for the Unix socket control endpoint |

**Valid combinations:**

| `--unix` | `--tcp` | Result |
|----------|---------|--------|
| on | on | Both transports active (default) |
| on | off | Unix only — local clients only, no mDNS advertisement |
| off | on | TCP only — useful for remote access or containerized setups |
| off | off | Invalid — daemon exits with an error |

Stream sockets inherit the active transport(s): if both are enabled the daemon
creates both a Unix socket and a TCP socket for each stream, and returns both
in `StreamEndpoint`. The client chooses whichever it prefers.

---

### 2.2 gRPC Service Definition (`aprilcam.proto`)

```protobuf
service AprilCam {
  // Camera lifecycle
  rpc ListCameras (Empty)             returns (ListCamerasResponse);
  rpc OpenCamera  (OpenCameraRequest) returns (OpenCameraResponse);
  rpc CloseCamera (CameraRequest)     returns (Empty);
  rpc ReloadCalibration (CameraRequest) returns (Empty);
  rpc Shutdown    (Empty)             returns (Empty);

  // One-shot queries (no streaming required)
  rpc GetCameraInfo  (CameraRequest)       returns (CameraInfoResponse);
  rpc CaptureFrame   (CameraRequest)       returns (CaptureFrameResponse);
  rpc GetTags        (CameraRequest)       returns (TagFrameResponse);

  // Stream discovery
  rpc GetImageStream (StreamRequest)  returns (StreamEndpoint);
  rpc GetTagStream   (StreamRequest)  returns (StreamEndpoint);
}

message StreamRequest {
  string cam_name = 1;
  uint32 max_hz   = 2;  // 0 = use default (20); client asks for its preferred cap
}

message StreamEndpoint {
  string socket_path = 1;  // non-empty when using Unix sockets
  uint32 tcp_port    = 2;  // non-zero when using TCP
}

message OpenCameraRequest { int32 index = 1; }
message OpenCameraResponse { string cam_name = 1; }
message CameraRequest      { string cam_name = 1; }
message CaptureFrameResponse { bytes jpeg = 1; }

message CameraInfoResponse {
  string cam_name    = 1;
  bool   calibrated  = 2;
  int32  frame_w     = 3;
  int32  frame_h     = 4;
  float  fps         = 5;
}

message TagFrameResponse {
  // Same as TagFrame below, used for one-shot GetTags
  uint64          frame_id          = 1;
  repeated TagMsg tags              = 2;
  repeated float  homography        = 3;  // 9 floats, row-major; empty = uncalibrated
  repeated float  playfield_corners = 4;  // 8 floats: x0,y0,x1,y1,x2,y2,x3,y3
}

message ListCamerasResponse { repeated string cameras = 1; }
message Empty {}
```

*(Full `.proto` to be completed at implementation time; sketched above for types.)*

---

### 2.3 Tag Stream Protocol

**Stream message framing:** 4-byte big-endian `uint32` length prefix + protobuf payload
(same framing as current msgpack stream; format changes from msgpack to protobuf).

**Tag stream message (`TagFrame`):**

| Field | Type | Description |
|-------|------|-------------|
| `frame_id` | `uint64` | Links to the image frame captured at the same instant |
| `ts_mono_ns` | `uint64` | Monotonic timestamp, nanoseconds |
| `ts_wall_ms` | `uint64` | Wall-clock timestamp, milliseconds |
| `tags` | `repeated TagMsg` | All currently visible tags |
| `homography` | `repeated float` (9) | 3×3 matrix, row-major; empty if uncalibrated |
| `playfield_corners` | `repeated float` (8) | UL/UR/LR/LL as x0,y0,… |
| `fps` | `float` | Rolling camera FPS |

**Tag message (`TagMsg`):**

| Field | Type | Description |
|-------|------|-------------|
| `id` | `int32` | Tag ID |
| `cx_px`, `cy_px` | `float` | Pixel center |
| `corners_px` | `repeated float` (8) | 4 corners, x0,y0,… |
| `yaw` | `float` | Orientation, radians |
| `wx`, `wy` | `float` | World coords (cm); 0,0 = uncalibrated |
| `in_playfield` | `bool` | Within playfield polygon |
| `vx_px`, `vy_px` | `float` | Pixel velocity |
| `speed_px` | `float` | Pixel speed |
| `vx_world`, `vy_world` | `float` | World velocity |
| `speed_world` | `float` | World speed |
| `heading_rad` | `float` | Heading |
| `age` | `float` | Seconds since last detected (0 = this frame) |

**Adaptive publish rate:**

The daemon maintains the previous published tag set. On every camera frame it
computes whether a "significant change" has occurred:
- Any tag enters or leaves the scene, **or**
- Any visible tag has moved more than `change_threshold_px` (default **8 px**) from
  its last-published position.

| Condition | Publish rate |
|-----------|-------------|
| No change | Heartbeat once per second |
| Change detected | Publish immediately; rate-limited to `max_hz` (default 20, client-configurable) |

The daemon records `last_publish_ts` per stream. A change triggers a publish only if
`now - last_publish_ts ≥ 1.0 / max_hz`. This prevents bursts faster than the agreed
cap. If `max_hz = 0`, the daemon publishes every frame.

---

### 2.4 Image Stream Protocol

**Image stream message (`ImageFrame`):**

| Field | Type | Description |
|-------|------|-------------|
| `frame_id` | `uint64` | Serial number; matches the `frame_id` in the simultaneous `TagFrame` |
| `ts_mono_ns` | `uint64` | Monotonic timestamp |
| `jpeg` | `bytes` | JPEG-encoded frame |
| `width`, `height` | `int32` | Frame dimensions |

The image and tag frames produced from the same camera read share a `frame_id`. A
client that receives both streams can join them on this field. A client that only
subscribes to the tag stream can safely ignore images.

---

### 2.5 Daemon-Side Producer Classes (`aprilcam.daemon.stream`)

```
ImageStreamProducer(cam_name, config)
    .start() → StreamEndpoint
    .publish(frame_id, ts_mono_ns, ts_wall_ms, jpeg, w, h)
    .stop()

TagStreamProducer(cam_name, config, max_hz, change_threshold_px)
    .start() → StreamEndpoint
    .publish_if_changed(tag_frame: TagFrame)  ← decides rate internally
    .force_publish(tag_frame: TagFrame)        ← heartbeat path
    .stop()
```

Each producer owns its server socket (Unix or TCP). No other daemon code touches the
socket. `start()` creates the socket and returns the endpoint; the endpoint is then
stored and returned by the `GetImageStream` / `GetTagStream` gRPC calls.

---

### 2.6 Client-Side Consumer Classes (`aprilcam.client.stream`)

```
ImageStreamConsumer(endpoint: StreamEndpoint)
    .connect()
    .read() → np.ndarray          # decodes JPEG → BGR array
    .read_raw() → (frame_id, jpeg_bytes)
    .close()
    __iter__ → Iterator[np.ndarray]

TagStreamConsumer(endpoint: StreamEndpoint)
    .connect()
    .read() → TagFrame            # Pydantic model
    .close()
    __iter__ → Iterator[TagFrame]
```

Neither consumer ever calls `socket.socket()` directly in user code — that lives
inside the consumer's `connect()`. The `endpoint` arg comes from a gRPC
`GetImageStream` / `GetTagStream` call; the consumer doesn't need to know whether it's
a Unix socket or TCP.

---

### 2.7 gRPC Client Wrapper (`aprilcam.client.control`)

```
DaemonControl(endpoint: str | int)   # Unix socket path, or TCP port
    .connect() → self
    .close()

    # Camera management
    .list_cameras() → list[str]
    .open_camera(index: int) → str               # returns cam_name
    .close_camera(cam_name: str)
    .reload_calibration(cam_name: str)

    # One-shot queries
    .get_camera_info(cam_name: str) → CameraInfo
    .capture_frame(cam_name: str) → np.ndarray   # JPEG decoded
    .get_tags(cam_name: str) → TagFrame

    # Stream discovery
    .get_image_stream(cam_name: str, max_hz: int = 0) → ImageStreamConsumer
    .get_tag_stream(cam_name: str, max_hz: int = 20) → TagStreamConsumer

    .shutdown()

    @classmethod
    .connect_default(config: Config) → "DaemonControl"
```

`get_image_stream` and `get_tag_stream` call the corresponding gRPC methods,
receive the `StreamEndpoint`, construct and connect the consumer, and return it
ready to iterate. The caller sees only typed Python objects.

---

### 2.8 Pydantic Domain Models (`aprilcam.client.models`)

Used by application code after deserialization. Not sent on the wire.

```python
class TagRecord(BaseModel):
    id: int
    center_px: tuple[float, float]
    corners_px: list[tuple[float, float]]   # 4 points
    yaw: float
    world_xy: tuple[float, float] | None    # None if uncalibrated
    in_playfield: bool
    vel_px: tuple[float, float] | None
    speed_px: float | None
    vel_world: tuple[float, float] | None
    speed_world: float | None
    heading_rad: float | None
    age: float

class TagFrame(BaseModel):
    frame_id: int
    ts_mono_ns: int
    ts_wall_ms: int
    tags: list[TagRecord]
    homography: list[list[float]] | None    # 3x3; None if uncalibrated
    playfield_corners: list[tuple[float, float]]  # 4 points
    fps: float

class CameraInfo(BaseModel):
    cam_name: str
    calibrated: bool
    frame_size: tuple[int, int]
    fps: float

class PathRecord(BaseModel):
    points: list[tuple[float, float]]   # world coords (cm)
    color: tuple[int, int, int]         # BGR
    thickness: int
    closed: bool
```

Protobuf ↔ Pydantic conversion happens in a thin adapter inside the consumer and
control classes. Application code never touches proto-generated types.

---

### 2.9 Files To Create / Change

| File | Change |
|------|--------|
| `proto/aprilcam.proto` | **New** — gRPC service + all message definitions |
| `src/aprilcam/daemon/grpc_server.py` | **New** — gRPC servicer replacing `server.py`'s JSON control loop |
| `src/aprilcam/daemon/stream.py` | **New** — `ImageStreamProducer`, `TagStreamProducer` |
| `src/aprilcam/client/__init__.py` | **New** — `aprilcam.client` package |
| `src/aprilcam/client/control.py` | **New** — `DaemonControl` |
| `src/aprilcam/client/stream.py` | **New** — `ImageStreamConsumer`, `TagStreamConsumer` |
| `src/aprilcam/client/models.py` | **New** — Pydantic domain models |
| `src/aprilcam/daemon/server.py` | **Update** — replace JSON control loop with gRPC server; keep camera/pipeline management |
| `src/aprilcam/daemon/camera_pipeline.py` | **Update** — call producers instead of bundling frame+tags into one message |
| `src/aprilcam/daemon/protocol.py` | **Keep/update** — framing helpers reused for stream sockets |
| `src/aprilcam/daemon/client.py` | **Remove** — replaced by `aprilcam.client.control` |
| `src/aprilcam/cli/view_cli.py` | **Update** — use `DaemonControl` + consumers |
| `src/aprilcam/cli/daemon_cli.py` | **Update** — use `DaemonControl` |
| `pyproject.toml` | Add `grpcio`, `grpcio-tools`, `grpcio-reflection`, `protobuf` dependencies |

---

### 2.10 Service Discovery / Interface Reflection

**Schema discovery: gRPC Server Reflection.**

The daemon enables the standard gRPC reflection service at startup:

```python
from grpc_reflection.v1alpha import reflection
reflection.enable_server_reflection(
    [aprilcam_pb2.DESCRIPTOR.services_by_name["AprilCam"].full_name,
     reflection.SERVICE_NAME],
    grpc_server,
)
```

This lets any client query the live server for its full service schema — methods,
message types, field names and types — without distributing `.proto` files separately.

**What this enables:**

- `grpcurl -plaintext localhost:5280 list` — enumerate services
- `grpcurl -plaintext localhost:5280 describe aprilcam.AprilCam` — dump the schema
- `grpcui` — browser UI that auto-generates a form for every method
- Programmatic introspection in client code to validate version compatibility at
  connect time

**Endpoint discovery (how clients find the daemon):**

| Scenario | Mechanism |
|----------|-----------|
| Same machine | Well-known Unix socket path from config (`.aprilcam` / `~/.aprilcam`) |
| Same LAN, TCP mode | mDNS/Bonjour: daemon registers `_aprilcam._tcp.local.` at startup; clients browse with `zeroconf` |
| Explicit override | `APRILCAM_HOST` + `APRILCAM_PORT` env vars, or config file values |

`DaemonControl.connect_default(config)` tries these in order: env vars → config file →
Unix socket → mDNS browse.

The daemon only registers an mDNS record when bound to TCP; Unix-socket-only mode
stays invisible on the network.

**Additional dependency:** `grpcio-reflection`, `zeroconf`

---

### 2.11 Open Questions (Resolved from Conversation)

| Question | Decision |
|----------|----------|
| Auto-start daemon? | Yes — `DaemonControl.connect_default()` calls `ensure_running()` |
| Push or pull for streams? | Pull iterator (synchronous); caller drives the loop |
| Image frame format to user? | `np.ndarray` (BGR, decoded from JPEG inside consumer) |
| Streaming wire format | Length-prefixed protobuf (consistent with gRPC service) |
| Tag change threshold | 8 px default; configurable per `GetTagStream` call |
| Heartbeat interval | 1 second regardless of change activity |
