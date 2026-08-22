# client/

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-21 · **Status:** stable

---

## 1. Purpose

`client/` is the shared client library — per its own module docstring, "the only sanctioned way to talk to a daemon." Every component that is not the daemon itself sits on top of it: `cli/`, `mcp/` (the MCP server), `view/` (the live viewer), and — per the system design's process view — future robot programs and a ROS bridge. `docs/design/process.md` requires every daemon capability to be reachable from this Python module first, with the CLI a shell over it and never the reverse.

It owns five things:

- **The gRPC wrapper** (`daemon_client.GrpcDaemon`) — a complete, concrete implementation of `daemon.interfaces.Daemon`'s methods.
- **Daemon discovery** (`discovery.Discovery`) — finding daemons on the network and connecting to a named or local one.
- **Client-side lifecycle** (`lifecycle.DaemonLifecycle`) — starting, stopping, and checking on the *local* daemon process from an outside process (distinct from the daemon's own internal `daemon.lifecycle.DaemonLifecycle`).
- **The proto boundary's wire-to-Python half** (`models.py`'s `from_proto` constructors) — per `docs/design/design.md` §6, "generated proto types cross into Python only via `from_proto` constructors in `client/models.py`." This is that door, and the only one.
- **A client-side rendering library** (`renderer.Renderer`) — rasterizing a served frame's embedded tags/annotations to a PNG, cv2-free.

It also carries the no-daemon-needed `guides.Guides` accessor and the shared `interfaces.py` contracts (`Discovery`, `Guides`, `Renderer` as plain base classes; `DaemonInfo` as a frozen dataclass) that concrete implementations here derive from or construct.

The subsystem boundary is mechanically enforced, not just documented: `client/`, `view/`, and `mcp/` are swept by an AST-based test (`tests/test_no_cv2_in_client_modules.py`) that fails if any of them imports `cv2`. This is what keeps the base `aprilcam` package pipx-installable with only Pillow/NumPy/mss/python-dotenv — OpenCV stays a `daemon`-only extra — and it is the concrete expression of the architectural principle "single vision authority": nothing under `client/` may open a camera or do vision work; it only talks to the daemon that does.

## 2. Orientation

Eight non-empty files, ~2885 lines:

- **`interfaces.py`** (156 lines) — `DaemonInfo` (frozen `@dataclass(frozen=True, kw_only=True)`: `host`, `port`, `is_local`, `reachable`), directly constructed rather than built via a separate implementation class. `Discovery`, `Guides`, `Renderer` are plain base classes (`NotImplementedError` bodies, never `typing.Protocol`) that this package's concrete classes derive from. `ImageStream`/`TagStream` deliberately do *not* live here — they're declared in `daemon/interfaces.py` next to the `Daemon` methods that return them, since unsubscription is a daemon-visible event.
- **`daemon_client.py`** (633 lines) — `GrpcDaemon`, the concrete `Daemon`. Builds every outgoing proto request itself (the one client module allowed to touch a request message directly); every response is handed straight to a `models.py` converter, never read here. Owns `_reconstruct_error`, the trailing-metadata-driven error mapper, including the ticket-031-001 `DaemonVersionMismatch` narrowing.
- **`discovery.py`** (487 lines) — `Discovery.find_all()` (mDNS: a local lock/socket probe plus a real bounded `zeroconf.ServiceBrowser` scan, self-filtered) and `Discovery.connect()` (a direct unix/TCP connect that never touches discovery machinery at all).
- **`lifecycle.py`** (450 lines) — the *client-side* `DaemonLifecycle`: spawns/signals `python -m aprilcam.daemon` as a subprocess, polls the lock file and socket. `status()` additionally runs a best-effort `ps`-based orphan-process scan (ticket 031-002).
- **`models.py`** (544 lines) — every `from_proto` converter: `camera_state_from_proto`, `tag_record_from_proto`, `playfield_from_proto`, `calibration_from_proto`, `annotation_from_proto`, and friends. Uniform `HasField()` discipline throughout.
- **`streams.py`** (214 lines) — `SocketImageStream`/`SocketTagStream`, the concrete `daemon.interfaces.ImageStream`/`TagStream`. Raw length-prefixed sockets (`aprilcam.framing`), decoded via `models.py`'s converters — no second decode path.
- **`renderer.py`** (353 lines) — `Renderer`, the concrete `client.interfaces.Renderer`. Pillow-only; draws tag boundary quads/"hats" from `TagRecord` corners and one shape per `AnnotationShape` (`Marker`/`PathShape`/`Polygon`/`Circle`/`Text`), all through `calibration.geometry.world_to_deskewed_pixel`.
- **`guides.py`** (48 lines) — `Guides`, reading only `aprilcam.guides` directly: no `Daemon`, no `Discovery`, no network at all.
- **`__init__.py`** — empty.

Rough call shape: an adapter constructs a `Discovery`, calls `.connect(host)` to get a `Daemon` (in practice always a `GrpcDaemon`), and calls methods on it. `GrpcDaemon.stream_images`/`stream_tags` hand off to `streams.py` to open a second, raw socket. `view/viewer.py` is the one place that also constructs a `Renderer` to turn a served `ImageFrame` into pixels for display. `cli/guide.py` and `cli/daemon.py`/discovery-adjacent commands are the ones that reach for `guides.py`/`lifecycle.py` without necessarily going through `Discovery.connect()` first.

## 3. Constraints and Invariants

- **cv2-free, mechanically gated.** `tests/test_no_cv2_in_client_modules.py` AST-scans `client/`, `view/`, and `mcp/` for any `cv2`/`cv2.*` import and fails the suite if found. Violating this breaks the pipx base-install promise and the single-vision-authority principle simultaneously.
- **Proto types cross the boundary in one direction, through one door.** Only `client/models.py`'s `from_proto` functions read a response proto message's fields. `daemon_client.py` builds *requests* directly (that is the outgoing direction, which the rule does not restrict) but never reads a response field itself. `streams.py` decodes its own response sub-message type via the same `models.py` converters — it does not invent a second decode path. Consequence of violating this: a second, drifting copy of the wire-to-Python mapping, and — since `calibration_from_proto` deliberately builds the narrower `calibration.interfaces.Calibration` rather than the concrete `calibration.calibration.Calibration` (which imports `cv2` at module scope) — a stray direct read could silently reintroduce a `cv2` dependency into every module that transitively imports `client/models.py`, including all of `mcp/`.
- **`HasField()` discipline, never coerced.** Every optional scalar or singular message field (`playfield`, `TagRecord.world`/`heading_rad`/`world_velocity`/`speed`/`yaw_rad`, `Calibration.reason`, etc.) round-trips through `HasField()` — `None` on the Python side iff absent on the wire, never defaulted to `0.0`/empty. Violating it collapses "not detected" into "detected at world (0, 0)" — the exact failure UC-09.S-4 exists to prevent. `world_corners`/`pixel_corners` are the one documented exception: proto3 disallows `optional repeated`, so an empty `repeated` is the unambiguous "absent" encoding there instead.
- **Discovery vs. connect are structurally separate.** `find_all()` is a full network operation (browse + reachability probes) that can fail or take up to `_DEFAULT_BROWSE_TIMEOUT` (2s). `connect(host)` never calls `find_all()` or the browse step — it is unaffected by a broken multicast stack, which is what lets `-d HOST` work fast and reliably regardless of discovery health.
- **Client-side `DaemonLifecycle` is local-only, structurally, not by convention.** It takes no host parameter anywhere in its constructor or methods. Refusing `aprilcam daemon start -d remote` is entirely the CLI's job (`cli/daemon.py`'s `_refuse_remote`).
- **Orphan detection is report-only — never kill, never signal.** `client/lifecycle.py`'s `_list_daemon_processes`/`_scan_orphan_processes` only ever *report* pids; nothing in the codebase signals a pid found solely by this scan. The scan is best-effort by contract: any exception the injected seam raises is caught and degrades silently to an empty result rather than failing `status()`.
- **Streaming `close()` unsubscribes implicitly, never via RPC.** Per `daemon/interfaces.py`'s `ImageStream`/`TagStream` docstrings, closing the client-side socket is the entire unsubscribe mechanism — the daemon's own sender thread notices the disconnect on its next write attempt.
- **Stream iteration trap (documented, not fixed).** Each call to `SocketImageStream.__iter__`/`SocketTagStream.__iter__` returns a *new* generator sharing the same socket, and that generator's `finally` closes the socket — including when the generator is merely garbage-collected. A `for frame in stream:` loop is safe; a throwaway `next(iter(stream))` with no reference kept to the intermediate iterator closes the connection immediately after one frame.
- **The renderer never touches `Calibration` or a homography.** It converts world -> pixel using only the frame's own published `px_per_cm`/`world_extent` metadata via `calibration.geometry.world_to_deskewed_pixel` — the same function `calibration/deskew.py` uses to build the warp itself, imported (not re-derived) so both sides share one formula.
- **Error mapping is closed and typed, never string-matched.** `_reconstruct_error` maps gRPC trailing metadata to one of a fixed set of `aprilcam.errors` types; any caller catching a specific type is guaranteed the same exception this project's daemon-side `_abort` contract produced.
- **Whole-document config pushes only.** `set_playfield`/`set_camera_config` send complete documents; there are no field-level edit RPCs to build partial requests for.

## 4. Design

**Key structures and ownership.** `GrpcDaemon` is stateless beyond `(stub, target)` — no caching, no retry logic of its own; each call is a synchronous RPC through the grpc stub, mapped through `_reconstruct_error` on failure. `Discovery` holds its DI seams (`lock_holder`, `socket_reachable`, `browse`, `connect_timeout`) and a `Directories` for path resolution, nothing else persistent. `DaemonInfo` and `DaemonStatus` are the two value types crossing this boundary; both are frozen dataclasses, directly constructed (no parallel sibling classes remain).

**Version-mismatch detection (ticket 001).** `daemon_client._reconstruct_error(exc, target)` reads `exc.trailing_metadata()` into a dict and looks for the `aprilcam-error-type` key the server-side `_abort` contract sets. If that key is *absent* and the gRPC status code is `UNIMPLEMENTED`, the failure is reclassified as `aprilcam.errors.DaemonVersionMismatch(target)` rather than falling through to the generic `ConnectionFailed`. The reasoning recorded in the code: a v1 daemon sharing this project's conventional control-plane socket path with a v2 daemon implements none of `aprilcam_v2.proto`'s RPCs, so the first RPC a fresh v2 client makes comes back `UNIMPLEMENTED`/"Method not found!" with no `aprilcam-error-type` metadata at all, since v1 predates that contract. This is a narrower classification carved out of the existing fallback — any other unclassified failure (e.g., `UNAVAILABLE` from a dead socket) still falls through to `ConnectionFailed` unchanged. The distinction matters operationally: `ConnectionFailed` means "nothing answered"; `DaemonVersionMismatch` means "something answered, but it isn't a compatible daemon."

**Orphan-process detection (ticket 002).** `client.lifecycle.DaemonLifecycle.status()` always runs `_scan_orphan_processes(holder_pid)`, regardless of whether the lock is currently held, via the injectable `list_daemon_processes` seam. The default shells out to `ps -eo pid=,args=` (not `/proc`, since this must run on macOS dev machines as well as Raspberry Pi) with a 5s timeout, `shell=False`, and never raises on failure — it logs and returns `()`. Matching is by command-line regex against every process, then the current lock holder's own pid (if any) is excluded from the result. The result populates `DaemonStatus.other_daemon_processes` — this is the *only* call site in the codebase that ever fills that field with a real scan; every RPC-sourced `DaemonStatus` always reports it empty, since a process-table scan has no meaning against a remote host. The gap this closes: a process that is alive but holds no lock at all is invisible to flock-based detection alone. `cli/daemon.py`'s `_print_status` surfaces the result as an "orphan processes (local scan, not the lock holder, not killed)" line, printed only when non-empty.

**Concurrency: stream consumption.** `SocketImageStream`/`SocketTagStream` are single-reader, generator-based wrappers over one raw socket each, opened unix-preferred when `host` is local or TCP otherwise. Reading uses `aprilcam.framing.read_frame`, which raises `EOFError` on a clean close or short read; `__iter__` catches exactly that and ends the generator normally rather than surfacing a clean daemon-side disconnect as a crash.

**Rejected alternatives recorded in the code:**
- Discovery's local-only mDNS stub (sprint 020) was a deliberate, honestly-scoped placeholder — kept until sprint 028 added the real `zeroconf.ServiceBrowser` scan.
- The 2-second default browse timeout was set well above the measured ~0.01s loopback-multicast round trip specifically to survive a slow interface or loaded CI runner while still keeping `find_all()` bounded.
- `discovery.py`'s `_SERVICE_TYPE` is duplicated, not imported, from `daemon/mdns.py`'s private constant — a deliberate "two independent consumers of `zeroconf`" dependency-direction choice over adding a `client/`<->`daemon/` coupling edge.
- The daemon-readiness race was fixed in `daemon/lifecycle.py` (reordering enumeration before the listener binds) rather than by adding a richer client-side polling heuristic here.
- `client.lifecycle.DaemonLifecycle.start()` pre-flight-probes the lock before spawning, rather than "spawn and see," specifically so a contended start never touches the process table of an already-running daemon (UC-01.S-3).
- `calibration_from_proto` intentionally narrows to `calibration.interfaces.Calibration` rather than round-tripping every field the wire message carries — those are daemon-internal solve details no client-side consumer needs, and building the interface type instead of the concrete one is what keeps this module, and everything that imports it, `cv2`-free.

## 5. Interfaces

### Exposes

- **`client.discovery.Discovery`** — `find_all() -> tuple[DaemonInfo, ...]` (empty tuple means genuinely no daemon found; `DiscoveryFailed` means the probe/browse mechanism itself broke) and `connect(host: Optional[str] = None) -> Daemon` (precedence: explicit `host` argument > `APRILCAM_DAEMON_HOST` env > local default; raises `ConnectionFailed` or `DaemonVersionMismatch`).
- **`client.daemon_client.GrpcDaemon`** — the concrete `daemon.interfaces.Daemon`. Callers obtain it via `Discovery.connect()`.
- **`client.lifecycle.DaemonLifecycle`** (client-side) — `start()`/`stop()`/`status() -> DaemonStatus`, local-machine-only. `status()` additionally carries the orphan-process report in `other_daemon_processes`.
- **`client.models`** — the full set of `from_proto` converters. In practice only `daemon_client.py` and `streams.py` call into this module.
- **`client.streams.SocketImageStream` / `SocketTagStream`** — the concrete `ImageStream`/`TagStream`; returned by `GrpcDaemon.stream_images`/`stream_tags`.
- **`client.renderer.Renderer`** — `render(frame: ImageFrame, *, layers: Optional[tuple[str, ...]] = None, extra: tuple[Annotation, ...] = ()) -> bytes` (PNG). Used by `view/viewer.py`.
- **`client.guides.Guides`** — `get(name: Optional[str] = None) -> str` / `available() -> tuple[str, ...]`. Needs no daemon connection at all.
- **`client.interfaces`** — `Discovery`, `Guides`, `Renderer` base classes and the `DaemonInfo` value type.

### Consumes

By reference only, per this codebase's proto-boundary and interface-inheritance conventions:

- `aprilcam.daemon.interfaces` — `Daemon`, `DaemonStatus`, `MountRegistration`, `ImageStream`, `TagStream`, `DaemonLifecycle` base classes.
- `aprilcam.proto` (`aprilcam_v2_pb2`, `aprilcam_v2_pb2_grpc`) — the generated wire contract; request-building lives in `daemon_client.py` alone, response-reading in `models.py` alone.
- `aprilcam.config.interfaces` / `aprilcam.config.loader` / `aprilcam.config.directories` — `CameraConfig`, `CameraPosition`, `Feature`, `PlayfieldDefinition` value shapes; `Directories` for lock/socket path resolution; `DEFAULT_DAEMON_PORT`.
- `aprilcam.calibration.interfaces` — `Calibration`, `CalibrationResult` (the interface module only, never `calibration.calibration`, to stay `cv2`-free).
- `aprilcam.playfield.interfaces` / `aprilcam.playfield.playfield` — `Playfield`.
- `aprilcam.types` — the full shared value vocabulary.
- `aprilcam.errors` — the typed exception taxonomy raised across the client/daemon boundary.
- `aprilcam.framing` — the length-prefixed socket read/write helpers shared with `daemon/stream_transport.py` on the server side.
- `aprilcam.guides` — the packaged guide text.
- `zeroconf`, `grpc`, `PIL` (third-party) — mDNS browse, channel/stub mechanics, the renderer's decode/draw path.

## 6. Open Questions / Known Limitations

- **`client.lifecycle.DaemonLifecycle.status()` swallows `DaemonVersionMismatch` back into a generic "running" result.** Its RPC-answered branch does `except AprilCamError: return DaemonStatus(running=True, cameras=(), ...)` — a broad catch that also covers `DaemonVersionMismatch`, a subclass of `AprilCamError`. So a bare `aprilcam daemon status` against a stray v1 daemon holding the local lock reports "running" with zero cameras, structurally identical to any other unreachable-but-locked case, even though ticket 001 exists specifically to distinguish that scenario. The distinction *does* surface for `aprilcam daemon status -d HOST` (and `daemon list`), which call `Discovery().connect(host).status()` directly and let the specific exception propagate. This asymmetry between the local and RPC status paths is not called out anywhere in the code as a deliberate decision.
- **`mcp/connection.py`'s `ConnectionManager` cache-evicts only on `ConnectionFailed`, not `DaemonVersionMismatch`.** A target that resolves once and then starts failing with a version mismatch keeps its cached (still-connected) `GrpcDaemon` entry; every subsequent call re-raises the same `DaemonVersionMismatch` rather than the cache being cleared.
- **`client/interfaces.py`'s module docstring is now stale.** It states `Guides` and `Renderer` are "behavioral, so they are plain base classes; neither has a concrete implementation yet to derive from (out of this pass's scope)." Both now have concrete, derived implementations. Not a functional bug, but worth a follow-up doc pass.
- **Discovery's local lock-probe cannot itself detect a version mismatch.** `find_all()`'s local entry reports `reachable` purely from a raw gRPC channel-ready check, which succeeds against any listener, v1 or v2. A stray v1 daemon holding the local lock is listed as a normal, reachable local daemon by `find_all()`/`daemon list`; the mismatch is only discoverable once something actually issues an RPC through `connect()`.
- **No reconnect, backpressure, or multi-consumer fan-out inside `streams.py`.** `SocketImageStream`/`SocketTagStream` are strictly single-reader, no-retry wrappers; anything wanting automatic reconnect-on-drop or multiplexing one subscription to several local consumers builds it on top.
- **Orphan-process scanning is Unix-`ps`-only.** No Windows implementation exists or is claimed; the default seam shells out to `ps -eo pid=,args=` and would need a different default on any platform lacking it.
- **`Discovery.connect()`'s host-resolution precedence has no "file" tier yet** — only explicit argument and `APRILCAM_DAEMON_HOST` env, pending a future daemon-target configuration document.
