---
title: Daemon Wire Protocol
blurb: The gRPC control service and length-prefixed protobuf stream sockets the aprilcam daemon exposes — the wire-level contract beneath the Python client.
order: 50
updated: 2026-06-20
tags: [daemon, grpc, protobuf, protocol]
---

# AprilCam Daemon Wire Protocol

`aprilcamd` is a long-running background process that owns all cameras,
runs AprilTag/ArUco detection, and serves per-frame data to any number of
subscribers. The daemon exposes a **gRPC** control service plus on-demand
**length-prefixed protobuf** stream sockets.

This document describes the wire-level contract. If you are writing a
robot program in Python, use the [Robot Direct API](robot-direct-api.md)
client (`DaemonControl`) instead of speaking the protocol by hand — it
wraps everything below. This page is for understanding the protocol or
implementing a client in another language. The authoritative schema is
[`proto/aprilcam.proto`](https://github.com/League-Robotics/aprilcam/blob/master/proto/aprilcam.proto).

---

## Starting the daemon and file paths

The daemon auto-spawns when the first client connects, and can be managed with
`aprilcam daemon start|stop|status|restart`. For the full lifecycle, the
config/environment variables, the FHS/XDG directory layout, and systemd, see
**[Operating the Daemon](daemon.md)**.

Endpoints and on-disk files this protocol references (all under `Config`-derived
directories — see `src/aprilcam/config.py`):

| Path | Default |
|------|---------|
| gRPC control socket | `<socket_dir>/control.sock` |
| Pidfile / spawn lock | `<socket_dir>/aprilcamd.pid` · `<socket_dir>/aprilcamd.spawn.lock` |
| Per-camera directory | `<data_dir>/cameras/<cam_name>/` |
| Per-camera config (developer-owned, static) | `<data_dir>/cameras/<cam_name>/config.json` |
| Per-camera calibration (daemon-owned, regenerable) | `<data_dir>/cameras/<cam_name>/calibration.json` |
| Per-camera info / paths | `<data_dir>/cameras/<cam_name>/info.json` · `paths.json` |
| Camera registry (persistent enum) | `<data_dir>/cameras/registry.json` |
| Daemon log | `<log_dir>/aprilcamd.log` |

**Camera naming.** `<cam_name>` is a slug derived from the OS device name
(e.g. `arducam-ov9782-usb-camera`), *not* `cam_<index>`. `OpenCamera` takes an
OpenCV device `index` and returns the resolved `cam_name`. The stable,
user-facing camera handle is the **persistent enumeration number** stored in
`registry.json` and returned by `EnumerateCameras` as `CameraDevice.enum`
(see below) — the OS `index` changes across replug.

**config.json vs calibration.json.** Static, developer-owned fields
(`device_name`, `resolution`, UVC `settings`, `camera_position`,
`static_marker_ids`, linked `playfield`) live in `config.json`. `calibration.json`
is regenerable and holds only homography/geometry; it must never carry the
config-owned keys. The daemon overlays the config-owned fields at load time.

---

## Control Service (gRPC)

**Endpoint:** `unix:<socket_dir>/control.sock` (a TCP port is used as a
fallback when Unix sockets are unavailable).

**Service:** `aprilcam.AprilCam`. Generated stubs live in
`aprilcam.proto.aprilcam_pb2_grpc`. All RPCs are unary (single request,
single response); the streaming endpoints below return a *descriptor* of a
separate socket rather than a gRPC stream.

| RPC | Request | Response | Purpose |
|-----|---------|----------|---------|
| `ListCameras` | `Empty` | `ListCamerasResponse` | Names of currently-open cameras. |
| `EnumerateCameras` | `Empty` | `EnumerateCamerasResponse{cameras[]}` | Probe host hardware; each `CameraDevice` carries `index`, `name`, `slug`, and the persistent `enum`. |
| `OpenCamera` | `OpenCameraRequest{index}` | `OpenCameraResponse{cam_name, camera_dir}` | Open a camera by OpenCV index; idempotent if already open. |
| `CloseCamera` | `CameraRequest{cam_name}` | `Empty` | Stop the pipeline and release the camera. |
| `GetCameraConfig` / `SetCameraConfig` | `CameraRequest` / `CameraJsonRequest` | `JsonBlobReply` / `StatusReply` | Read/write `config.json` as an opaque JSON blob. |
| `GetCalibration` / `SetCalibration` | `CameraRequest` / `CameraJsonRequest` | `JsonBlobReply` / `StatusReply` | Read/write `calibration.json`; `SetCalibration` triggers a live pipeline reload. |
| `ReloadCalibration` | `CameraRequest{cam_name}` | `Empty` | Reload `calibration.json` from disk into the live pipeline. |
| `Shutdown` | `Empty` | `Empty` | Stop all pipelines and exit the daemon. |
| `GetCameraInfo` | `CameraRequest{cam_name}` | `CameraInfoResponse` | `cam_name`, `calibrated`, `frame_w/h`, `fps`. |
| `CaptureFrame` | `CameraRequest{cam_name}` | `CaptureFrameResponse{jpeg}` | Most recent raw frame as JPEG bytes. |
| `GetTags` | `CameraRequest{cam_name}` | `TagFrameResponse` | One-shot latest tag detections. |
| `GetObjects` | `CameraRequest{cam_name}` | `GetObjectsResponse{objects[]}` | Colored non-tag objects; `wx,wy` in the same A1-centred cm frame as tags. |
| `WhereIs` | `WhereRequest{query, cam_name}` | `WhereResponse` | Natural-language playfield feature lookup. |
| `GetImageStream` | `StreamRequest{cam_name, max_hz}` | `StreamEndpoint` | Allocate an image stream socket on demand. |
| `GetTagStream` | `StreamRequest{cam_name, max_hz}` | `StreamEndpoint` | Allocate a tag/overlay stream socket on demand. |
| `PublishOverlay` | `PublishOverlayRequest{cam_name, overlay}` | `StatusReply{ok, error}` | Inject overlay elements to all tag-stream subscribers. |

### CameraDevice (EnumerateCameras)

| Field | Type | Description |
|-------|------|-------------|
| `index` | int32 | OS probe index — **unstable**, changes on plug/unplug. Used only to open the device. |
| `name` | string | Human-readable device name. |
| `slug` | string | URL-safe identifier (= `cam_name` / per-camera dir). |
| `enum` | int32 | **Persistent enumeration number** — the stable, user-facing camera handle (`0` if unregistered). |

`OpenCamera` still takes the OS `index`; higher layers (the CLI and the MCP
`open_camera`) accept the `enum` and resolve it to the live `index` via
`EnumerateCameras`.

### TagMsg fields

`GetTags` and the tag stream both carry `TagMsg` records:

| Field | Type | Description |
|-------|------|-------------|
| `id` | int32 | AprilTag/ArUco ID. |
| `cx_px`, `cy_px` | float | Center pixel coordinate. |
| `corners_px` | repeated float (8) | Corner pixels `x0,y0,…,x3,y3` (UL, UR, LR, LL). |
| `yaw` | float | Orientation in radians. |
| `wx`, `wy` | float | World position in cm (`0` when uncalibrated). |
| `in_playfield` | bool | Center inside the playfield polygon. |
| `vx_px`, `vy_px`, `speed_px` | float | Pixel velocity components and speed. |
| `vx_world`, `vy_world`, `speed_world` | float | World velocity components and speed (cm/s). |
| `heading_rad` | float | Velocity heading in radians. |
| `age` | float | Seconds since last detected (`0` = this frame). |

`TagFrameResponse` wraps the tag list with `frame_id`, a row-major 3×3
`homography` (9 floats; empty when uncalibrated), `playfield_corners`
(8 floats), and `field_width_cm`/`field_height_cm`.

---

## Stream Sockets

`GetTagStream` and `GetImageStream` do not stream over gRPC. Each returns
a `StreamEndpoint` describing a freshly-allocated socket:

```
StreamEndpoint {
  string socket_path = 1;  // non-empty when using a Unix socket
  uint32 tcp_port    = 2;  // non-zero when using TCP (127.0.0.1)
}
```

Connect to that socket and read a continuous stream of **length-prefixed
protobuf** messages. The daemon fans each frame out to all connected
subscribers; slow subscribers have frames dropped silently (per-subscriber
send queue capped at 2) — you always get the latest available frame, never
a backlog.

### Wire framing

Both stream types use the same framing:

```
[4 bytes: big-endian uint32 length][<length> bytes: protobuf payload]
```

Read exactly 4 bytes for the length, then exactly that many bytes for the
payload, and parse with the appropriate protobuf message type.

### Tag stream payload — `StreamMessage`

The tag stream multiplexes tag data and overlays through a `oneof`:

```
StreamMessage {
  oneof payload {
    TagFrame     tag_frame = 1;   // detections (see TagMsg above)
    OverlayFrame overlay   = 2;   // overlays published by other processes
  }
}
```

`TagFrame` carries `frame_id`, `ts_mono_ns`, `ts_wall_ms`, the `tags`
list, `homography`, `playfield_corners`, `fps`, and field dimensions. It
is published on every detected change and at a 1-second heartbeat, rate-
limited to the `max_hz` the subscriber requested (default 20). A
consumer-only client can ignore `overlay` messages.

### Image stream payload — `ImageFrame`

```
ImageFrame {
  uint64 frame_id;    // matches the TagFrame captured at the same instant
  uint64 ts_mono_ns;
  bytes  jpeg;
  int32  width;
  int32  height;
}
```

---

## Live Overlays

Overlays are world-coordinate (cm) drawables pushed to every tag-stream
subscriber for a camera, via `PublishOverlay`. The viewer drops any
`OverlayFrame` whose `(now - timestamp) > ttl`, so a short TTL makes stale
data disappear automatically.

```
OverlayElement {
  string         type;       // "arc" | "arrow" | "point" | "polyline"
                             // | "text" | "rect" | "polygon"
  repeated float params;     // shape-specific, world cm (see proto comments)
  repeated int32 color;      // [R, G, B], 0-255
  int32          thickness;  // -1 = filled
  string         text;       // content for "text" elements
}

OverlayFrame {
  double                  timestamp;
  float                   ttl;
  repeated OverlayElement elements;
  string                  camera_id;
}
```

See the [Robot Direct API](robot-direct-api.md#live-overlay--push-annotations-at-5-50-hz)
for the Python `publish_overlay` helper and a control-loop example.

---

## info.json

Written atomically to `<data_dir>/cameras/<cam_name>/info.json` when a
camera is opened. It currently records the per-camera paths file location:

```json
{
  "paths_file": "/abs/path/data/aprilcam/cameras/arducam-ov9782-usb-camera/paths.json"
}
```

Calibration state, frame size, and fps are not stored here — query them
live with `GetCameraInfo`.

---

## paths.json

Written atomically by the MCP server (not the daemon) whenever paths are
created, deleted, or cleared. The live viewer polls this file's `mtime`
and reloads when it changes; the daemon never reads or writes it. The path
is announced in `info.json` (`paths_file`).

**Format:** JSON array of path objects, or `[]` when no paths are active.

```json
[
  {
    "path_id": "path_000",
    "playfield_id": "arducam-ov9782-usb-camera",
    "waypoints": [
      {
        "x": 20.0, "y": 15.0, "size_cm": 5.0,
        "symbol": "filled_circle",
        "symbol_color": [255, 0, 0],
        "line_color": [0, 200, 0]
      }
    ]
  }
]
```

Valid symbols: `square`, `filled_square`, `circle`, `filled_circle`,
`triangle`, `filled_triangle`, `x`, `none`. Colors are RGB `[0..255]`.
See the [Robot Direct API](robot-direct-api.md#persistent-paths--write-to-pathsjson)
for writing paths directly from a robot program.

---

## Configuration

All paths and tunables come from `Config` (`src/aprilcam/config.py`), resolved
from `APRILCAM_*` environment variables, `.env`/`.aprilcam` dotfiles, and
`/etc/aprilcam.env`, with an auto-selected FHS (root) or XDG (user) directory
layout. The full table of variables, the priority order, the directory layout,
and systemd deployment are documented in
**[Operating the Daemon → Configuration](daemon.md#configuration)**.

> A client and the daemon **must resolve the same `APRILCAM_SOCKET_DIR`** or
> they cannot find each other on the control socket.

---

## Known Limitations

- **Dead pipeline not auto-restarted.** If a camera is unplugged, its
  capture thread exits but the daemon still considers it registered;
  `OpenCamera` then returns the existing (dead) handle. Workaround:
  `CloseCamera` then `OpenCamera`, or restart the daemon.

- **Daemon does not exit when idle.** It runs until `Shutdown` or a kill
  signal, even with no cameras open and no subscribers connected.
