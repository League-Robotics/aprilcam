---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 002 Use Cases

---

## SUC-001: Client Auto-Spawns Daemon on First Use

- **Actor**: Any AprilCam client (MCP server, `aprilcam view`, calibration CLI)
- **Preconditions**: No `aprilcamd` process is running; the control socket does
  not exist.
- **Main Flow**:
  1. Client calls `ensure_running(config)`.
  2. `ensure_running` checks for the control socket — not found.
  3. `ensure_running` takes an exclusive flock on the spawn lock file.
  4. `ensure_running` re-checks the socket (another client may have raced).
  5. Socket still absent; client forks the daemon via `subprocess.Popen`
     (detached, `start_new_session=True`).
  6. Client polls the socket every 50ms for up to 5 seconds.
  7. Daemon binds the socket; client connects.
  8. Client releases the spawn lock; returns a connected `ControlClient`.
- **Postconditions**: Exactly one `aprilcamd` process is running; the client
  holds a `ControlClient`.
- **Acceptance Criteria**:
  - [ ] First client to call `ensure_running()` with no daemon running results
        in exactly one daemon process.
  - [ ] Two clients calling `ensure_running()` concurrently still produce
        exactly one daemon (spawn-lock prevents race).
  - [ ] If the daemon does not appear within 5 seconds, `ensure_running()`
        raises a clear error.
  - [ ] Subsequent calls when the daemon is already running return immediately
        (no re-spawn).

---

## SUC-002: Daemon Opens Camera and Publishes Frame Stream

- **Actor**: MCP server (via `ControlClient` RPC)
- **Preconditions**: Daemon is running; camera hardware is available.
- **Main Flow**:
  1. MCP server sends `open_camera(index)` RPC to daemon.
  2. Daemon opens `cv.VideoCapture(index)`, assigns a `cam_name`.
  3. Daemon creates `<data_dir>/<cam_name>/`, loads calibration if present,
     starts the capture+detect pipeline thread.
  4. Daemon writes `<data_dir>/<cam_name>/info.json` (data socket path,
     paths file path, frame size, homography).
  5. Daemon opens the per-camera data socket.
  6. Daemon returns `cam_name` and `info.json` path in the RPC response.
  7. Each captured frame is JPEG-encoded, merged with tag records and
     metadata, and fanned out to all connected subscribers.
- **Postconditions**: Camera is open; info.json is on disk; data socket is
  accepting subscriber connections; frame messages are being published.
- **Acceptance Criteria**:
  - [ ] `open_camera` RPC returns `cam_name` and `info.json` path.
  - [ ] `info.json` contains `data_socket`, `paths_file`, `frame_size`,
        `homography` (null if uncalibrated), `calibrated`.
  - [ ] Data socket is accessible at the advertised path.
  - [ ] A subscriber connecting to the data socket receives frame messages.
  - [ ] Only one `cv.VideoCapture` per camera is ever open system-wide.

---

## SUC-003: Multiple Viewers Receive the Same Frame Stream

- **Actor**: Human user running multiple `aprilcam view` windows
- **Preconditions**: Daemon is running; at least one camera is open.
- **Main Flow**:
  1. First viewer connects to the camera's data socket.
  2. Second viewer connects to the same data socket.
  3. Each captured frame is delivered to all connected subscribers.
  4. Either viewer may disconnect at any time without affecting the other.
- **Postconditions**: Both viewers show the same live feed independently.
- **Acceptance Criteria**:
  - [ ] Two simultaneous viewers both display the live feed.
  - [ ] Disconnecting one viewer does not interrupt the other.
  - [ ] A slow viewer's send queue is capped at 2 frames; stale frames are
        dropped rather than blocking the capture loop.
  - [ ] A new viewer connecting while a first is running receives the stream
        immediately.

---

## SUC-004: MCP Path Tools Write paths.json; Viewers Reload on Change

- **Actor**: AI agent (via MCP `create_path`, `delete_path`, `clear_paths`)
- **Preconditions**: Daemon is running; camera is open; at least one
  `aprilcam view` window is connected.
- **Main Flow**:
  1. Agent calls `create_path(playfield_id, waypoints_json)`.
  2. MCP server validates input, updates `PathRegistry`.
  3. MCP server reads `paths_file` path from the camera's `info.json`.
  4. MCP server atomically rewrites `paths.json` with the current registry
     state for that playfield.
  5. On the next frame, each viewer stats the `paths_file`; detects mtime
     change; reloads and re-renders.
- **Postconditions**: All viewers show the updated path overlay within ~33ms.
- **Acceptance Criteria**:
  - [ ] `create_path` results in an updated `paths.json` within the same
        request.
  - [ ] Both running viewers re-render the new path within approximately
        one frame interval.
  - [ ] `delete_path` and `clear_paths` follow the same file-write pattern.
  - [ ] `paths.json` is valid JSON after every write.
  - [ ] Atomic write (write-then-rename) prevents torn reads by viewers.

---

## SUC-005: Viewer Displays Tag Overlays and Paths on Each Frame

- **Actor**: Human user watching `aprilcam view`
- **Preconditions**: Viewer is connected to a camera's data socket; daemon is
  publishing frames.
- **Main Flow**:
  1. Viewer receives a frame message from the data socket.
  2. Viewer JPEG-decodes the frame image.
  3. Viewer stats `paths_file`; reloads if mtime changed.
  4. Viewer calls `PlayfieldDisplay.draw_overlays()` for tag boxes, IDs,
     velocity vectors.
  5. Viewer calls `draw_paths()` for agent-defined waypoint paths.
  6. Viewer calls `cv.imshow()` with the composited frame.
- **Postconditions**: Display reflects the latest frame with all overlays.
- **Acceptance Criteria**:
  - [ ] Tag boxes and IDs are drawn correctly for detected tags.
  - [ ] Agent-drawn paths appear with correct symbols, colors, and sizes.
  - [ ] Paths are drawn client-side — the daemon never composites pixels.
  - [ ] If `paths_file` does not exist or is empty, the viewer renders
        without paths (no crash).
  - [ ] If homography is null, `draw_paths` is a no-op (no crash).

---

## SUC-006: Viewer Auto-Discovers Daemon and Camera on Startup

- **Actor**: Human user running `aprilcam view --camera <name>`
- **Preconditions**: May or may not have a daemon running.
- **Main Flow**:
  1. `aprilcam view` calls `ensure_running(config)` (spawns daemon if needed).
  2. Viewer sends `open_camera` RPC if camera is not already open, or
     queries `get_camera_info` if it is.
  3. Viewer reads `info.json` to get the data socket path and paths file path.
  4. Viewer connects to the data socket and enters the display loop.
  5. On startup, viewer loads `paths_file` (if it exists) to display
     pre-existing paths immediately.
- **Postconditions**: Viewer is displaying the live feed with any pre-existing
  paths visible from the first frame.
- **Acceptance Criteria**:
  - [ ] `aprilcam view --camera cam_2` with no daemon running succeeds
        (auto-spawn works end-to-end).
  - [ ] Pre-existing paths in `paths.json` appear on the very first frame.
  - [ ] Restarting the viewer restores the same path display (daemon and
        `paths.json` survived).

---

## SUC-007: Configuration Loaded from Dotfile, .env, and Environment

- **Actor**: Daemon, MCP server, view CLI, calibration CLI
- **Preconditions**: Configuration may exist in `~/.aprilcam`, a
  project-local `.aprilcam`, `.env`, or process environment variables.
- **Main Flow**:
  1. Client or daemon calls `Config.load()` at startup.
  2. Loader reads `~/.aprilcam` first (user-global).
  3. Loader walks up from cwd looking for `.aprilcam` (project-local; first
     match wins).
  4. Loader reads `.env` via python-dotenv.
  5. Process environment variables override all files.
  6. A `Config` dataclass is returned with resolved values.
- **Postconditions**: All entry points share the same config values; env vars
  always win.
- **Acceptance Criteria**:
  - [ ] `APRILCAM_DATA_DIR` env var overrides dotfile value.
  - [ ] Project-local `.aprilcam` overrides `~/.aprilcam`.
  - [ ] Default values apply when no config source specifies a variable.
  - [ ] All entry points (daemon, view, MCP server, calibrate) call
        `Config.load()` first.

---

## SUC-008: Calibration CLI Routes Through Daemon

- **Actor**: Human user running `aprilcam calibrate`
- **Preconditions**: Daemon is running (or will be auto-spawned); a viewer
  may be active on the same camera.
- **Main Flow**:
  1. Calibration CLI calls `ensure_running(config)`.
  2. CLI sends `open_camera` RPC to daemon (or reuses existing open camera).
  3. For each calibration frame, CLI sends `capture_frame(cam_name)` RPC;
     daemon returns the decoded frame bytes.
  4. CLI computes homography (unchanged math).
  5. CLI sends `get_calibration_save_path()` RPC; writes JSON to returned path.
  6. CLI sends `reload_calibration()` RPC; daemon re-loads homography.
- **Postconditions**: New calibration is active for all subscribers on the
  next frame; the camera was never opened directly by the CLI.
- **Acceptance Criteria**:
  - [ ] `aprilcam calibrate` works while a viewer is running (no camera
        contention).
  - [ ] After calibration, the viewer picks up the new homography on the
        next frame (daemon reloads and includes it in subsequent messages).
  - [ ] Calibration JSON is written to the path advertised by the daemon.

---

## SUC-009: Sprint 001 Path Drawing Verified End-to-End

- **Actor**: AI agent + human tester
- **Preconditions**: Sprint 002 daemon architecture is implemented; a camera
  with 4 ArUco corner tags is available.
- **Main Flow**:
  1. `open_camera(index)` → camera_id; `create_playfield(camera_id)` → pf_id;
     `calibrate_playfield(pf_id)`.
  2. `create_path(pf_id, [...])` → `path_000`.
  3. `aprilcam view --camera <cam_name>` — viewer shows camera feed.
  4. Confirm path symbols, colors, and connecting lines appear correctly.
  5. Add a second path while viewer is running — appears within ~1 frame.
  6. `delete_path("path_000")` — only second path remains.
  7. Restart viewer — second path reappears immediately.
  8. `clear_paths(pf_id)` — all symbols vanish.
- **Postconditions**: Sprint 001 path-drawing feature is fully verified against
  real camera hardware with the daemon architecture in place.
- **Acceptance Criteria**:
  - [ ] Red filled square and blue circle appear with correct RGB colors
        (not BGR-flipped).
  - [ ] Paths appear on viewer startup from paths file without any MCP
        interaction.
  - [ ] Dynamic path additions appear within approximately one frame.
  - [ ] `delete_path` and `clear_paths` update the viewer within one frame.
  - [ ] No crash when viewer is restarted while daemon and paths are active.
