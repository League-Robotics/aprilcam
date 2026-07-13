---
status: pending
---

# AprilCam Daemon Architecture

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
- **Clients auto-spawn the daemon.** No human ever runs `aprilcam daemon`
  directly. When any client (MCP server, viewer, calibration tool, ad-hoc
  script) starts up, it checks for the control socket; if not present, it
  forks the daemon and waits for the socket to appear. A pidfile + flock
  prevents two clients from racing to spawn two daemons.
- **Configuration via dotfile, `.env`, or environment.** The daemon and every
  client read config from (in priority order, env wins):
  `~/.aprilcam` → `./.aprilcam` (walking up from cwd) → `./.env` → env vars.
  Variables include data-directory location, calibration-source path, and
  the calibration-output path the calibration tool uses.
- **Calibration tool routes through the daemon.** Today's `aprilcam calibrate`
  opens the camera directly; with the daemon owning cameras, it instead asks
  the daemon for frames and asks the daemon where to save the resulting
  calibration JSON.

## Configuration

Reading order, env vars override files:

1. `~/.aprilcam` (user-global)
2. `./.aprilcam` walking up the directory tree from cwd to `/` (project-local;
   first match wins)
3. `./.env` (loaded via existing `python-dotenv` dep)
4. Process environment

File format is `KEY=value` per line, `#` comments allowed. Variables:

| Variable | Default | Used by |
|---|---|---|
| `APRILCAM_DATA_DIR` | `./data/runtime/` (or `~/.aprilcam/runtime` outside a project) | Daemon — root for `<cam_name>/info.json`, `paths.json` |
| `APRILCAM_SOCKET_DIR` | `/tmp/aprilcam/` | Daemon — control socket and per-camera data sockets (separate from data dir to dodge AF_UNIX 104-byte path limit) |
| `APRILCAM_CALIBRATION_SOURCE` | `./data/calibration.json` | Daemon — read on camera-open and `reload_calibration` |
| `APRILCAM_CALIBRATION_SAVE_PATH` | same as `APRILCAM_CALIBRATION_SOURCE` | Calibration tool — where to write a new calibration; daemon advertises this in the control RPC `get_calibration_save_path` |
| `APRILCAM_LOG_LEVEL` | `INFO` | Daemon and CLIs |
| `APRILCAM_DAEMON_PIDFILE` | `<APRILCAM_SOCKET_DIR>/aprilcamd.pid` | Daemon (held under flock); clients consult to detect existing instance |

A new module `src/aprilcam/config.py` (extending the existing one) loads
this config and exposes a `Config` dataclass. All entry points (daemon,
view CLI, MCP server, calibration CLI) call `Config.load()` first thing.

## Daemon Lifecycle (auto-spawn)

The user never runs `aprilcam daemon` directly. Instead, every client uses
a shared helper `aprilcam.daemon.client.ensure_running(config) -> ControlClient`:

1. Try `connect()` to `<socket_dir>/control.sock`. If it succeeds, return the
   connected `ControlClient`. Done.
2. Otherwise, take an exclusive `fcntl.flock` on
   `<socket_dir>/aprilcamd.spawn.lock` (blocks if another client is mid-spawn).
3. Re-check the socket — another client may have just brought the daemon up.
4. If still down, `subprocess.Popen([sys.executable, "-m", "aprilcam.daemon"],
   start_new_session=True, stdout=DEVNULL, stderr=<log_file>)`. Detached;
   client doesn't wait on it.
5. Poll the socket every 50ms for up to 5s; bail with a clear error if it
   never appears.
6. Release the flock; connect; return.

The daemon itself, on startup:
- Acquires an exclusive flock on `<socket_dir>/aprilcamd.pid`. If the lock is
  taken, exits silently with a "daemon already running" log line — this makes
  the spawn step idempotent under races.
- Writes its PID to the pidfile (under the same lock).
- Binds the control socket; if `EADDRINUSE`, removes the stale socket file
  and retries once (handles unclean shutdown).
- Sets up SIGTERM/SIGINT handlers that close cameras, unlink sockets, release
  the lock cleanly.

Idle behavior: the daemon stays up after all cameras are closed. It can be
shut down explicitly via a `shutdown` control RPC, or with SIGTERM. (No
auto-exit timer in this sprint — keep it simple.)

## Calibration Tool Integration

The existing `aprilcam calibrate` ([src/aprilcam/cli/calibrate_cli.py](src/aprilcam/cli/calibrate_cli.py))
opens the camera directly today. After this sprint:

1. Calibration CLI calls `ensure_running(config)` like every other client.
2. Asks daemon to open the target camera (RPC `open_camera`).
3. For each calibration frame: asks daemon `capture_frame(cam_name)` (RPC),
   which returns a single decoded frame from the daemon's pipeline.
4. Computes the homography exactly as today (no math changes).
5. Asks daemon `get_calibration_save_path()` → returns the path from
   `APRILCAM_CALIBRATION_SAVE_PATH`. Calibration writes the JSON there.
6. Calls daemon `reload_calibration()` so live consumers pick up the new
   homography on the next frame.

This lets the calibration tool work even while a viewer is running — the
camera is shared through the daemon, not held exclusively by either tool.

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
- `src/aprilcam/daemon/__main__.py` — `python -m aprilcam.daemon` entry point that clients spawn
- `src/aprilcam/daemon/server.py` — supervisor + control socket + pidfile/flock
- `src/aprilcam/daemon/camera_pipeline.py` — per-camera capture/detect/publish
- `src/aprilcam/daemon/protocol.py` — message schema (msgpack), control commands
- `src/aprilcam/daemon/client.py` — `ensure_running()` + `ControlClient` shared by every client
- `src/aprilcam/cli/view_cli.py` — `aprilcam view`

### Modified
- [src/aprilcam/server/mcp_server.py](src/aprilcam/server/mcp_server.py) —
  replace direct `cv.VideoCapture` calls with `ensure_running()` + daemon RPC;
  replace pipe `send_command` calls with `paths.json` rewrites.
- [src/aprilcam/cli/calibrate_cli.py](src/aprilcam/cli/calibrate_cli.py) —
  route through `ensure_running()` + daemon's `capture_frame` RPC; write to
  `get_calibration_save_path()` then call `reload_calibration()`.
- [src/aprilcam/cli/__init__.py](src/aprilcam/cli/__init__.py) — register the
  new `view` subcommand. (No `daemon` subcommand — clients launch via
  `python -m aprilcam.daemon`.)
- [src/aprilcam/config.py](src/aprilcam/config.py) — extend with the new
  `Config` dataclass, dotfile/`.env`/env-var loader.
- `pyproject.toml` — add `msgpack` dependency. (`python-dotenv` already present.)

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

- launchd / systemd integration. The daemon stays user-spawned via the
  client `ensure_running()` helper; not registered as a system service.
- Multi-host / TCP transport — Unix sockets only.
- Authentication / multi-user.
- Recording / playback through the daemon (already exists via `LoopingVideoCapture`).
- Auto-shutdown of an idle daemon — it stays up until SIGTERM or explicit
  `shutdown` RPC.

## Migration notes

- The sprint-001 `paths.py` + `PathRegistry` + 4 MCP tools all stay; only the
  side effect changes (file write instead of pipe write).
- Existing tests for the path data model (`test_paths.py`,
  `test_mcp_path_tools.py`, `test_draw_paths.py`) keep working.
- `test_live_view_ipc.py` is deleted with the IPC layer.
- The CLI `aprilcam live` can either be aliased to `daemon + view` for
  backward compat, or printed as a deprecation pointing at the new commands.

## Verification

1. With no daemon running, `aprilcam view --camera cam_2` is the first thing
   started: it auto-spawns the daemon, waits, then shows the camera feed
   with tag overlays drawn client-side. (Proves auto-spawn works.)
2. `pgrep -fl aprilcamd` shows exactly one daemon. Run two more clients
   simultaneously — still exactly one daemon (proves the flock works).
3. Open a second viewer — both show the feed; proves fan-out.
4. From the MCP server: `open_camera(index=2)` returns a camera_id; `get_tags`
   returns the latest detections. (MCP server didn't need to spawn a daemon
   because step 1 already did; if it's the first client instead, it spawns.)
5. `create_path(playfield_id="pf_cam_2", waypoints_json="...")` — both viewers
   redraw with the path within ~33ms (one frame).
6. `delete_path` / `clear_paths` — both viewers update within one frame.
7. Stop and restart `aprilcam view` — paths reappear immediately (proves
   paths-file discovery + reload-on-connect works).
8. Stop and restart the MCP server — daemon keeps publishing; viewers keep
   running; restarted MCP server reads `paths.json` to rebuild its registry
   (or starts empty — decision pending).
9. Set `APRILCAM_DATA_DIR=/tmp/test-aprilcam` in the env, kill the daemon,
   run a client — confirm it spawns a new daemon that uses the new path.
10. Run `aprilcam calibrate` while a viewer is active — confirm both get
    frames simultaneously, calibration writes to the configured path, and the
    viewer picks up the new homography on the next frame.
11. `uv run pytest` — existing sprint-001 unit tests still pass; new daemon
    tests cover the protocol round-trip, per-frame backpressure, ensure-running
    spawn-race, and config-loader precedence.

After this sprint, sprint 001's path drawing works end-to-end with any viewer
running on any camera the daemon owns, and the camera-ownership conflict that
blocked T005 is gone.
