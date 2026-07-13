---
id: '006'
title: Update DaemonServer to use gRPC and dual-transport startup
status: done
use-cases:
  - SUC-001
  - SUC-002
depends-on:
  - '005'
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update DaemonServer to use gRPC and dual-transport startup

## Description

Replace `DaemonServer`'s JSON accept loop with a gRPC server startup. The server
now binds both a Unix domain socket and a TCP socket by default, controlled by startup
flags. The pidfile/flock mechanism and camera pipeline registry are preserved unchanged.

The `__main__.py` entry point is updated to accept and parse the new transport flags
and pass them to `DaemonServer`.

The old `daemon.client` module (`ControlClient`, `ensure_running`) is removed from
active use (it will be formally deleted in ticket 008 when the CLI is updated).

## Acceptance Criteria

- [x] `DaemonServer` accepts transport configuration: `unix_enabled`, `tcp_enabled`,
      `tcp_port`, `unix_path`.
- [x] When `unix_enabled=True`, the gRPC server binds `unix://<unix_path>`.
- [x] When `tcp_enabled=True`, the gRPC server binds `[::]:<tcp_port>`.
- [x] When both are enabled (default), both transports are active simultaneously.
- [x] `--no-unix --no-tcp` causes the daemon to exit with a clear error message.
- [x] Pidfile/flock logic is unchanged.
- [x] `aprilcam daemon start` (using old client) connects successfully after this change
      (backward compatibility note: old client cannot connect to gRPC — CLI update is
      ticket 008; the daemon must be startable from `__main__.py` for manual testing).
- [x] `__main__.py` parses `--unix/--no-unix`, `--tcp/--no-tcp`, `--tcp-port N`,
      `--unix-path PATH` flags.
- [x] `uv run pytest` passes.

## Implementation Plan

### Approach

1. Update `src/aprilcam/daemon/server.py`:
   - Replace constructor params: add `unix_enabled=True`, `tcp_enabled=True`,
     `tcp_port=5280`, `unix_path="/tmp/aprilcam/control.sock"`.
   - In `run()`: remove the `_bind_unix_socket` / accept loop block. Instead, call
     `grpc_server.make_grpc_server(transports, servicer)` to get a `grpc.Server`.
   - Add the appropriate addresses:
     - `server.add_insecure_port(f"unix:{unix_path}")` if unix_enabled.
     - `server.add_insecure_port(f"[::]:{tcp_port}")` if tcp_enabled.
   - Call `server.start()`. Use `server.wait_for_termination()` or a shutdown event.
   - On `SIGTERM`/`SIGINT`, call `server.stop(grace=5)`.
   - Keep pidfile/flock logic exactly as-is.
   - Keep `_cameras` dict and `_cam_lock` for the servicer to reference.
   - Remove the old `_data_socks`, `_data_accept_loop`, `_data_sender` methods.

2. Update `src/aprilcam/daemon/__main__.py`:
   - Add `argparse` to parse the four transport flags.
   - Pass parsed values to `DaemonServer(config, unix_enabled=..., tcp_enabled=...,
     tcp_port=..., unix_path=...)`.

3. Keep `daemon/client.py` in place for now (do not delete yet); ticket 008 handles
   the CLI update and final removal.

### Files to Modify

- `src/aprilcam/daemon/server.py` — replace JSON loop with gRPC server
- `src/aprilcam/daemon/__main__.py` — add transport flag parsing

### Testing Plan

- Manual: `python -m aprilcam.daemon --tcp-port 15280` (use non-standard port to
  avoid conflicts); verify it starts without error, then stop with Ctrl-C.
- Manual: `grpcurl -plaintext localhost:15280 list` should return the `AprilCam`
  service (requires ticket 005 done first).
- Unit: test that `DaemonServer.__init__` raises or logs error when both transports
  are disabled.
- Run `uv run pytest` for regression check.
