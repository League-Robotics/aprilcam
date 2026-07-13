---
id: '002'
title: AprilCam Daemon Architecture
status: done
branch: sprint/002-aprilcam-daemon-architecture
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
- SUC-007
- SUC-008
- SUC-009
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 002: AprilCam Daemon Architecture

## Goals

Introduce `aprilcamd` — a long-running daemon that owns all cameras, runs
detection, and fans out per-frame data to subscribers via Unix domain sockets.
Every other process (MCP server, viewer, calibration CLI) becomes a stateless
consumer. This eliminates the macOS AVFoundation exclusive-camera conflict that
blocked sprint 001's manual path-drawing verification (T005).

After this sprint, sprint 001's agent-drawn paths feature works end-to-end:
any `aprilcam view` window shows paths drawn by the agent in real time,
regardless of how many viewers or how many restarts.

## Problem

On macOS AVFoundation, `cv.VideoCapture` is exclusive — two processes
cannot open the same camera simultaneously. Today, the camera is claimed by
whichever process wins the race (MCP server, live-view child, calibration CLI).
Sprint 001 deferred the final manual verification step (T005) because
starting an independent viewer window fought the MCP server for the camera.

Additionally, the parent-to-child OS-pipe IPC introduced in sprint 001 only
works when the MCP server spawned the live-view child. A user-launched
`aprilcam live` window has no pipe and cannot receive path commands.

## Solution

A single `aprilcamd` process owns all cameras via a per-camera capture and
detect pipeline thread. It broadcasts per-frame snapshots (JPEG image + tags +
metadata + paths-file pointer) over a Unix domain socket per camera. All
consumers connect to the socket; the daemon fans out. Camera ownership
contention is impossible because only the daemon ever calls `cv.VideoCapture`.

Paths continue to live in `PathRegistry` (sprint 001 data model unchanged).
The MCP path tools now atomically rewrite a `paths.json` file (advertised by
the daemon in each frame message); viewers stat-and-reload on mtime change.
The OS-pipe IPC from sprint 001 is deleted entirely.

A shared `ensure_running()` helper auto-spawns the daemon on demand; clients
never start it manually. A flock-protected spawn lock prevents duplicate
daemon processes under concurrent client startup.

## Success Criteria

- With no daemon running, `aprilcam view --camera <name>` auto-spawns the
  daemon and shows the camera feed with tag overlays.
- Running two additional viewers results in exactly one daemon process.
- Both viewers display the same feed simultaneously (socket fan-out confirmed).
- `create_path` from the MCP server causes both viewers to redraw within
  approximately one frame (~33ms).
- `delete_path` / `clear_paths` update both viewers within one frame.
- Stopping and restarting a viewer — paths reappear immediately from the
  paths file.
- Stopping and restarting the MCP server — daemon keeps running; viewers
  keep running.
- `aprilcam calibrate` works while a viewer is active (shared camera via daemon).
- All sprint 001 unit tests continue to pass unchanged.
- New unit tests cover: protocol round-trip, backpressure, spawn-race, and
  config-loader precedence.

## Scope

### In Scope

- `src/aprilcam/daemon/` — new package: `__init__.py`, `__main__.py`,
  `server.py`, `camera_pipeline.py`, `protocol.py`, `client.py`
- `src/aprilcam/cli/view_cli.py` — new `aprilcam view` subcommand
- `src/aprilcam/config.py` — extend with full `Config` dataclass and
  dotfile/`.env`/env-var loader chain
- `src/aprilcam/server/mcp_server.py` — replace direct `cv.VideoCapture`
  calls with daemon RPC; replace OS-pipe `send_command` with `paths.json`
  atomic rewrites; remove sprint 001 pipe plumbing
- `src/aprilcam/cli/calibrate_cli.py` — route through `ensure_running()` plus
  daemon `capture_frame` RPC
- `src/aprilcam/cli/__init__.py` — register `view` subcommand
- `src/aprilcam/ui/liveview.py` — delete `LiveViewProcess`, `_child_main`,
  `_drain_commands` entirely
- `pyproject.toml` — add `msgpack` dependency
- New and updated unit tests for all of the above
- Final manual verification of sprint 001 path-drawing (enabled by eliminating
  the camera-ownership conflict)

### Out of Scope

- launchd/systemd integration — daemon remains user-spawned only
- TCP or streamable HTTP transport — Unix sockets only
- Authentication or multi-user support
- Recording/playback through the daemon
- Auto-shutdown of an idle daemon
- `aprilcam daemon` CLI subcommand — clients launch via `python -m aprilcam.daemon`
- Web UI or browser-facing interface

## Test Strategy

- Unit tests (pytest, no camera hardware required):
  - Protocol round-trip: encode a frame message, decode it, assert all fields
    are preserved correctly.
  - Backpressure: daemon drops a frame for a slow subscriber rather than
    blocking the capture loop.
  - Spawn-race: two threads call `ensure_running()` concurrently; assert
    exactly one daemon process was spawned.
  - Config-loader precedence: env var beats dotfile, dotfile beats default.
  - Paths-file write: MCP path tools write valid JSON to the configured path.
- Existing tests (`test_paths.py`, `test_mcp_path_tools.py`,
  `test_draw_paths.py`) must continue to pass unchanged.
- `test_live_view_ipc.py` is deleted with the OS-pipe IPC layer.
- Manual verification sequence from the daemon-architecture issue (items 1-10)
  is the acceptance test for the sprint.

## Architecture Notes

- The daemon uses threading for the supervisor and one pipeline thread per
  camera; asyncio is not required.
- Unix socket path limit (104 bytes on macOS) is handled by keeping sockets
  in `APRILCAM_SOCKET_DIR` (default `/tmp/aprilcam/`), separate from the
  data directory.
- Msgpack framing: each message is length-prefixed (4-byte big-endian uint32)
  followed by the msgpack payload.
- Backpressure: per-subscriber send queue capped at 2 frames; daemon drops
  stale frames for slow consumers, not the capture loop.
- `PathRegistry` and `Waypoint`/`Path` data model from sprint 001 are
  unchanged. Only the side effect of path mutations changes (file write
  instead of pipe write).

## GitHub Issues

(None — driven by internal CLASI issues.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Extend config.py with Config dataclass and multi-source loader | — |
| 002 | Implement daemon protocol module (msgpack frame schema) | 001 |
| 003 | Implement daemon camera pipeline (capture, detect, encode, fan-out) | 002 |
| 004 | Implement daemon server (control socket, pidfile/flock, RPC dispatch) | 003 |
| 005 | Implement daemon client (ensure_running, ControlClient, spawn-race guard) | 004 |
| 006 | Implement aprilcam view CLI (subscriber loop, overlay rendering) | 005 |
| 007 | Refactor MCP server to use daemon client and paths.json writes | 005 |
| 008 | Refactor calibrate CLI to route through daemon and delete liveview.py | 007 |
| 009 | Write unit tests for daemon components (protocol, backpressure, spawn-race, config) | 008 |
| 010 | Manual verification of sprint 001 path drawing end-to-end | 009 |

Tickets execute serially in the order listed.
