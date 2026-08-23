---
title: Daemon Wire Protocol
blurb: The gRPC control service and length-prefixed protobuf stream sockets the aprilcam v2 daemon exposes — the wire-level contract beneath the Python client.
order: 6
updated: 2026-08-23
tags: [daemon, grpc, protobuf, protocol, mdns]
---

# AprilCam Daemon Wire Protocol

`aprilcamd` is a long-running background process that owns all cameras, runs
AprilTag/ArUco detection, and serves per-frame data to any number of clients.
It exposes one **gRPC** control service plus on-demand **length-prefixed
protobuf** stream sockets for bulk data (images, tag streams) — frame bytes
never flow over the gRPC connection itself.

This page describes the wire-level contract for someone implementing a client
in another language. If you are writing a robot program in Python, use the
[Robot Direct API](robot-direct-api.md) client instead — it wraps everything
below. For running and configuring the daemon itself, see
[Operating the Daemon](daemon.md); for the big picture, [Overview](overview.md).
The authoritative schema is
[`proto/aprilcam_v2.proto`](https://github.com/League-Robotics/aprilcam/blob/master/proto/aprilcam_v2.proto).

---

## Endpoints and discovery

The control plane is the gRPC service **`aprilcam.v2.AprilCamV2`**, served on
two transports simultaneously:

| Transport | Endpoint | Notes |
|-----------|----------|-------|
| Unix socket | `<socket_dir>/control.sock` | Same-host clients. `socket_dir` resolves to `/run/aprilcam` (root) or an XDG runtime dir — see [Operating the Daemon](daemon.md). |
| TCP | `0.0.0.0:5280` | `DEFAULT_DAEMON_PORT`, unauthenticated plaintext (`add_insecure_port`). A TCP bind failure is non-fatal: the daemon logs a warning and continues unix-socket-only. |

**mDNS discovery.** When (and only when) the TCP bind succeeds, the daemon
registers a Bonjour/zeroconf service record:

- Service type: **`_aprilcam._tcp.local.`**
- Instance name: `aprilcam-<hostname>._aprilcam._tcp.local.`
- Port: the bound gRPC TCP port (5280 by default)
- TXT properties: `version` (the aprilcam package version) and `host` (the
  daemon's `socket.gethostname()`)

Clients browse that service type to build a daemon list. The `host` TXT key is
load-bearing: the Python client compares it against its *own* hostname to drop
the local daemon's advertisement (the local daemon is found by probing the
lock file and `control.sock` directly instead). Advertising itself is never
load-bearing — a daemon whose multicast stack is broken still works over its
sockets, and a direct connect (`-d HOST`, implied port 5280) never touches
discovery at all.

**Version handshake.** `GetStatus` — the first RPC the Python client makes on
any fresh connection — echoes `protocol_version` (currently **1**,
`daemon.rpc.PROTOCOL_VERSION`). The proto file only ever grows — existing
fields are never removed or renumbered — so an older client keeps working
against a newer daemon (unknown fields are ignored per protobuf rules), and
`protocol_version` bumps only if that guarantee is ever broken. A responder
that fails `GetStatus` with `UNIMPLEMENTED` and no `aprilcam-error-type`
trailing metadata is not a v2 daemon. Note: `grpcio-reflection` is declared
as a daemon dependency, but the server does not currently register the gRPC
reflection service — use `GetStatus` (or the `.proto` file) rather than
runtime reflection.

**Camera addressing.** Every `camera` request field accepts either form
`aprilcam camera list` prints: an **all-digits token** resolves by persistent
registry number, anything else by slug (slugs always contain a non-digit, so
the forms can't collide). There is no open/close RPC — the daemon opens a
camera on first use and holds it.

---

## Control service RPCs

All RPCs are unary. The two `Get*Stream` RPCs return a *descriptor* of a
separate socket, never a gRPC stream.

**Cameras and status**

| RPC | Purpose |
|-----|---------|
| `ListCameras` | Every camera the registry knows (present or not), from cached state — never re-probes. |
| `ProbeCameras` | Deep re-interrogation of every backend. May be slow; cameras held open for a live caller are reported from cache and named in `skipped`. |
| `GetStatus` | `running`, camera states, `config_dir`, `state_dir`, `protocol_version`. |
| `GetFrame` | One raw or deskewed frame (`encoding`: `"jpeg"` default, `"png"` lossless). `deskewed=true` on an uncalibrated camera fails — never silently substitutes a raw frame. |

**Tags**

| RPC | Purpose |
|-----|---------|
| `GetTags` | All tags currently detected on a camera. Triggers exactly one detection burst and appends it to the ring buffer; an empty field returns an empty `TagFrame`, never an error. |
| `GetTag` | One tag by `(family, number)` — same single burst, filtered. Absent `record` means "not detected this burst", never a zero-valued record. |
| `GetTagHistory` | Recent `TagFrame`s from the ring buffer (optionally one tag, last N frames). Pure buffer read; frames where the tag was absent appear as entries with empty `tags` — gaps are never dropped. |

**Playfields, config, calibration**

| RPC | Purpose |
|-----|---------|
| `SetPlayfield` / `GetPlayfield` / `ListPlayfields` | Adopt/fetch/list playfield definitions, held in daemon memory, keyed by name. `GetPlayfield` adds derived `linked_cameras`. |
| `SetCameraConfig` / `GetCameraConfig` | Push/fetch a camera's config (playfield link, hardware settings as JSON text, camera position). An unconfigured camera is `NOT_FOUND`. |
| `ResetCameraConfig` | Discard the live override and re-apply `config_dir/cameras/<slug>/config.json` from disk — ends an experiment without a daemon restart. |
| `Calibrate` | Run a detection burst and solve the pixel↔world homography against the linked playfield's ArUco set (optional `estimate_intrinsics`). The client sends a camera name, never pixels; no playfield link → `FAILED_PRECONDITION`. |
| `GetCalibration` | The currently-held calibration. Absent means "never calibrated" — never a fabricated default. |

**Annotations, features, mobile tags, guides**

| RPC | Purpose |
|-----|---------|
| `PutAnnotation` / `RemoveAnnotation` / `ReplaceLayer` / `ClearAnnotations` / `GetAnnotations` | The playfield-addressed annotation store (world-cm drawings the daemon stores and embeds in deskewed frames — it never renders). Upsert by `(layer, id)`, last write wins; `ReplaceLayer` swaps a layer atomically. In-memory only — a restart yields an empty set at revision 0. |
| `FindFeature` | Fuzzy-match free text ("blue dot") against a playfield's surveyed features. No camera, no calibration needed; no match → empty list. |
| `RegisterTag` / `UnregisterTag` / `ListTagParameters` | The mobile-tag registry: how a tag is mounted on its robot (size, offset, height, yaw). In-memory; corrections are applied inside the daemon before a `TagRecord` is serialized — the math never crosses the wire. |
| `GetGuide` | The packaged agent/robot usage guide text (`"agent"` default, `"robot"`). |

**Stream endpoints**

| RPC | Purpose |
|-----|---------|
| `GetImageStream` | Allocate (or reuse) the raw-socket endpoint for a camera's image stream, raw or `deskewed`. |
| `GetTagStream` | Same, for a camera's tag stream. |

### Error mapping

The daemon's typed errors cross the wire as a gRPC status code plus **trailing
metadata**, so a client can reconstruct the same typed exception instead of
parsing status text. Every error carries `aprilcam-error-type` (the exception
class name); specific types add fields:

| Error | Status code | Extra metadata keys |
|-------|-------------|---------------------|
| `UnknownCamera`, `UnknownPlayfield`, `UnknownGuide` | `NOT_FOUND` | `aprilcam-requested`, `aprilcam-available` (comma-joined) |
| `CameraNotPresent` | `FAILED_PRECONDITION` | `aprilcam-slug` |
| `NotCalibrated` | `FAILED_PRECONDITION` | — |
| `NoPlayfieldLinked` | `FAILED_PRECONDITION` | `aprilcam-camera` |
| `AlreadyRunning` | `ALREADY_EXISTS` | `aprilcam-holder` |
| `AnnotationLimitExceeded` | `RESOURCE_EXHAUSTED` | `aprilcam-kind`, `aprilcam-bound`, `aprilcam-actual` |
| `UnsupportedSetting` | `INVALID_ARGUMENT` | `aprilcam-key`, `aprilcam-reason` |
| anything else | `INTERNAL` | — |

---

## Key message schemas

A wire-wide convention: **absence is meaningful**. proto3 `optional` fields
distinguish "not applicable" from a legitimate zero — an uncalibrated tag
record has *no* `world` field, never a `(0, 0)` placeholder. Check `HasField`
(or your language's presence API) rather than testing for zero.

### TagRecord / TagFrame

`GetTags`, `GetTag`, `GetTagHistory`, the tag stream, and the `tags` embed in
`GetFrameResponse` all carry the same records:

| Field | Type | Description |
|-------|------|-------------|
| `tag` | `TagId` | `family` (`APRILTAG` \| `ARUCO`) + `number` (the family's own non-negative id). |
| `world` | optional `WorldPoint` | Position in cm, A1-centred, +x east +y north. Absent when uncalibrated. |
| `heading_rad`, `speed` | optional double | Velocity heading and speed (world units). |
| `world_velocity` | optional `WorldPoint` | Velocity components, cm/s. |
| `timestamp` | double | Capture time. |
| `pixel` | optional `PixelPoint` | Center pixel, origin top-left. |
| `calibrated` | bool | Whether world fields could be populated. |
| `yaw_rad` | optional double | Tag orientation. |
| `world_corners` / `pixel_corners` | repeated points | Exactly 4 when present (front-left, front-right, back-right, back-left); empty = absent. |

`TagFrame` wraps `repeated TagRecord tags` with `timestamp`, `frame_index`,
and `calibration_stale`. For a registered mobile tag, `world`/`yaw_rad` are
already the *robot's* pose — parallax/offset correction runs inside the daemon.

### GetFrameResponse (frames)

| Field | Type | Description |
|-------|------|-------------|
| `data` | bytes | Encoded image (`jpeg` or `png`). |
| `width`, `height` | int32 | Pixel dimensions. |
| `encoding` | string | What was actually produced. |
| `timestamp` | double | Capture time. |
| `deskewed`, `calibration_stale`, `frame_dark` | bool | Frame flags. |
| `tags` | optional `TagFrame` | The burst nearest this frame's capture. Absent = detection has never run; present-but-empty = ran and saw nothing. `GetFrame` never triggers a burst itself. |
| `px_per_cm` | optional double | Deskewed frames only: derived scale (`frame_width / field_width_cm`). |
| `world_extent` | optional `WorldExtent` | Deskewed only: field size `(width_cm, height_cm)`. |
| `annotations` | optional `AnnotationSet` | Deskewed only, and *always* set on that branch (even empty, revision 0). Never on a raw frame — raw means raw. |

One deskewed frame therefore carries everything needed to render a fully
annotated view.

### PlayfieldDefinition (playfield)

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | The playfield's key. |
| `width_cm`, `height_cm` | double | Field dimensions. |
| `april_tags`, `aruco_tags`, `rectangles`, `dots` | repeated `PlayfieldFeatureEntry` | Surveyed features by section. |

Each `PlayfieldFeatureEntry`: `slug`, `x`, `y` (world cm), optional `id` (tag
sections only — the calibration ground truth), optional `colour`
(rectangles/dots only), and `height_cm` above the field plane (plain field —
`0.0` means on the plane). `FindFeature` answers with the more abstract
`FeatureMatch` shape (`slug`, `kind`, `position`, optional `colour`,
`height_cm` — no numeric id).

### CameraConfig (config)

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Camera slug. |
| `playfield` | optional string | Link to a held playfield. |
| `settings_json` | string | Hardware settings as JSON object text (e.g. `"{}"`), stored and echoed verbatim. |
| `camera_position` | optional `CameraPosition` | `nadir` (`WorldPoint` under the lens) + `height_cm` — the inputs for tall-tag parallax correction. Absent = not configured. |

### Calibration

`Calibration`: `stale`, `calibrated_at`, `playfield`, `homography` (the solved
pixel→world 3×3 matrix as exactly 9 row-major doubles), `matched_ids`,
optional `reprojection_error_cm`, and — when a lens model was estimated —
`camera_matrix` (9 row-major doubles) and `dist_coeffs` (empty = no lens
model). `CalibrationResult` (from `Calibrate`) reports `succeeded`,
`matched_ids`, `missing_ids` (always the exact ids, never a count), and
optional `reason` / intrinsics-estimation fields.

---

## Stream sockets

`GetImageStream` and `GetTagStream` allocate — or **reuse**, one listener per
`(camera, deskewed)` / `camera` key — a raw socket and return where to connect:

```proto
message StreamEndpoint {
  optional string socket_path = 1;  // unix socket, same-host clients
  optional int32  tcp_port    = 2;  // TCP on 0.0.0.0, OS-assigned ephemeral port
}
```

Unix stream sockets are bound at `<socket_dir>/stream-<key>.sock`
(`stream-images-raw-<camera>.sock`, `stream-images-deskewed-<camera>.sock`,
`stream-tags-<camera>.sock`). Either field may be absent if that transport
failed to bind; the Python client prefers the unix path when the daemon is
local and falls back to TCP.

Allocation is cheap and starts nothing: capture, deskew, and detection begin
only when the **first connection is accepted**, and stop when the last
subscriber disconnects. There is no unsubscribe RPC — closing your socket *is*
the unsubscribe. Preconditions (unknown camera, deskewed stream on an
uncalibrated camera) are checked at first connect, not at allocation: a bad
request gets a socket that connects and then immediately closes without a
single frame — treat "connected, then EOF with no frame" as a real error, not
a hiccup.

### Wire framing

Both stream types use the same framing (`src/aprilcam/framing.py`):

```
[4 bytes: big-endian uint32 length][<length> bytes: protobuf payload]
```

Read exactly 4 bytes, then exactly that many payload bytes, and parse with the
appropriate message type. A short read means the daemon closed the stream.

### Payloads

- **Image stream** — each message is a serialized **`GetFrameResponse`**,
  byte-for-byte identical to what the one-shot `GetFrame` RPC returns for the
  same frame (tags/px_per_cm/annotations embeds included). Published
  unconditionally at a steady cadence (~15 Hz), always JPEG.
- **Tag stream** — each message is a serialized **`TagFrame`** (the same shape
  as `GetTagsResponse.tags`; there is no `oneof` wrapper message in v2).
  Detection runs at ~30 Hz while subscribed; frames are published on
  meaningful change (0.5 cm world / 8 px uncalibrated, and always when a tag
  appears or disappears), rate-capped at 20 Hz, with an unchanged-frame
  heartbeat every 1 s. The rate is fixed server-side — requests carry no
  `max_hz` field.

**Backpressure is silent drop.** Each subscriber has its own bounded queue
(depth 2); a slow reader loses frames rather than building a backlog — you
always get the latest available frame. N subscribers to the same key share
one producer: one capture, one warp, one detection per tick, fanned out.

The client-side reference implementation is
[`src/aprilcam/client/streams.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/streams.py)
(`SocketImageStream` / `SocketTagStream`); the daemon side is
[`src/aprilcam/daemon/stream_transport.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/daemon/stream_transport.py)
and
[`src/aprilcam/daemon/streaming.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/daemon/streaming.py).

