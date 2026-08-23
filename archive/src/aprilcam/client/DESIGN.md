# Client Library

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** stable

---

## 1. Purpose

The only sanctioned way to talk to the AprilCam daemon. Every non-daemon component — MCP server, web server, CLI commands, viewer, robot programs — reaches the daemon through this package: discovery of *which* daemon to talk to, a typed gRPC wrapper (`DaemonControl`), binary stream consumers, Pydantic domain models, and the base-26 host/camera addressing scheme. The seam it owns is the **proto boundary**: protobuf-generated types enter and leave the system only inside this package's `from_proto` adapters, so no consumer ever handles wire types. It is deliberately installable without OpenCV — the base (`pipx install aprilcam`) client must work on machines that will never do vision.

## 2. Orientation

Five modules. `discovery.py`: mDNS browse (`_aprilcam._tcp.local.`, zeroconf optional) and `resolve_daemon_target`, the single precedence resolver every entry point uses. `control.py`: `DaemonControl`, one method per daemon RPC, converting protos to Pydantic models or plain dicts; `connect_default` combines discovery with a reachability probe. `stream.py`: `ImageStreamConsumer` and `TagStreamConsumer`, blocking socket readers for the daemon's length-prefixed protobuf streams. `models.py`: `TagRecord`, `TagFrame`, `ImageFrame`, `CameraInfo`, `CameraDevice`, `PathRecord`, `StreamEndpoint` — the vocabulary all consumers share. `host_codes.py`: bijective base-26 codec (`A`…`Z`, `AA`…), the `hosts.json` store written by `aprilcam probe`, and code→host/camera resolution. `_imaging.py`: the lazy `require_cv2()` gate.

Typical flow: `resolve_daemon_target(config, cli_args)` → `DaemonControl(...)` → `connect()` (gRPC channel over unix socket or TCP) → RPCs; for live data, `get_tag_stream()`/`get_image_stream()` allocate a stream endpoint via RPC, then a consumer connects a raw socket and iterates decoded messages.

## 3. Constraints and Invariants

- **cv2-free at import time** — no module in this package imports OpenCV at top level. Only `DaemonControl.capture_frame()` (JPEG → BGR ndarray) calls `require_cv2()`; `capture_frame_jpeg()` and both stream consumers use Pillow or raw bytes. Breaking this breaks the entire base install.
- **`from_proto` classmethods are the only proto boundary** — proto types may appear in adapter bodies and in the two documented leaks (§6), nowhere else. Letting `*_pb2` types escape couples every consumer to protobuf regeneration.
- **`resolve_daemon_target` / `connect_default` never spawn a daemon** — clients that auto-start daemons violate the singleton-daemon design and race the flock. The daemon is started only by `aprilcam daemon start` / systemd.
- **Discovery precedence is fixed**: (1) explicit CLI `daemon_host`, (2) `APRILCAM_DAEMON_HOST`/config, (3) local unix-socket connectivity probe (`<socket_dir>/control.sock`, 0.2 s, sends nothing), (4) mDNS — exactly one result auto-selects; more than one raises `DaemonNotFoundError` listing `host:port` candidates; zero raises with start-the-daemon guidance. Reordering silently changes which daemon a lab machine talks to.
- **Stream wire format is frozen**: 4-byte big-endian uint32 length prefix + protobuf payload. The tag stream carries `StreamMessage` (oneof `tag_frame` | `overlay`); the image stream carries bare `ImageFrame`. Robots and the future ROS bridge depend on this contract.
- **Unix stream sockets are used only when the daemon is local** (`_host_is_local`) — a remote daemon's `socket_path` names a file on *its* filesystem; consumers must fall through to `tcp_port` with the gRPC host.
- **`.local` hostnames must be resolved through the OS resolver before reaching gRPC** (`_resolve_host_to_ip`) — gRPC's c-ares resolver cannot do mDNS; passing `vidar.local` straight to `grpc.insecure_channel` fails even though the host is reachable.
- **Host/camera number stability**: once `hosts.json` assigns a number it is never reassigned by re-probing; the local host is always num 1 (letter `A`) and remotes start at 2. The camera **slug** is the reliable join key — stored enums can go stale across replug.
- **`ImageStreamConsumer.read()` returns RGB** (Pillow), not BGR, and returns a *writable copy* (`np.array`, not `asarray`) so consumers can draw in place.

## 4. Design

**DaemonControl.** Thin, synchronous, stateful only in the channel (`_channel`/`_stub`; `connect()` idempotent, context-manager support). `connect_default` resolves a target, connects, then probes with `list_cameras()` — an unreachable daemon fails fast with a `DaemonNotFoundError` naming the exact target, instead of erroring on the first real RPC. `_stream_host()` picks `"localhost"` for unix-connected instances and `self._host` for TCP, so stream consumers reach the same machine as the control channel. Mobile-tag RPC replies round floats to shed float32 wire noise.

**Stream consumers.** Deliberately blocking-socket, not gRPC-streaming: the daemon publishes frames on dedicated sockets with bounded queues and silent drop, and a dumb blocking reader is trivially consumable from threads (the MCP server and web server both wrap `read()` in a thread). `set_timeout()` exists so viewers can bound the wait for a first frame. Iteration swallows `EOFError` as normal end-of-stream and closes the socket.

**Models.** Pydantic throughout; proto→model conversion normalises flattened wire arrays (9-float homography → 3×3, flat corner list → pairs). The proto cannot encode `None`, so `from_proto` treats all-zero velocity/speed/heading as absent, and `world_xy` as absent unless `in_playfield` or a nonzero coordinate says otherwise — a genuinely-at-origin calibrated tag survives via the `in_playfield` flag, but callers needing certainty must check `CameraInfo.calibrated`. `TagFrame` carries the daemon-published `homography`, `origin_x`/`origin_y` (A1-centred, +y north) and field dimensions so clients can convert world↔pixel without private knowledge; `by_id()` is the sanctioned per-tag accessor.

**Host codes.** `num_to_alpha`/`alpha_to_num` are bijective base-26 (A=1). Codes: local camera = one letter (`B` = local cam 2); remote = host letter + camera letter (`FB` = host 6, cam 2). `merge_probe_results` enforces stability: hosts matched by hostname (`.local`-tolerant) or address overlap keep their number; local is forced to 1, bumping any usurper; cameras matched by slug keep theirs. The store is written atomically (temp file + `os.replace`); a corrupt store is silently reset rather than crashing probe.

## 5. Interfaces

### Exposes

- `resolve_daemon_target(config, cli_args=None) -> (host, port, unix_path|None)` — the four-level resolver; raises `DaemonNotFoundError`. `discover_daemons(timeout)` — raw mDNS browse, `[]` without zeroconf.
- `DaemonControl` — constructor (`unix_path` | `host`/`port`), `connect_default(config, cli_args=…)` (never spawns; probes), `connect`/`close`/context manager. RPCs: `list_cameras`, `enumerate_cameras -> list[CameraDevice]`, `open_camera(index) -> (cam_name, camera_dir)`, `close_camera`, `get_camera_info -> CameraInfo`, `reload_calibration`, `capture_frame_jpeg -> bytes`, `capture_frame -> BGR ndarray` (needs cv2), `get_tags -> TagFrame`, `get_tag(cam, id) -> TagRecord|None`, `get_objects`, `where_is(query, cam_name) -> dict`, `get_image_stream`/`get_tag_stream -> connected consumer`, `publish_overlay(cam, elements, ttl) -> bool`, mobile-tag registry (`register_mobile_tag`, `clear_mobile_tag(s)`, `list_mobile_tags`), config/state proxies (`get/set_camera_config`, `get/set_calibration`, `get/set_paths`), `list_playfields` (reply carries `boot_id` for restart detection), `shutdown`.
- `ImageStreamConsumer` — `read() -> RGB ndarray`, `read_raw() -> (frame_id, jpeg)`, `read_image_frame() -> ImageFrame`, `set_timeout`, iteration. `TagStreamConsumer` — `read() -> TagFrame | OverlayFrame` (multiplexed oneof), iteration.
- Models: `TagRecord`, `TagFrame` (incl. homography/origin/field dims, `by_id`), `ImageFrame`, `CameraInfo`, `CameraDevice` (`index` = volatile OS index, `enum` = stable handle), `PathRecord`, `StreamEndpoint`.
- `host_codes`: `num_to_alpha`/`alpha_to_num`, `code_for`, `resolve_code(_or_none)`, `resolve_host_token`, `find_host`, `load_store`/`save_store`, `merge_probe_results`.
- `_imaging.require_cv2()` — shared lazy cv2 gate with install hint.

### Consumes

- `aprilcam.proto` (`aprilcam_pb2`, `aprilcam_pb2_grpc`) — generated wire types; owned by proto/.
- `aprilcam.config.Config` — socket_dir, daemon_host/port, data_dir; owned by top-level config.
- `aprilcam.errors` — `DaemonNotFoundError`; owned by top-level errors module.
- `grpc`, `zeroconf` (optional), `PIL` — third-party.

## 6. Open Questions / Known Limitations

- **`PathRecord.color` is documented BGR while `server/paths.py` stores RGB** — a live inconsistency between the two path models; nothing currently round-trips a `PathRecord` through the server registry, which is why it hasn't bitten. Moot in v2: paths-as-a-service is removed (spec §6).
- **Zero-as-None float sentinels** in `TagRecord.from_proto` (speed/heading exactly 0.0 → `None`) misclassify a genuinely stationary reading; acceptable for display, wrong for control loops that distinguish "zero" from "unknown". A proto `optional`/presence migration would fix it.
- **Two proto leaks**: `get_objects` returns the raw `GetObjectsResponse`, and the file-proxy RPCs return raw `JsonBlobReply`/`StatusReply` — both violate the "Pydantic at the boundary" rule and are consumed directly by the MCP/web servers.
- **v2 additions (spec §4.6)**: a pure-Python **drawing library** lives here — fetch a raw or deskewed frame, overlay paths/waypoints/markers/shapes in world coordinates using stream metadata for the cm↔px mapping; it absorbs the `ui/display.py` draw helpers and becomes the *only* way to draw on the playfield (per-client, in-process, nothing persisted by AprilCam). Also a **deskewed-stream consumer** for the daemon's new deskewed playfield stream (spec §4.1), whose metadata (frame dims + field cm dims) makes conversion exact.
- **v2 removals**: `get_paths`/`set_paths` and `publish_overlay` wrappers go with the path/overlay machinery (spec §6). Discovery, `DaemonControl`, stream framing, models, and host codes carry forward unchanged — the ROS bridge (spec §8) depends on these stream contracts staying stable.
