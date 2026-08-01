---
id: '004'
title: Daemon lifecycle and narrow gRPC control plane
status: done
use-cases:
- UC-01
- UC-13
depends-on:
- '003'
github-issue: ''
issue: daemon-bring-up-with-basic-camera-cli.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon lifecycle and narrow gRPC control plane

## Description

Depends on ticket 003 (`daemon/service.py` delegates to `Camera`
instances and `CameraDiscovery`/`CameraRegistry`). Implements:

- `daemon/lifecycle.py` — the `DaemonLifecycle` Protocol server side:
  flock singleton acquisition (raising `AlreadyRunning(holder)` when
  another instance holds the lock), opening the local unix-socket
  control-plane listener (see ticket 001's Open-Question-2 resolution —
  unix socket only this sprint, no TCP:5280), clean shutdown.
- `daemon/service.py` — the `Daemon` Protocol's RPC-body
  implementations for this sprint's narrow surface:
  `list_cameras`, `probe_cameras`, `get_frame`, `status`. Delegates to
  `camera/registry.py` (ticket 002) and `Camera`/`CameraDiscovery`
  (ticket 003) — no business logic duplicated here.
- `daemon/rpc.py` plus a **new, sprint-scoped `.proto` file**
  (`proto/aprilcam_v2.proto` or similar — not `archive/proto/aprilcam.proto`,
  which is the full v1 surface) defining exactly four RPCs:
  `ListCameras`, `ProbeCameras`, `GetFrame`, `GetStatus`. This is
  Architecture Decision 1's resolution: gRPC (already a base dependency
  per `pyproject.toml`), narrow surface, additive with Stage 8's later
  full-proto conversion rather than a throwaway bespoke protocol.
- `daemon/__main__.py` — process entry point wiring lifecycle + service
  + rpc together, invoked by `DaemonLifecycle.start()`'s local subprocess
  spawn (client-side half is ticket 005).

**Resolves Sprint 020 architecture Open Question 1** (`get_camera_config`
needed this sprint?): **No** — `CameraState` alone (already returned by
`list_cameras`/`probe_cameras`) carries everything UC-14.S-1 and UC-25's
scenarios need (`playfield`, `calibrated`, `number`, `present`,
`usable`). This ticket does **not** implement `get_camera_config` on
`daemon/service.py` or in the `.proto` subset. If ticket 006 or 007
finds a CLI scenario that genuinely needs it, that is new information
overriding this resolution and should be flagged back rather than
silently added — but the current read is that it is fully deferred with
the rest of UC-03.

**Reuse from archive**: convert `archive/src/aprilcam/daemon/server.py`
(459 lines — flock singleton, socket lifecycle, pidfile, clean shutdown
sequencing) for `daemon/lifecycle.py`; this is the closest existing
match to the new interface's intent. Do **not** convert
`archive/src/aprilcam/daemon/grpc_server.py` (1193 lines) or
`archive/proto/aprilcam.proto` (369 lines) wholesale — both are
significantly over-scoped for this sprint (full calibration/annotation/
config-push/stream RPC surface); write the new narrow `.proto` and a
thin `daemon/rpc.py` instead, per Architecture Decision 1. Do not
convert `archive/src/aprilcam/daemon/mdns.py` this sprint — Architecture
Decision 2 stubs discovery to local-only (ticket 005's concern, not
this one).

## Acceptance Criteria

- [x] `DaemonLifecycle.start()` with zero cameras and zero configuration
      succeeds and reports ready (UC-01.S-2).
- [x] `DaemonLifecycle.start()` with both Mac cameras attached succeeds,
      populates the registry, and `list_cameras()` (over the gRPC
      surface) returns both with stable enumeration numbers
      (UC-01.S-1).
- [x] A second `DaemonLifecycle.start()` while one instance holds the
      lock raises/reports `AlreadyRunning(holder)` naming the existing
      instance's identity (pid or socket path), and does not disturb
      the running daemon (UC-01.S-3).
- [x] Restarting the daemon (`stop()` then `start()`) yields the *same*
      enumeration numbers as before the restart, read via
      `registry.json` (UC-01.S-1's restart-stability check, this time
      through the full lifecycle rather than ticket 002's unit-level
      check).
- [x] `daemon/rpc.py`'s four RPCs round-trip correctly against
      `daemon/service.py` with a fake in-process camera fixture
      (headless, no real hardware): `ListCameras`, `ProbeCameras`,
      `GetFrame` (JPEG and PNG encoding), `GetStatus`.
- [x] `Daemon.status()` reports `running`, `cameras`, `config_dir`,
      `state_dir` correctly for a running daemon (UC-05's
      remotely-readable service view, as scoped by this sprint's
      `GetStatus` RPC).
- [x] The new `.proto` file is documented (header comment) as a
      sprint-020-scoped subset that Stage 8 extends in place, not
      replaces — so a future contributor does not treat it as
      disposable.

## Testing

- **Existing tests to run**: `tests/aprilcam/camera/` (tickets 002,
  003) — the daemon service delegates to both.
- **New tests to write**: `tests/aprilcam/daemon/test_lifecycle.py`
  (flock/`AlreadyRunning`/clean-shutdown, headless, using a
  temp lock-file path so tests don't collide with a real running
  daemon); `tests/aprilcam/daemon/test_service.py` (RPC round-trips
  against a fake `Camera`/`CameraRegistry`, headless);
  `tests/aprilcam/daemon/test_lifecycle_integration.py` marked
  `@pytest.mark.camera` for the full start/stop/restart-stability
  cycle against real hardware on this Mac.
- **Verification command**: `uv run pytest tests/aprilcam/daemon/`
  then `uv run pytest -m camera tests/aprilcam/daemon/` on this Mac.
