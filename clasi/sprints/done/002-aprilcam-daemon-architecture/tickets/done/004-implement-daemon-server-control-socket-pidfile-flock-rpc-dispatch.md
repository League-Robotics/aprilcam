---
id: '004'
title: Implement daemon server (control socket, pidfile/flock, RPC dispatch)
status: done
use-cases:
  - SUC-001
  - SUC-002
  - SUC-008
depends-on:
  - '003'
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement daemon server (control socket, pidfile/flock, RPC dispatch)

## Description

Create `src/aprilcam/daemon/server.py` and `src/aprilcam/daemon/__main__.py`.
The `DaemonServer` is the daemon's supervisor: it acquires the pidfile flock,
binds the control socket, starts per-camera data sockets, manages
`CameraPipeline` instances, and dispatches JSON-RPC commands from clients.
It is the only process that ever calls `CameraPipeline.start()`.

`__main__.py` is the entry point for `python -m aprilcam.daemon`.

## Acceptance Criteria

- [x] `src/aprilcam/daemon/server.py` created with `DaemonServer(config)` class.
- [x] `DaemonServer.run()` blocks until shutdown:
  - Acquires exclusive flock on `<socket_dir>/aprilcamd.pid`; exits silently
    if lock is already held (another daemon is running).
  - Writes own PID to the pidfile.
  - Binds control socket at `<socket_dir>/control.sock`; if `EADDRINUSE`,
    removes stale socket file and retries once.
  - Installs SIGTERM and SIGINT handlers that call `_shutdown()`.
  - Accepts connections on the control socket in a loop.
- [x] Control RPC commands handled:
  - `list_cameras` — returns list of currently open camera names.
  - `open_camera(index)` — opens camera, creates data socket, starts
    `CameraPipeline`, returns `cam_name` and `info_json_path`.
  - `close_camera(cam_name)` — stops pipeline, removes data socket.
  - `reload_calibration(cam_name)` — reloads calibration JSON for camera;
    pipeline picks it up on the next frame.
  - `get_camera_info(cam_name)` — returns the `info.json` dict.
  - `capture_frame(cam_name)` — returns the most recent raw frame as base64
    bytes (for calibration CLI use).
  - `get_calibration_save_path()` — returns `config.calibration_save_path`.
  - `shutdown` — stops all pipelines, closes sockets, exits.
- [x] Per-camera data socket bound at `<socket_dir>/<cam_name>/data.sock`;
  subscriber connections are accepted and their queues registered with the
  pipeline via `add_subscriber`.
- [x] `src/aprilcam/daemon/__main__.py` created; calls `Config.load()` then
  `DaemonServer(config).run()`.
- [x] Stale pidfile from unclean shutdown does not prevent daemon restart
  (flock is released by OS on process exit; EADDRINUSE handling for socket).
- [x] Daemon accepts multiple simultaneous control connections (concurrent
  clients can issue RPCs).

## Implementation Plan

### Approach

`DaemonServer.run()` uses `select.select` (or `threading`) to accept control
socket connections. Each accepted connection is handled in its own thread
(short-lived; one RPC per connection or keep-alive — simpler to do one-and-
done per connection for this sprint). Per-camera data socket accept loop runs
in a dedicated thread per camera.

### Files to Create

- `src/aprilcam/daemon/server.py`
- `src/aprilcam/daemon/__main__.py`

### Files to Modify

None beyond what previous tickets created.

### RPC Protocol

Client sends a single JSON object (line-terminated or length-prefixed — use
newline for simplicity):
```
{"cmd": "open_camera", "index": 2}\n
```
Server responds with a single JSON object:
```
{"ok": true, "cam_name": "cam_2", "info_json_path": "/..."}\n
```
On error: `{"ok": false, "error": "<message>"}`.

### Testing Plan

Covered partly by T009 (spawn-race test exercises the pidfile/flock logic).
Additional coverage in T009: mock `DaemonServer` RPC round-trip with a real
Unix socket pair.

### Documentation Updates

Module-level docstring in `server.py` describing the control RPC protocol
and socket paths.
