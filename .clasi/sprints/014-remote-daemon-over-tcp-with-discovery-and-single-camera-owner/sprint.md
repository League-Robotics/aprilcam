---
id: '014'
title: Remote Daemon over TCP with Discovery and Single Camera Owner
status: planning-docs
branch: sprint/014-remote-daemon-over-tcp-with-discovery-and-single-camera-owner
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
  - SUC-005
  - SUC-006
issues:
  - plan-daemon-runs-on-a-remote-raspberry-pi-every-client-discovers-it-and-connects-over-tcp.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 014: Remote Daemon over TCP with Discovery and Single Camera Owner

## Goals

Run the AprilCam daemon on a remote Raspberry Pi (Ubuntu 24.04, aarch64) and
have the MCP server and all CLI clients discover it via mDNS, connect over
TCP, and switch between daemons at runtime — while enforcing the invariant
that the daemon's `CameraPipeline` is the sole camera opener (no client ever
opens a camera device directly).

## Problem

Today the daemon, MCP server, and CLI all run on one machine and multiple
components open camera hardware directly. There is no client-side mDNS
discovery and no runtime daemon switch. Moving the camera to a Pi exposes
three structural problems:

1. The MCP server and CLI open `cv2.VideoCapture(index)` directly — they would
   target the Mac's cameras, not the Pi's.
2. No client-side discovery module: `zeroconf` is advertise-only; there is no
   `ServiceBrowser` anywhere.
3. Stream sockets bind `127.0.0.1`; calibration/config/paths files are read
   from local disk — both break when the daemon is remote.

## Solution

Five workstreams that together enforce the remote Pi architecture:

1. **W1 — Single camera owner**: New `EnumerateCameras` RPC. Remove all
   direct `cv2.VideoCapture` from MCP server and CLI. All detection sources
   become `DaemonCapture` (RPC). `vision/objects.py` stops opening its own
   capture.
2. **W2 — Discovery + targeting + runtime switch**: New `client/discovery.py`
   (zeroconf `ServiceBrowser`). Shared resolver with precedence: explicit flag
   → env/config → local unix probe → mDNS. Remove `DaemonControl` auto-spawn.
   Shared `cli/_daemon.py` helper. New `connect_daemon` MCP tool. `get_version`
   reports active target.
3. **W3 — Streaming over TCP**: Daemon stream sockets bind `0.0.0.0`. Frame
   fetch via gRPC `CaptureFrame` (retire AF_UNIX `_frames_from_daemon`). Stream
   consumers accept a `host` parameter. Live view passes daemon host to subprocess.
4. **W4 — File-proxy RPCs**: Proto + regen + daemon handlers + MCP wiring for
   `GetCameraConfig/SetCameraConfig`, `GetCalibration/SetCalibration`,
   `GetPaths/SetPaths`, `ListPlayfields`, `EnumerateCameras`. MCP stops touching
   the daemon's disk.
5. **W5 — Pi deployment**: Pinned-wheel build, pipx install with daemon extra,
   systemd unit, provision script, seed calibration. Deploy to `vali.local`.

## Success Criteria

- `grep -r "VideoCapture" src/aprilcam/ --include="*.py" | grep -v "camera_pipeline.py"` → zero matches.
- `grep -r "AF_UNIX" src/aprilcam/server/ --include="*.py"` → zero matches.
- `grep -r "subprocess.Popen" src/aprilcam/client/control.py` → zero matches.
- `aprilcam cameras` (no env vars, one daemon on LAN) auto-discovers and returns Pi's camera list.
- `uv run pytest` passes.
- Daemon deployed to `vali.local`; MCP golden path works from the Mac.

## Scope

### In Scope

- `EnumerateCameras` RPC (proto + regen + daemon handler + client stub).
- File-proxy RPCs (7 RPCs: GetCameraConfig/Set, GetCalibration/Set,
  GetPaths/SetPaths, ListPlayfields).
- All proto regeneration.
- Removal of `cam_<index>` direct-VideoCapture branches from `mcp_server.py`.
- Removal of AF_UNIX `_frames_from_daemon` path from `mcp_server.py`.
- `client/discovery.py` with `discover_daemons` and `resolve_daemon_target`.
- `cli/_daemon.py` shared argparse helper.
- `DaemonControl.connect_default` auto-spawn removal.
- `DaemonNotFoundError` in `errors.py`.
- `Config.daemon_host`/`daemon_port` fields + `CONFIG_VARS` entries.
- `zeroconf` promoted to base deps.
- `aprilcam mcp` argv parsing for `--daemon-host`/`--daemon-port`.
- `connect_daemon` MCP tool and session teardown.
- `get_version` reporting active daemon target.
- `daemon/stream.py` bind `0.0.0.0`.
- `client/stream.py` host-aware consumers.
- `cli/tags_cli.py` conversion to daemon RPC.
- `cli/cameras_cli.py` conversion to `EnumerateCameras` RPC.
- `cli/calibrate_cli.py` camera selection via `EnumerateCameras` RPC.
- `cli/view_cli.py` host-aware consumer + shared daemon args.
- `vision/objects.py` `ObjectTracker` audit and guard.
- `calibration/calibration.py` `parse_calibration_from_dict` helper.
- `camera/camera_config.py` `parse_camera_config` helper.
- `core/playfield_def.py` `PlayfieldDefinitionRegistry.add_from_dict()`/`clear()`.
- MCP server playfield loading via `ListPlayfields` RPC on connect.
- Unit tests: discovery resolver, no-spawn, file-proxy round-trips,
  connect_daemon teardown, stream consumer host.
- Local TCP integration test (no hardware).
- `deploy/aprilcamd.service`, `deploy/provision-pi.sh`, `deploy/README.md`.
- Live bring-up on `vali.local` (hardware required; may defer if camera unavailable).
- Version bump.

### Out of Scope

- TLS for gRPC (documented as known limitation).
- Streamable HTTP transport.
- `vidar.local` live bring-up (SSH key not in place — separate ticket when resolved).
- `web_server.py` full update (best-effort; MCP server is the primary target).
- Changing the gRPC wire protocol format (JSON blobs stay as strings).

## Test Strategy

- Unit tests for all new modules using `unittest.mock` (no live daemon needed).
- Integration test: start daemon locally over TCP, exercise RPCs.
- Verification greps to prove the camera-owner invariant.
- Live bring-up test on `vali.local` (hardware ticket 010).

## Architecture Notes

Sprint 013 must merge before Sprint 014 executes. Sprint 014 adopts Sprint
013's `Config` fields, FHS/XDG directories, and the `deploy/aprilcamd.service`
scaffold. If Sprint 013 added stub `CONFIG_VARS` entries for
`APRILCAM_DAEMON_HOST`/`APRILCAM_DAEMON_PORT`, Sprint 014 activates them.

gRPC is insecure (no TLS). Stream sockets bind `0.0.0.0` — acceptable on a
trusted lab LAN. Document firewall implications in deploy README.

`DaemonControl.connect_default` no longer spawns a daemon. Any workflow that
relied on auto-spawn must pre-start the daemon via `aprilcam daemon start`
or `systemctl start aprilcamd`.

## GitHub Issues

- plan-daemon-runs-on-a-remote-raspberry-pi-every-client-discovers-it-and-connects-over-tcp.md

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (APPROVE)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | EnumerateCameras RPC — proto addition and regeneration | — |
| 002 | EnumerateCameras daemon handler and convert CLI/MCP to use it | 001 |
| 003 | client/discovery.py, shared cli/_daemon.py, and DaemonControl no-spawn refactor | 001 |
| 004 | File-proxy RPCs — proto additions, regen, and daemon handlers | 001 |
| 005 | MCP server — wire file-proxy RPCs and remove local disk access | 003, 004 |
| 006 | Remove direct VideoCapture in MCP server and vision/objects.py | 003 |
| 007 | TCP streaming — daemon binds 0.0.0.0, host-aware consumers, tags CLI, view CLI | 003, 006 |
| 008 | Unit and integration tests, verification grep, and version bump | 002, 003, 004, 005, 006, 007 |
| 009 | Pi deployment assets and local TCP golden-path integration test | 008 |
| 010 | Live bring-up on vali.local (HARDWARE/NETWORK REQUIRED) | 009 |

Tickets execute serially in the order listed.
