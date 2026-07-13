---
status: pending
---

# AprilCam Daemon Architecture

## Pending Issues (to create after sprint close)

- **aprilcam daemon CLI subcommand** — `daemon_cli.py` was never created; the
  `aprilcam daemon` command is missing. Users must run `uv run python -m aprilcam.daemon`
  instead. Add `daemon` subcommand to `src/aprilcam/cli/__init__.py` and create
  `src/aprilcam/cli/daemon_cli.py` as a thin wrapper that calls
  `Config.load()` → `DaemonServer(config).run()`.



## Context

Today, the camera is opened by whichever process gets there first — the
CLI live view ([src/aprilcam/cli/live_cli.py](src/aprilcam/cli/live_cli.py)),
the MCP server's [_handle_open_camera](src/aprilcam/server/mcp_server.py),
the MCP-spawned live-view child in [src/aprilcam/ui/liveview.py](src/aprilcam/ui/liveview.py),
or any of the other ~7 sites that call `cv.VideoCapture`. On macOS AVFoundation
this is exclusive, so two consumers fight for the same physical camera. The
symptom that surfaced this: sprint 001's agent-drawn-paths feature only works
when the MCP server spawned the live view itself — a user-started `aprilcam live`
window can't receive path commands because there's no parent→child OS pipe.

We want a clean separation: one daemon owns the camera and does all per-frame
processing, and everyone else (live viewer, MCP server, web UI, ad-hoc scripts)
is a stateless consumer. Path overlays, debug HUDs, and any other annotations
get drawn by the client, not composited by the daemon.

## Approved Design (from stakeholder)

- **`aprilcamd`** is a long-running process that owns all cameras, runs
  detection, maintains the homography, and publishes a per-frame data stream.
- **One Unix domain socket per camera.** Subscribers connect; daemon fans out.
- **Each socket message contains everything for one frame**: frame image, tag
  records, homography, frame_id, timestamps, plus the **filesystem path to the
  paths file**. Single atomic snapshot — no torn reads.
- **Client-side overlay rendering.** The daemon never composites paths, tag
  boxes, or any decorative pixels. Each viewer draws what it wants on top of
  the published frame.
- **Paths live in a file** announced by the daemon. The MCP server writes the
  file; viewers stat-and-reload on `mtime` change. The daemon never touches it.
- **Daemon is the namespace authority** — it creates `data/runtime/<camera>/`
  and decides where `paths.json` lives. MCP server + viewer learn the location
  from the daemon (info file or socket message), never hardcode it.

## Components

### `aprilcamd` (new)

- New module `src/aprilcam/daemon/` with entry point `aprilcam daemon`.
- One asyncio (or threading) supervisor + one per-camera pipeline thread.
- Reuses **without modification**:
  - [AprilCam.process_frame](src/aprilcam/core/aprilcam.py) for detection
  - [Playfield](src/aprilcam/core/playfield.py) and `PlayfieldBoundary`
  - [load_calibration_for_camera](src/aprilcam/calibration/calibration.py)
- Owns one control socket: `data/runtime/control.sock` (JSON-RPC).
  Commands: `list_cameras`, `open_camera`, `close_camera`, `reload_calibration`,
  `get_camera_info`.
- Per camera at open:
  1. Creates `data/runtime/<cam_name>/`.
  2. Loads calibration via existing loader.
  3. Starts a capture+detect thread.
  4. Writes `data/runtime/<cam_name>/info.json` containing `data_socket`,
     `paths_file`, `device_name`, `homography`, `calibrated`, `frame_size`.
  5. Opens its data socket at `data/runtime/<cam_name>/data.sock`.
- Per frame, atomically broadcasts to all connected subscribers:
  ```
  msgpack({
    "schema": 1,
    "frame_id": int,
    "ts_mono_ns": int,
    "ts_wall_ms": int,
    "frame_jpeg": bytes,      # quality 85, ~150KB at 1280x800
    "frame_w": int, "frame_h": int,
    "tags": [TagRecord, ...],
    "homography": [[...], [...], [...]] | null,
    "playfield_corners": [[x,y], ...],
    "paths_file": "data/runtime/cam_2/paths.json",
    "fps": float,
  })
  ```
- Backpressure: per-subscriber send queue capped at 2 frames; daemon drops
  stale frames for slow consumers rather than blocking the capture loop.

### `aprilcam view` (new — replaces today's live view subprocess)

- New CLI subcommand: `aprilcam view --camera <name>`.
- ~150 lines: connect to `data.sock`, run an OpenCV `imshow` loop.
- Per frame received:
  - JPEG-decode (~5–10ms on a modern Mac with libjpeg-turbo).
  - Stat the published `paths_file`; reload JSON if `mtime` changed.
  - Draw overlays: existing [PlayfieldDisplay.draw_overlays](src/aprilcam/ui/display.py)
    for tag boxes/IDs/velocity, plus the already-built
    [draw_paths](src/aprilcam/ui/display.py) for agent paths.
  - `cv.imshow`.
- Multiple viewers per camera are fine; they all read the same socket and the
  same paths file.

### MCP server (refactored)

- On startup, connects to `data/runtime/control.sock`.
- `open_camera(index)` → control RPC `open_camera`; remember `cam_name` and
  the `info.json` path returned. The MCP `camera_id` becomes `cam_name`
  (already what the registry uses).
- `create_playfield(camera_id)` — daemon detects playfield itself; this tool
  either becomes a "wait for playfield to be detected" query or is removed.
- `start_live_view(camera_id)` — `subprocess.Popen` the viewer client. (Or
  just deprecate it — the human can run `aprilcam view` themselves.)
- `get_tags(source_id)` — reads the daemon's `info.json` (snapshot of latest
  tags) OR connects briefly to `data.sock` for one message.
- **Path tools (`create_path` / `delete_path` / `list_paths` / `clear_paths`)** —
  keep the existing `PathRegistry` and validation logic unchanged. The only
  change to the side effect: instead of `lv.send_command(...)` over the OS
  pipe, atomically rewrite `<paths_file>` (read from the camera's info.json)
  with the current state of `path_registry.list_for(playfield_id)`.

## Files

### New
- `src/aprilcam/daemon/__init__.py`
- `src/aprilcam/daemon/server.py` — supervisor + control socket
- `src/aprilcam/daemon/camera_pipeline.py` — per-camera capture/detect/publish
- `src/aprilcam/daemon/protocol.py` — message schema (msgpack), control commands
- `src/aprilcam/cli/view_cli.py` — `aprilcam view`
- `src/aprilcam/cli/daemon_cli.py` — `aprilcam daemon`

### Modified
- [src/aprilcam/server/mcp_server.py](src/aprilcam/server/mcp_server.py) —
  replace direct `cv.VideoCapture` calls with daemon RPC; replace pipe
  `send_command` calls with `paths.json` rewrites.
- [src/aprilcam/cli/__init__.py](src/aprilcam/cli/__init__.py) — register new
  `daemon` and `view` subcommands.
- `pyproject.toml` — add `msgpack` dependency.

### Deleted
- [src/aprilcam/ui/liveview.py](src/aprilcam/ui/liveview.py) — the entire
  parent-child OS-pipe machinery (`LiveViewProcess`, `_child_main`,
  `_drain_commands`) goes away. `PlayfieldDisplay` stays (used by viewer).
- The sprint-001 IPC additions to `mcp_server.py` (`set_initial_paths`,
  `send_command` plumbing in `_handle_start_live_view`) go away.

## Reuse, don't reinvent

- Detection: `AprilCam.process_frame`
- Tag record schema: existing `TagRecord` / world-coord computation
- Calibration JSON: existing `data/calibration.json` + loader
- Overlay drawing: existing `PlayfieldDisplay.draw_overlays` and `draw_paths`
- Ring buffer: existing `RingBuffer` for tag history (lives in daemon now)
- Path data model: existing `Waypoint` / `Path` / `PathRegistry` from sprint 001

## Performance budget (30fps target)

Daemon pipeline per frame:
- `cap.read()` 5–15ms · detect 5–15ms · JPEG encode ~5ms · socket fan-out <1ms
- Total: 15–35ms. Fits 33ms budget; downscale via existing `proc_width` if not.

Viewer pipeline per frame:
- Socket recv <1ms · JPEG decode 5–10ms · stat paths file ~50µs · reload paths
  (only on mtime change) ~200µs · `draw_overlays` + `draw_paths` ~1ms · imshow ~5ms
- Total: ~10–20ms. Comfortable.

End-to-end "tag moves → screen shows it": ~50–70ms (≈2 frames). Same order
of magnitude as today's parent-child pipe arrangement.

## Out of scope (future work)

- Daemon auto-spawn / launchd integration. For now: `aprilcam daemon` is a
  foreground process you start once; MCP server errors clearly if it's not
  running.
- Multi-host / TCP transport — Unix sockets only.
- Authentication / multi-user.
- Recording / playback through the daemon (already exists via `LoopingVideoCapture`).
- Calibration UI changes — the existing `aprilcam calibrate` writes
  `data/calibration.json`; daemon picks up changes on `reload_calibration` RPC.

## Migration notes

- The sprint-001 `paths.py` + `PathRegistry` + 4 MCP tools all stay; only the
  side effect changes (file write instead of pipe write).
- Existing tests for the path data model (`test_paths.py`,
  `test_mcp_path_tools.py`, `test_draw_paths.py`) keep working.
- `test_live_view_ipc.py` is deleted with the IPC layer.
- The CLI `aprilcam live` can either be aliased to `daemon + view` for
  backward compat, or printed as a deprecation pointing at the new commands.

## Verification

1. `aprilcam daemon` starts, prints control socket path.
2. In another terminal: `aprilcam view --camera cam_2` shows the camera feed
   with tag overlays drawn client-side (proves daemon is publishing).
3. Open a second viewer simultaneously — both show the feed; proves fan-out.
4. From the MCP server: `open_camera(index=2)` returns a camera_id; `get_tags`
   returns the latest detections.
5. `create_path(playfield_id="pf_cam_2", waypoints_json="...")` — both viewers
   redraw with the path within ~33ms (one frame).
6. `delete_path` / `clear_paths` — both viewers update within one frame.
7. Stop and restart `aprilcam view` — paths reappear immediately (proves
   paths-file discovery + reload-on-connect works).
8. Stop and restart the MCP server — daemon keeps publishing; viewers keep
   running; restarted MCP server reads `paths.json` to rebuild its registry
   (or starts empty — decision pending).
9. `uv run pytest` — existing sprint-001 unit tests still pass; new daemon
   tests cover the protocol round-trip and per-frame backpressure.

After this sprint, sprint 001's path drawing works end-to-end with any viewer
running on any camera the daemon owns, and the camera-ownership conflict that
blocked T005 is gone.
