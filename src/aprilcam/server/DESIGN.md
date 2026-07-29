# Server (MCP Server + Web Server)

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** in-flux

---

## 1. Purpose

The agent-facing surface of AprilCam: a FastMCP stdio server (`mcp_server.py`) that lets AI agents perceive and query the playfield through tool calls, plus a Starlette HTTP/WS bridge (`web_server.py`). The seam is *protocol adaptation*: the daemon owns cameras and vision; this subsystem owns nothing physical — it translates MCP tool calls (and HTTP requests) into daemon RPCs, maintains the per-session handle bookkeeping (camera_id, playfield_id, path_id, frame_id) that agents need, and enforces the "handles, never file paths" contract for MCP clients. It alone owns the agent conversation model: golden-path instructions, error-dict conventions, and the session registries.

## 2. Orientation

`mcp_server.py` (~3,800 lines) declares one `FastMCP("aprilcam")` instance and **44 tools**. Most tools are thin proxies: they resolve a handle to a daemon `cam_name`, call a `DaemonControl` RPC, and reshape the reply into JSON. A lazily connected module-level `DaemonControl` singleton (`_ensure_daemon_client`) is shared by every handler. Six module-level registries hold session state: `CameraRegistry` (handle → sentinel; the daemon holds the real capture), `PlayfieldRegistry` (`PlayfieldEntry`: homography, FieldSpec, polygon, tag-1 origin), `PathRegistry` (`paths.py`), `FrameRegistry` (`frame.py`), `detection_registry` (per-source `TagStreamConsumer` + reader thread + 300-frame deque), and `live_view_registry` (spawned-viewer sentinels). A `PlayfieldDefinitionRegistry` mirrors the daemon's playfield definitions, populated over the `ListPlayfields` RPC and kept fresh by a `boot_id` comparison.

A minority of tools do **local OpenCV work** in the MCP process (calibration, static-image playfield/deskew, the frame-processing pipeline) via lazy `_require_cv2()` imports. `web_server.py` is a separate `create_app()` factory: seven routes each mapping 1:1 onto one daemon RPC, plus a `/ws/tags` WebSocket that relays the daemon tag stream, and an embedded single-page HTML UI. `paths.py` holds the path data model; `frame.py` the frame ring buffer. Entry points: `aprilcam mcp` → `mcp_server.main()` (stdio); `aprilcam web` → `web_server.create_app()` under uvicorn.

## 3. Constraints and Invariants

- **The daemon is the sole vision authority; this server must not process pixels** — the local-cv2 paths that remain (see §6) are acknowledged violations, not precedent. New tools that decode/warp/detect locally re-break the cv2-free base install and duplicate daemon logic.
- **MCP clients never see filesystem paths; handles only** — the `format="file"` outputs and the `*_from_image`/`save_frame` path parameters are legacy exceptions slated for removal. Telling an agent to touch a file breaks remote-daemon deployments where the file is on another machine.
- **All state is in-process and lost on restart** — the server instructions explicitly tell agents to re-run the golden path (`open_camera` → `create_playfield`) every session. Nothing here may be treated as durable; durable state lives behind daemon RPCs (`SetCalibration`, `SetCameraConfig`, `SetPaths`).
- **Tool error contract: return `{"error": "..."}`, never raise** — a raised exception surfaces as an MCP protocol error and derails agent loops. Every `@server.tool()` body is wrapped.
- **A1-centred origin shift is a pure subtraction on both axes, never a y-flip** (`_a1_coord_transform`, `_handle_pixel_to_world`) — a stale `origin_y - y` once inverted north/south. `+y` is north.
- **Path colors are RGB in the data model; BGR conversion happens only at the draw site** (`paths.py` module contract). Converting earlier double-swaps channels in the viewer.
- **`_maybe_repopulate_registry` must run on every daemon call path, and must be a no-op when `boot_id` is unchanged** — repopulating on every gRPC blip clobbers a correct registry; never repopulating serves stale playfields after `aprilcamd` restarts under a long-lived MCP session. A pre-`boot_id` daemon reports `""`; the empty-registry retry in `create_playfield`/`calibrate_playfield`/`set_camera_playfield` is the compatibility floor.
- **`main()` uses `add_help=False` and `parse_known_args`** — argparse must never write to stdout/exit, because stdout is the MCP wire.
- **The persistent enumeration number, not the OS probe index, is the user-facing camera `index`** — surfacing the volatile OS index breaks handle stability across replug.

## 4. Design

**Daemon client lifecycle.** First tool call triggers `DaemonControl.connect_default` with CLI `--daemon-host/--daemon-port` overrides folded into a proxy namespace. Every subsequent `_ensure_daemon_client()` call piggybacks a `ListPlayfields` probe: `_maybe_repopulate_registry` compares the reply's `boot_id` (daemon `uuid4().hex`, minted at startup) to the last-seen value and re-runs `_on_daemon_connect` (clear + reload `playfield_def_registry`) only on change. This distinguishes a daemon restart from a transport reconnect (Sprint 016).

**Handles.** `open_camera` resolves index/pattern through the daemon's `EnumerateCameras`, calls `OpenCamera`, and registers a `None` sentinel under the daemon's `cam_name` — the handle *is* the cam_name. It then rehydrates a `PlayfieldEntry` (`pf_<cam>`) entirely over RPC (`GetCameraConfig` → linked playfield slug → `GetCalibration` → homography + staleness check), so a calibrated camera yields world coordinates with no extra tool calls. `create_playfield` falls back from live ArUco corner detection to the stored calibration (corners projected via H⁻¹). World-coordinate tools accept either handle: `_lookup_playfield_for_source` resolves camera_id → its playfield.

**Detection streams.** `start_detection`/`stream_tags` subscribe to the daemon's `GetTagStream` socket; a daemon thread drains `TagFrame`s into a `deque(maxlen=300)` (OverlayFrames skipped). `get_tags` reads `history[0]`, or falls back to the one-shot `GetTags` RPC when no stream is running; `get_tag_history` serves the deque. Optional robot-tag support computes `gripper_world_xy` by projecting the tag's forward direction through the homography.

**Paths.** `create_path` validates waypoints (8-symbol set, RGB colors), stores them in the thread-safe `PathRegistry` (monotonic `path_000…` ids, no eviction), then `_push_paths_rpc` serialises the playfield's full path list and pushes it whole via the `SetPaths` RPC — the daemon writes `paths.json`, which the viewer polls. Push failures are silently swallowed (best-effort mirror).

**Calibration (local-cv2).** `calibrate_playfield` runs `calibrate_from_playfield_def` *in the MCP process*, feeding it a `_DaemonCap` adapter that mimics `cv2.VideoCapture` over the `CaptureFrame` RPC, then persists via `SetCalibration`/`SetCameraConfig` RPCs (`skip_save=True`; the daemon writes the files). Correct results, wrong process: this is the flagship vision-boundary violation.

**Frame pipeline (local-cv2).** `FrameRegistry` (capacity 300, oldest-first eviction, `frm_NNN` ids) stores `FrameEntry`s with three promotion slots — `original`/`deskewed`/`processed` start as one shared array (zero copy); `deskew` replaces slots 2–3, detection ops write `results` without touching pixels. `run_operations` dispatches `deskew`/`detect_tags`/`detect_aruco`.

**Live view.** `start_live_view` spawns `python -m aprilcam view <cam>` as a detached (`start_new_session=True`) subprocess, forwarding `--daemon-host/--daemon-port` when TCP-connected, and records a handle-less sentinel. `stop_live_view` therefore cannot terminate the process — it only clears the sentinel; the viewer exits when its stream socket drops. `set_live_overlay`/`clear_live_overlay` proxy `PublishOverlay`.

**Web server.** `create_app()` connects one `DaemonControl` in the lifespan (explicit coords or `resolve_daemon_target`), stores it in `app.state`, and degrades to 503s if unreachable. `/ws/tags` runs the blocking `TagStreamConsumer.read()` in a thread executor per message so the event loop never blocks; only `TagFrame`s are forwarded as JSON.

## 5. Interfaces

### Exposes

**MCP tools (44), stdio transport** — all return `TextContent` JSON (or `ImageContent`), errors as `{"error": ...}`:

- *Session/meta*: `get_version`, `connect_daemon` (explicit reconnect + registry repopulate), `get_robot_api_guide`; MCP resources `aprilcam://docs/robot-api`, `aprilcam://docs/agent-guide`.
- *Cameras*: `list_cameras` (persistent enum as `index`), `open_camera` (→ `camera_id`, plus `playfield_id`/`playfield_name`/`calibration_stale` when rehydrated), `capture_frame`, `close_camera`, `set_camera_playfield` (read-modify-write of camera config via RPC).
- *Playfields*: `create_playfield`, `get_playfield_info`, `calibrate_playfield`, `list_playfields`, `get_playfield`, `pixel_to_world`, `where` (keyword search; `needs_resolution` hands the full map back for LLM disambiguation); local-cv2: `create_playfield_from_image`, `deskew_image`.
- *Detection*: `start_detection`/`stop_detection`, `stream_tags`/`stop_stream` (same machinery, preferred names), `get_tags`, `get_tag_history`, `get_objects`.
- *Mobile tags*: `register_mobile_tag`, `clear_mobile_tags`, `list_mobile_tags` (daemon-persisted registry).
- *Frames*: `get_frame` (raw JPEG passthrough; `annotate` accepted but ignored); local-cv2 registry: `create_frame`, `create_frame_from_image`, `process_frame`, `get_frame_image`, `save_frame`, `release_frame`, `list_frames`.
- *Paths*: `create_path`, `delete_path`, `list_paths`, `clear_paths`.
- *Live view*: `start_live_view`, `stop_live_view`, `set_live_overlay`, `clear_live_overlay`.

**HTTP/WS** (`web_server.create_app`): `GET /` (HTML UI or JSON discovery, content-negotiated), `POST /api/list_cameras|tags|objects|where|overlay`, `GET /api/frame` (JPEG passthrough), `WS /ws/tags?source_id=`. Each is one daemon RPC; no pixel work.

**Python**: `format_image_output`, `run_operations`, `FrameRegistry`, `PathRegistry`, `Waypoint`/`Path`, `VALID_SYMBOLS` (used by tests and the tool layer).

### Consumes

- `client/` — `DaemonControl` (all RPCs), `TagStreamConsumer`, `TagFrame`/models, `resolve_daemon_target`. Owning subsystem: client.
- `core/` — `PlayfieldBoundary` (corner detection/polygon), `PlayfieldDefinitionRegistry`, `playfield_query` (where), `AprilTag`. Owning: core (v2: vision/daemon).
- `calibration/` — `calibrate_from_playfield_def`, `FieldSpec`, `CameraCalibration`, geometry helpers (local-cv2 paths only). Owning: calibration.
- `camera.camera_config.parse_camera_config` — config-dict parsing. Owning: camera.
- `config.Config` — directory/env resolution. Owning: top-level config.

## 6. Open Questions / Known Limitations

- **Local-cv2 paths violate the vision boundary** — `calibrate_playfield`, `create_playfield_from_image`, `deskew_image`, the whole frame registry (`create_frame`…`list_frames`), and `format_image_output`. **v2: all removed** (spec §4.7, §6); consumers needing pixel work call `get_frame` and process the JPEG themselves; calibration becomes a daemon-side `Calibrate` RPC with this server as pure orchestrator (spec §4.4).
- **Paths-as-a-service is removed in v2** — `paths.py`, `PathRegistry`, the four path tools, `SetPaths`/`GetPaths` proxying, and `set_live_overlay`/`clear_live_overlay`/`PublishOverlay` all go; drawing becomes the client-side library (spec §4.6, §6, §9.2).
- **`web_server.py` is removed entirely in v2** (spec §6) — consumers are MCP, the viewer, robots via the client library, and a future ROS bridge.
- **v2 renames the package `server/` → `mcp/`** and adds whole-document configuration tools (get/set playfield, get/set/reset camera config) per spec §4.7; the deskewed-frame tool consumes the new daemon deskew stream (spec §4.1).
- **`stop_live_view` cannot stop the viewer** (detached, no handle); the `main()` shutdown path even calls `entry.process.stop()` on a `None` process (harmlessly swallowed). v2's `view/` subsystem should own viewer lifecycle.
- **Stale docstrings**: `stream_tags` still describes in-process "AprilCam + DetectionLoop + RingBuffer" machinery that was removed; the module docstring says paths are written "atomically to paths.json" locally when they actually go via the `SetPaths` RPC.
- **`_handle_where` and `_handle_get_playfield` retain legacy local-file fallbacks** (`data/aprilcam/playfield.json`) that assume the daemon's disk is local — wrong for remote daemons; superseded by the registry path and removable once migration is universal.
- **Registry loss on restart is by design but costs agents a re-setup round trip** every session; v2 keeps this (the daemon holds all durable state).
