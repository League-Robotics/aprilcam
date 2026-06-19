---
title: Daemon Wire Protocol
blurb: The gRPC control service and length-prefixed protobuf stream sockets the aprilcam daemon exposes — the wire-level contract beneath the Python client.
order: 30
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

## Starting the Daemon

The daemon auto-spawns when any client (`DaemonControl.connect_default`,
the viewer, or the MCP server) first connects: the client acquires a spawn
lock at `<socket_dir>/aprilcamd.spawn.lock`, launches
`python -m aprilcam.daemon` as a detached background process, then probes
the gRPC channel until it answers.

You can also start it explicitly:

```
aprilcam daemon start          # start in the background
aprilcam daemon status         # running/stopped, open cameras
aprilcam daemon stop
aprilcam daemon restart
python -m aprilcam.daemon       # run in the foreground
```

The daemon will not start a second instance — it holds an exclusive
`flock` on its pidfile. Log output goes to `<log_dir>/aprilcamd.log`.

---

## File Paths

All paths derive from `Config` (see `src/aprilcam/config.py`). Defaults:

| Path | Default |
|------|---------|
| gRPC control socket | `<socket_dir>/control.sock` |
| Pidfile | `<socket_dir>/aprilcamd.pid` |
| Spawn lock | `<socket_dir>/aprilcamd.spawn.lock` |
| Per-camera directory | `<data_dir>/cameras/<cam_name>/` |
| Per-camera calibration | `<data_dir>/cameras/<cam_name>/calibration.json` |
| Per-camera info file | `<data_dir>/cameras/<cam_name>/info.json` |
| Per-camera paths file | `<data_dir>/cameras/<cam_name>/paths.json` |
| Daemon log | `<log_dir>/aprilcamd.log` |

`<socket_dir>`, `<data_dir>`, and `<log_dir>` are auto-selected by
`APRILCAM_SYSTEM` (see the Configuration section below). Override individual
paths with `APRILCAM_SOCKET_DIR`, `APRILCAM_DATA_DIR`, and `APRILCAM_LOG_DIR`.

**Camera naming.** `<cam_name>` is a slug derived from the OS device name
(e.g. `arducam-ov9782-usb-camera`), *not* `cam_<index>`. `OpenCamera`
takes an OpenCV device `index` and returns the resolved `cam_name`.

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
| `OpenCamera` | `OpenCameraRequest{index}` | `OpenCameraResponse{cam_name, camera_dir}` | Open a camera by OpenCV index; idempotent if already open. |
| `CloseCamera` | `CameraRequest{cam_name}` | `Empty` | Stop the pipeline and release the camera. |
| `ReloadCalibration` | `CameraRequest{cam_name}` | `Empty` | Reload `calibration.json` from disk into the live pipeline. |
| `Shutdown` | `Empty` | `Empty` | Stop all pipelines and exit the daemon. |
| `GetCameraInfo` | `CameraRequest{cam_name}` | `CameraInfoResponse` | `cam_name`, `calibrated`, `frame_w/h`, `fps`. |
| `CaptureFrame` | `CameraRequest{cam_name}` | `CaptureFrameResponse{jpeg}` | Most recent raw frame as JPEG bytes. |
| `GetTags` | `CameraRequest{cam_name}` | `TagFrameResponse` | One-shot latest tag detections. |
| `WhereIs` | `WhereRequest{query, cam_name}` | `WhereResponse` | Natural-language playfield feature lookup. |
| `GetImageStream` | `StreamRequest{cam_name, max_hz}` | `StreamEndpoint` | Allocate an image stream socket on demand. |
| `GetTagStream` | `StreamRequest{cam_name, max_hz}` | `StreamEndpoint` | Allocate a tag/overlay stream socket on demand. |
| `PublishOverlay` | `PublishOverlayRequest{cam_name, overlay}` | `StatusReply{ok, error}` | Inject overlay elements to all tag-stream subscribers. |

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

Priority order (highest wins):

| Priority | Source |
|----------|--------|
| 4 (highest) | `APRILCAM_*` environment variables |
| 3 | `.env` file (walk up from CWD) |
| 2 | `.aprilcam` project dotfile (walk up from CWD) |
| 1 | `~/.aprilcam` user dotfile |
| 0 (lowest) | `/etc/aprilcam.env`, `/etc/aprilcam/aprilcam.env` |

System-wide defaults in `/etc/aprilcam.env` (or `/etc/aprilcam/aprilcam.env`)
are loaded first and can be overridden by any higher-priority source. This is
the recommended place to set `APRILCAM_SYSTEM=1` for a system service
installation.

| Key | Default | Description |
|-----|---------|-------------|
| `APRILCAM_DATA_DIR` | FHS: `/var/lib/aprilcam` · XDG: `~/.local/share/aprilcam` | Root directory for persistent state (cameras, calibrations, playfields). |
| `APRILCAM_SOCKET_DIR` | FHS: `/run/aprilcam` · XDG: `$XDG_RUNTIME_DIR/aprilcam` | Directory for the control socket, stream sockets, and pidfile. |
| `APRILCAM_LOG_DIR` | FHS: `/var/log/aprilcam` · XDG: `~/.local/state/aprilcam` | Directory for `aprilcamd.log`. |
| `APRILCAM_LOG_LEVEL` | `INFO` | Python logging level for the daemon (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `APRILCAM_DAEMON_PIDFILE` | `<socket_dir>/aprilcamd.pid` | Pidfile path. |
| `APRILCAM_DETECTION_FPS` | `10` | Detection loop frame-rate cap in frames per second. |
| `APRILCAM_STATIC_DESKEW` | `1` | Enable homography-derived static-camera deskew (set `0` to disable). |
| `APRILCAM_DESKEW_PX_PER_CM` | `0` | Output resolution for the deskewed view in pixels/cm (`0` = auto). |
| `APRILCAM_UNDISTORT` | `0` | Apply lens undistortion before deskew warp when intrinsics are present. |
| `APRILCAM_MOVEMENT_THRESHOLD_PX` | `0` | Movement-invalidation threshold in source pixels (`0` = auto). |
| `APRILCAM_SYSTEM` | `auto` | Force FHS (`1`) or XDG (`0`) directory layout regardless of euid. Auto selects FHS when `euid == 0`. Set to `1` when using `DynamicUser=yes` in systemd. |

### Directory Layout

AprilCam auto-selects between two directory layouts based on whether it is
running as root (`euid == 0`) or `APRILCAM_SYSTEM=1` is set.

| Path | FHS (root / `APRILCAM_SYSTEM=1`) | XDG (user) |
|------|-----------------------------------|------------|
| Data | `/var/lib/aprilcam` | `~/.local/share/aprilcam` |
| Socket / runtime | `/run/aprilcam` | `$XDG_RUNTIME_DIR/aprilcam` |
| Logs | `/var/log/aprilcam` | `~/.local/state/aprilcam` |
| Config (read-only) | `/etc/aprilcam/aprilcam.env` | `~/.aprilcam` |

When running under systemd with `DynamicUser=yes`, the daemon is not root but
systemd creates the FHS directories via `StateDirectory=`, `RuntimeDirectory=`,
and `LogsDirectory=`. Set `APRILCAM_SYSTEM=1` (in the unit's `Environment=`
line or in `/etc/aprilcam.env`) so the config loader uses the FHS paths that
systemd prepared.

---

## Known Limitations

- **Dead pipeline not auto-restarted.** If a camera is unplugged, its
  capture thread exits but the daemon still considers it registered;
  `OpenCamera` then returns the existing (dead) handle. Workaround:
  `CloseCamera` then `OpenCamera`, or restart the daemon.

- **Daemon does not exit when idle.** It runs until `Shutdown` or a kill
  signal, even with no cameras open and no subscribers connected.
