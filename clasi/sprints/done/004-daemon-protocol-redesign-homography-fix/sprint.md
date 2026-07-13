---
id: '004'
title: Daemon Protocol Redesign & Homography Fix
status: done
branch: sprint/004-daemon-protocol-redesign-homography-fix
use-cases: []
issues:
- aprilcam-daemon-protocol-specification.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 004: Daemon Protocol Redesign & Homography Fix

## Goals

Replace the ad-hoc JSON/msgpack daemon protocol with a proper gRPC + dual-transport
design, and fix the homography bug that silently prevents world-coordinate computation.

## Problem

The current daemon has two related problems:

1. **Protocol deficiencies**: The control socket uses untyped newline-delimited JSON
   with no schema. The data socket bundles frames and tags together forcing tag-only
   consumers to receive and discard every JPEG. There is no TCP option so remote
   clients cannot connect. Raw socket code lives in `view_cli.py`.

2. **Homography bug**: `Playfield._auto_discover_homography()` calls
   `discover_homography()` with no arguments but the real signature requires
   `(device_name, width, height, data_dir)`. The TypeError is silently caught so
   `self._homography` is always None, making `tag.wx` and `tag.wy` always None
   even when a valid calibration file exists on disk.

## Solution

**Homography fix**: Defer homography discovery to `start()` time when the camera is
already open and device_name/resolution are known.

**Protocol redesign**:
- Replace the JSON control socket with a gRPC endpoint (Unix socket + TCP, both enabled
  by default).
- Split the per-camera data stream into two independent streams: image stream and tag
  stream. Consumers subscribe to either or both.
- Introduce `ImageStreamProducer` and `TagStreamProducer` on the daemon side;
  `ImageStreamConsumer` and `TagStreamConsumer` on the client side.
- Add a `DaemonControl` class wrapping the gRPC stub, replacing `ControlClient`.
- Add Pydantic domain models (`TagRecord`, `TagFrame`, `CameraInfo`) for
  application-facing data; protobuf is wire-only.
- Enable gRPC Server Reflection for interface discovery.
- Advertise via mDNS/Bonjour (`zeroconf`) when TCP is active.
- Update `daemon_cli.py` and `view_cli.py` to use the new client API.

## Success Criteria

- `aprilcam daemon start/stop/status/restart` work via gRPC.
- `aprilcam view` renders live frames and tags using `ImageStreamConsumer` and
  `TagStreamConsumer`.
- A client can connect using TCP (no Unix socket required).
- `grpcurl -plaintext localhost:5280 list` returns the `AprilCam` service.
- `Playfield._auto_discover_homography()` loads calibration correctly; `tag.wx`
  and `tag.wy` are non-None when a homography file exists.
- Adaptive tag publishing: no publish when nothing moves; heartbeat every 1s.
- mDNS advertisement present when `--tcp` is active.

## Scope

### In Scope

- Fix `Playfield._auto_discover_homography()` argument bug.
- Write `proto/aprilcam.proto` with full gRPC service definition.
- New `src/aprilcam/daemon/grpc_server.py` — gRPC servicer.
- New `src/aprilcam/daemon/stream.py` — `ImageStreamProducer`, `TagStreamProducer`.
- New `src/aprilcam/client/` package: `control.py`, `stream.py`, `models.py`.
- Update `src/aprilcam/daemon/server.py` — replace JSON control loop with gRPC server.
- Update `src/aprilcam/daemon/camera_pipeline.py` — call producers instead of bundling.
- Update `src/aprilcam/cli/view_cli.py` — use consumer API.
- Update `src/aprilcam/cli/daemon_cli.py` — use `DaemonControl`.
- Startup flags: `--unix/--no-unix`, `--tcp/--no-tcp`, `--tcp-port`, `--unix-path`.
- gRPC Server Reflection.
- mDNS advertisement via `zeroconf` (TCP mode only).
- `pyproject.toml`: add `grpcio`, `grpcio-tools`, `grpcio-reflection`, `protobuf`,
  `zeroconf`, `pydantic` dependencies.

### Out of Scope

- Streamable HTTP transport.
- `DaemonControl.connect_default()` mDNS client-side browse (env vars + config only).
- Multi-camera compositing (Sprint 006).
- MCP server tools (separate sprints).
- Performance tuning or ring buffer changes.

## Test Strategy

- Unit tests for Pydantic model construction and protobuf conversion adapters.
- Integration smoke test: start daemon in-process, connect via `DaemonControl`,
  call `list_cameras()`, verify response.
- `test_playfield_homography.py`: assert `_auto_discover_homography` returns a
  valid numpy array when a calibration file is present.
- Manual verification: `grpcurl` reflection query succeeds; `aprilcam view` renders.

## Architecture Notes

- Stream sockets are allocated on demand: `GetImageStream`/`GetTagStream` gRPC
  calls create the socket and return the endpoint. Clients do not use well-known paths.
- If both transports are active the daemon creates both Unix and TCP sockets per stream;
  the `StreamEndpoint` carries both.
- Tag stream uses adaptive publish rate: change threshold 8 px, max 20 Hz default,
  1 s heartbeat.
- `daemon.client` module is removed; replaced by `aprilcam.client.control`.
- Protobuf to Pydantic conversion lives in thin adapter code inside consumer and
  control wrapper classes; application code never imports proto-generated types.
- Proto compilation step runs via `grpcio-tools` in a Makefile target or build script.

## GitHub Issues

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Fix Playfield._auto_discover_homography argument bug | — |
| 002 | Add gRPC dependencies and compile aprilcam.proto | — |
| 003 | Add Pydantic domain models (client/models.py) | 002 |
| 004 | Implement ImageStreamProducer and TagStreamProducer (daemon/stream.py) | 002, 003 |
| 005 | Implement gRPC servicer (daemon/grpc_server.py) | 002, 004 |
| 006 | Update DaemonServer to use gRPC and dual-transport startup | 005 |
| 007 | Implement DaemonControl, ImageStreamConsumer, TagStreamConsumer (client/) | 003, 006 |
| 008 | Update daemon_cli and view_cli to use DaemonControl and stream consumers | 007 |
| 009 | Add mDNS advertisement via zeroconf | 006 |
| 010 | Integration smoke tests and regression test for homography fix | 008, 009 |

Tickets execute serially in the order listed.
