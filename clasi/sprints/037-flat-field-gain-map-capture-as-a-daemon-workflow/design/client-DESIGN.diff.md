---
source_file: client-DESIGN.md
source_hash: 2e11ccb5e246482cce5ee9dad08113e492f8f665eaf4f56f628a20a0d1322c97
---
# Diff: client-DESIGN.md

Comparison of the sprint overlay copy of `client-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- client-DESIGN.md (pristine)
+++ client-DESIGN.md (current)
@@ -25,10 +25,10 @@
 Eight non-empty files, ~2885 lines:
 
 - **`interfaces.py`** (156 lines) — `DaemonInfo` (frozen `@dataclass(frozen=True, kw_only=True)`: `host`, `port`, `is_local`, `reachable`), directly constructed rather than built via a separate implementation class. `Discovery`, `Guides`, `Renderer` are plain base classes (`NotImplementedError` bodies, never `typing.Protocol`) that this package's concrete classes derive from. `ImageStream`/`TagStream` deliberately do *not* live here — they're declared in `daemon/interfaces.py` next to the `Daemon` methods that return them, since unsubscription is a daemon-visible event.
-- **`daemon_client.py`** (633 lines) — `GrpcDaemon`, the concrete `Daemon`. Builds every outgoing proto request itself (the one client module allowed to touch a request message directly); every response is handed straight to a `models.py` converter, never read here. Owns `_reconstruct_error`, the trailing-metadata-driven error mapper, including the ticket-031-001 `DaemonVersionMismatch` narrowing.
+- **`daemon_client.py`** (~690 lines, sprint 037) — `GrpcDaemon`, the concrete `Daemon`. Builds every outgoing proto request itself (the one client module allowed to touch a request message directly); every response is handed straight to a `models.py` converter, never read here. Owns `_reconstruct_error`, the trailing-metadata-driven error mapper, including the ticket-031-001 `DaemonVersionMismatch` narrowing and (sprint 037) the `NoFieldMask` mapping alongside `NoPlayfieldLinked`'s. Sprint 037 adds `capture_flat_field`/`get_flatfield`/`clear_flat_field`, each a one-RPC-in, one-`models.py`-converter-out method exactly like `calibrate`/`get_calibration`.
 - **`discovery.py`** (487 lines) — `Discovery.find_all()` (mDNS: a local lock/socket probe plus a real bounded `zeroconf.ServiceBrowser` scan, self-filtered) and `Discovery.connect()` (a direct unix/TCP connect that never touches discovery machinery at all).
 - **`lifecycle.py`** (450 lines) — the *client-side* `DaemonLifecycle`: spawns/signals `python -m aprilcam.daemon` as a subprocess, polls the lock file and socket. `status()` additionally runs a best-effort `ps`-based orphan-process scan (ticket 031-002).
-- **`models.py`** (544 lines) — every `from_proto` converter: `camera_state_from_proto`, `tag_record_from_proto`, `playfield_from_proto`, `calibration_from_proto`, `annotation_from_proto`, and friends. Uniform `HasField()` discipline throughout.
+- **`models.py`** (~580 lines, sprint 037) — every `from_proto` converter: `camera_state_from_proto`, `tag_record_from_proto`, `playfield_from_proto`, `calibration_from_proto`, `annotation_from_proto`, and friends — plus (sprint 037) `flatfield_result_from_proto`/`flatfield_info_from_proto`, and `camera_state_from_proto`'s two new `HasField`-guarded fields, `flatfield_present`/`flatfield_stale`. Uniform `HasField()` discipline throughout, unchanged by this addition.
 - **`streams.py`** (214 lines) — `SocketImageStream`/`SocketTagStream`, the concrete `daemon.interfaces.ImageStream`/`TagStream`. Raw length-prefixed sockets (`aprilcam.framing`), decoded via `models.py`'s converters — no second decode path.
 - **`renderer.py`** (592 lines) — `Renderer`, the concrete `client.interfaces.Renderer`. Pillow-only. Per tag (ticket 032-001, replacing the old lime-box/single-hat scheme): an open red (`#FF0000`) three-sided boundary (never the front edge), a green (`#00FF00`) peaked hat closing that open edge (apex at `mid(front) + top_dir * clamp(0.35·|front edge|, 6px, 80px)`), the tag's own id number centred on the boundary's centroid (red fill, black outline via Pillow's `stroke_width`/`stroke_fill`), and a yellow (`#FFFF00`) crosshair at the tag's parallax/mount-corrected `TagRecord.world` (arm length `max(4px, 0.5·px_per_cm)`, skipped when `world` is `None` or the frame is raw). Plus one shape-draw routine per `AnnotationShape` (`Marker`/`PathShape`/`Polygon`/`Circle`/`Text`). Both halves place geometry through the same `calibration.geometry.world_to_deskewed_pixel`. **AprilTag-family only (sprint 033 ticket 001).** `render()`'s per-record loop skips any record whose `tag.family` is not `TagFamily.APRILTAG` before any geometry is computed for it — the field's ArUco 4x4 calibration corner markers are still detected by the daemon (unaffected) but never boxed, hatted, ID-labeled, or crosshaired here. This is one of two independent inline family guards this sprint adds (the other in `view/viewer.py`'s `convert()`) — not a shared exported predicate (sprint 033 sprint.md Design Rationale).
 - **`guides.py`** (48 lines) — `Guides`, reading only `aprilcam.guides` directly: no `Daemon`, no `Discovery`, no network at all.
@@ -73,7 +73,7 @@
 ### Exposes
 
 - **`client.discovery.Discovery`** — `find_all() -> tuple[DaemonInfo, ...]` (empty tuple means genuinely no daemon found; `DiscoveryFailed` means the probe/browse mechanism itself broke) and `connect(host: Optional[str] = None) -> Daemon` (precedence: explicit `host` argument > `APRILCAM_DAEMON_HOST` env > local default; raises `ConnectionFailed` or `DaemonVersionMismatch`).
-- **`client.daemon_client.GrpcDaemon`** — the concrete `daemon.interfaces.Daemon`. Callers obtain it via `Discovery.connect()`.
+- **`client.daemon_client.GrpcDaemon`** — the concrete `daemon.interfaces.Daemon`. Callers obtain it via `Discovery.connect()`. Sprint 037: `capture_flat_field(camera, *, exposure=None, frames=None, force=False, dry_run=False) -> FlatFieldResult`, `get_flatfield(camera) -> Optional[FlatFieldInfo]`, `clear_flat_field(camera) -> None`.
 - **`client.lifecycle.DaemonLifecycle`** (client-side) — `start()`/`stop()`/`status() -> DaemonStatus`, local-machine-only. `status()` additionally carries the orphan-process report in `other_daemon_processes`.
 - **`client.models`** — the full set of `from_proto` converters. In practice only `daemon_client.py` and `streams.py` call into this module.
 - **`client.streams.SocketImageStream` / `SocketTagStream`** — the concrete `ImageStream`/`TagStream`; returned by `GrpcDaemon.stream_images`/`stream_tags`.
```
