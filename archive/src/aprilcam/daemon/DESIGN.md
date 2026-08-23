# Daemon (`aprilcam.daemon`)

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** stable

---

## 1. Purpose

The daemon is the single camera owner and single vision authority: the only
process that opens capture devices, runs OpenCV, and computes derived
perception products (tag detections, world coordinates, velocities). The seam
is a *process* boundary, not just a module boundary — every other component
(MCP server, CLI, viewer, remote robots) is a thin client speaking gRPC and
binary streams. The daemon alone owns camera hardware contention, singleton
discipline, stream fan-out/backpressure, and the per-frame world-coordinate
post-processing (origin shift, parallax, mobile-tag mount correction).

## 2. Orientation

`server.py:DaemonServer` is the process skeleton: acquires the machine-global
flock, writes a convenience pidfile, builds the gRPC server, installs signal
handlers, starts mDNS, and blocks on a shutdown event. `grpc_server.py:
AprilCamServicer` implements every RPC as a thin delegation layer over a
shared `{cam_name: CameraPipeline}` dict; `make_grpc_server()` binds the unix
socket and TCP transports with reflection enabled. `camera_pipeline.py:
CameraPipeline` is the per-camera workhorse: one background capture thread
per open camera that reads frames, calls `core.AprilCam.process_frame()`,
applies world post-processing, appends to a 300-entry ring buffer, and hands
results to stream producers. `stream.py` owns all stream sockets:
`ImageStreamProducer` (JPEG frames) and `TagStreamProducer` (change-driven
`TagFrame`s with heartbeat), each with accept-loop and per-subscriber sender
threads. `mdns.py` advertises the TCP endpoint; `mobile_tags.py` persists the
client-registered mobile-tag registry; `v4l2_reader.py` is the Raspberry Pi
loopback capture shim. `protocol.py` and `client.py` are **dead legacy
transport** (msgpack framing, newline-JSON control client) — unreferenced by
any live code; do not build on them.

Data flow per frame: `cap.read()` → `process_frame()` (detection/tracking in
`core/`) → stale/dark advisory checks → world correction (A1 origin shift →
parallax → mount offset) → ring buffer → `ImageStreamProducer.publish()` +
`TagStreamProducer.publish_if_changed()`.

## 3. Constraints and Invariants

- **The lock file is never unlinked** (`server.py:442`) — unlinking reopens a
  create/lock race between unlink and the next open+flock. Stale-lock
  recovery is lock-first-then-rewrite on the *existing* file: a dead holder's
  flock is already released by the kernel.
- **The flock on `daemon_lock_path` (default `/tmp/aprilcamd.lock`) is the
  sole singleton guard**; the pidfile is a discovery cache only. Treating the
  pidfile as a lock reintroduces duplicate daemons across `socket_dir`s.
- **`grpc.so_reuseport` must stay 0** and every `add_insecure_port` return
  value checked (`grpc_server.py:1163-1191`) — otherwise a second daemon can
  silently co-bind port 5280 and the kernel load-balances RPCs between two
  daemons (observed pre-sprint-018).
- **World origin shift is a pure subtraction on both axes — NEVER a y-flip**
  (`camera_pipeline.py:1023-1044`). The def-driven homography is already
  A1-centred with +y=north; a stale `origin_y - y` inverted north/south. Any
  consumer of `world_xy` must use the daemon-published `origin_x`/`origin_y`.
- **Per-tag world correction is wrapped in try/except** — a bad correction
  must never kill the capture thread (that bricks the camera until restart);
  the uncorrected record is emitted instead.
- **The daemon reads `config.json` but never writes it**, except through the
  sanctioned `SetCameraConfig` proxy (writes to the config root via
  `camera_config.save_camera_config`). Violating this breaks the
  developer-owned config / regenerable state split.
- **Stream backpressure is silent drop**: per-subscriber `Queue(maxsize=2)`;
  a slow consumer loses frames, never blocks the pipeline. Do not "fix" the
  drop by blocking — one stalled viewer would stall detection for everyone.
- **`info.json` flags (`calibration_stale`, `frame_dark`) are purely
  advisory** — nothing may gate or refuse an operation on them.
- **`GetObjects` must use the same `_a1_origin()` as the tag path** —
  subtracting `fw/2, fh/2` independently double-shifted objects (fixed;
  `grpc_server.py:464`).
- **Dependency quirk**: `daemon/stream.py` imports `client.models`
  (`StreamEndpoint`) — the daemon depends on the client package's Pydantic
  models. Don't add heavier daemon→client imports.
- **Non-goal**: no HTTP/WS surface, no path/annotation state in the daemon
  (v2 removes the vestigial `GetPaths`/`SetPaths`/`PublishOverlay`).

## 4. Design

**Singleton & takeover.** `--force` performs a local SIGTERM → 5 s wait →
SIGKILL takeover, then retries the flock once (`server.py:_force_takeover`).
The graceful Shutdown-RPC-first step is deliberately layered in
`cli/daemon_cli.py`, keeping `daemon/server.py` a leaf module with no
`client/` import.

**Transports.** gRPC control plane on `unix:/tmp/aprilcam/control.sock` and
TCP `[::]:5280` (both default-on), with server reflection for grpcurl. mDNS
advertises `_aprilcam._tcp.local.` using the UDP-connect trick
(`mdns.py:_primary_routable_ipv4` — connect a SOCK_DGRAM to 192.0.2.1, read
`getsockname()`), avoiding Ubuntu's `/etc/hosts` 127.0.1.1 hostname alias.
All zeroconf failures are swallowed; mDNS is never load-bearing.

**Streams.** Allocated on demand by `GetImageStream`/`GetTagStream` (one
producer per camera per type, cached); each producer binds a uuid-suffixed
unix socket under `socket_dir/<cam>/` plus a TCP socket on an OS-assigned
ephemeral port (0.0.0.0 so LAN clients reach a Pi), returned in
`StreamEndpoint`. Wire framing for both streams: `[uint32 BE length][protobuf]`
— `ImageFrame` raw, `TagFrame`/`OverlayFrame` wrapped in `StreamMessage`.
Threads per producer: one accept loop per socket + one sender thread per
subscriber; `TagStreamProducer` adds a heartbeat thread.

**Tag publish policy** (`stream.py:TagStreamProducer`): publish when the
visible ID set changed or any tag moved > 8 px, rate-capped at 20 Hz
(`max_hz=0` = uncapped); a heartbeat force-publishes the latest frame after
1 s of silence so subscribers can distinguish "static scene" from "dead
stream".

**Idle gating.** The capture loop resolves subscribers up front each
iteration; with no image/tag subscribers and no pull RPC within a 3 s grace
window it drops to 1 fps and skips JPEG encoding entirely. `capture_frame()`
encodes lazily from the cached raw ndarray (frame-id-matched) so a pull
always gets a current frame without the loop paying encode cost for nobody.

**Self-heal** (`_capture_loop`): after 5 consecutive read failures, a
loopback capture (has `.reopen()` — the Pi bridge's v4l2loopback feed via
`V4l2CtlCapture`) reconnects *forever* (the bridge watchdog may restart it
any time); a plain USB `cv.VideoCapture` is re-opened at most 10 times
(re-applying UVC settings — the OV9782's manual exposure — each time), and
the budget resets to 0 on any healthy frame. Only a persistently dead USB
camera stops the pipeline. Rationale for the asymmetry: loopback dropouts
are routine and external; a USB device that won't reopen is unplugged.

**World post-processing order** (fixed, per tag): (1) A1 origin shift
(`_a1_origin()` prefers the calibrated `apriltag:1` static-marker world
position, falls back to field centre); (2) parallax via
`calibration.correct_world_for_height` when `camera_position` and a
registered tag height exist; (3) mobile-tag mount offset — offset (mm→cm)
rotated by robot heading is subtracted from the tag position, heading
rotated by mount yaw, so the reported pose is the *robot centre*
(`_robot_centre_from_tag`).

**Mobile-tag registry** (`mobile_tags.py`): `{tag_id: MobileTag(x_mm, y_mm,
z_cm, yaw_deg, owner)}` persisted atomically to `data_dir/tags.json` under
`"tags"`; legacy `{"tag_heights": …}` files read transparently and migrated
on next write. Register/Clear RPCs push the whole registry into every
running pipeline; `apply_mobile_registry` swaps whole dicts (atomic under
the GIL) so the capture thread reads lock-free.

**Advisory signals.** `calibration_stale` is surfaced from the playfield
boundary's movement-invalidation flag; `frame_dark` is a windowed signal
(mean luminance of a strided-downsample every 5th frame < 25.0 AND zero
tags, 30 consecutive samples to set, one non-dark sample to clear).
`info.json` is rewritten only on flag transitions, atomically (tmp+rename).

**Restart detection.** `AprilCamServicer.boot_id` (uuid4 per process) rides
on `ListPlayfieldsResponse` so long-lived clients distinguish a daemon
restart from a transient channel reconnect.

**Camera naming.** `OpenCamera` resolves index → stable identity →
`CameraRegistry.resolve()` → the record's `dir` as `cam_name`, so per-camera
data dirs are registry-owned and survive replug. Falls back to
`device_name_slug`/`cam-<index>` only if the registry fails.

## 5. Interfaces

### Exposes

**gRPC `AprilCam` service** (proto in `aprilcam/proto/`), grouped:

- *Lifecycle*: `ListCameras` (open cameras), `EnumerateCameras` (hardware
  probe + stable enum numbers; the only sanctioned enumeration path for
  clients), `OpenCamera(index) → cam_name, camera_dir` (idempotent),
  `CloseCamera`, `ReloadCalibration`, `Shutdown`.
- *One-shot queries*: `GetCameraInfo`, `CaptureFrame` (latest JPEG, 3 s
  wait), `GetTags` (latest `TagFrameResponse` with homography, corners,
  field dims, origin), `GetObjects` (one-shot HSV object detection with
  60 px polygon inset, aspect ≤ 2.0, min dim ≥ 15 px filters), `WhereIs`
  (keyword search over the playfield map, live-tag merge, full-map fallback).
- *Streams*: `GetImageStream` / `GetTagStream` → `StreamEndpoint
  {socket_path, tcp_port}`; then length-prefixed protobuf on the socket.
- *Config proxies* (whole JSON blobs, atomic writes): `GetCameraConfig` /
  `SetCameraConfig` (config root), `GetCalibration` / `SetCalibration`
  (state root; write triggers live pipeline reload; injects the `camera`
  slug key so RPC and direct writes produce the same schema), `GetPaths` /
  `SetPaths` (legacy), `ListPlayfields` (+ `boot_id`).
- *Mobile tags*: `RegisterMobileTag`, `ClearMobileTags`, `ListMobileTags`.
- `PublishOverlay` — broadcast an `OverlayFrame` to tag-stream subscribers
  (legacy; removed in v2).

Contract notes: NOT_FOUND for unopened cameras; errors as gRPC status
codes; all file writes atomic; `OpenCameraResponse.camera_dir` is the one
place a path leaks to clients (legacy — MCP must not forward it).

### Consumes

- `aprilcam.camera` — device enumeration, identity, registry, libcam
  backend, `config.json` load/save (owns camera identity; see its DESIGN).
- `aprilcam.core` — `AprilCam.process_frame`, `TagRecord`/`FrameRecord`/
  `RingBuffer`, `playfield_query` (owns detection/geometry).
- `aprilcam.calibration` — calibration load, `correct_world_for_height`,
  `corner_pixels_from_homography` (owns pixel→world math).
- `aprilcam.vision.color_classifier` — `GetObjects` classification.
- `aprilcam.client.models` — `StreamEndpoint` (dependency quirk, §3).
- `aprilcam.config` — directory resolution (`effective_config_dir` fallback
  for legacy single-tree installs), flags (`static_deskew`, `undistort`).

## 6. Open Questions / Known Limitations

- **Dead code**: `daemon/protocol.py` and `daemon/client.py` are the
  pre-gRPC transport, referenced by nothing. **Deprecated — v2 removes them
  outright** (spec §4.1, §6).
- **Stale-registry bug**: `OpenCamera`'s registry resolve can orphan a
  re-plugged camera from its calibration dir when a stale record owns the
  bare slug (see `clasi/issues/stale-registry-record-orphans-camera-from-
  its-calibration-dir.md`) — the daemon then runs uncalibrated with
  `world_xy=(0,0)`. Root cause lives in `camera/registry.py`.
- **Every client warps its own deskew** today. v2 adds a daemon-computed
  **deskewed playfield stream** (fixed bounding box, default 800×800,
  px-per-cm derived and reported in stream metadata) published like the raw
  stream (spec §4.1).
- **Calibration runs client-side** today (CLI/MCP import cv2). v2 moves it
  into the daemon behind a `Calibrate` RPC (spec §4.4).
- **v2 config service**: `SetPlayfield`/`GetPlayfield` and
  `ResetCameraConfig` join the whole-document proxies; the daemon becomes
  the rendezvous point for all shared configuration (spec §4.1, §4.5).
- **v2 removals** from this package's surface: `GetPaths`/`SetPaths`,
  `PublishOverlay`/`OverlayFrame` push, and the `paths_file` key in
  `info.json` (spec §6 — drawing becomes a client-side library).
- `CameraPipeline` still constructs the full `AprilCam` object (which drags
  in `ui.display.PlayfieldDisplay` even headless); v2's pipeline
  consolidation into `daemon/` should shed that (spec §4.3).
