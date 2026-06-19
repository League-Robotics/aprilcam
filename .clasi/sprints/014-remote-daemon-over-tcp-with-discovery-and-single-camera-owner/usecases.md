---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 014 Use Cases

## SUC-001: MCP server and CLI discover and connect to a remote daemon via mDNS

- **Actor**: AI agent (via MCP) or developer running a CLI command.
- **Preconditions**: AprilCam daemon running on a Raspberry Pi (`vali.local`
  or `vidar.local`) and advertising `_aprilcam._tcp` via mDNS. No
  `APRILCAM_DAEMON_HOST` set. The Mac has `zeroconf` available.
- **Main Flow**:
  1. Developer runs `aprilcam cameras` (or MCP session starts).
  2. `resolve_daemon_target()` in `client/discovery.py` runs mDNS browse
     for up to 1 second on `_aprilcam._tcp.local.`.
  3. Exactly one daemon is found; it is auto-selected.
  4. `DaemonControl.connect()` establishes a TCP gRPC connection to the Pi.
  5. The command calls `EnumerateCameras` RPC and returns the Pi's camera list.
  6. No camera hardware is probed or opened locally.
- **Postconditions**: Developer sees cameras attached to the Pi. No local
  camera access occurs.
- **Acceptance Criteria**:
  - [ ] With one daemon on the LAN, `aprilcam cameras` returns that daemon's
        cameras without `APRILCAM_DAEMON_HOST` being set.
  - [ ] With two daemons, `aprilcam cameras` exits with an error listing
        both daemons and instructing the user to set `APRILCAM_DAEMON_HOST`.
  - [ ] With zero daemons and no env var, `aprilcam cameras` exits with
        "no aprilcam daemon found" — no spawn attempt.
  - [ ] `APRILCAM_DAEMON_HOST=vali.local` bypasses mDNS and connects directly.
  - [ ] `--daemon-host vali.local` on the CLI bypasses mDNS and connects directly.

---

## SUC-002: The daemon on a Raspberry Pi is the sole owner of camera hardware

- **Actor**: AI agent or developer using any client interface.
- **Preconditions**: Daemon running on a Pi with an OV9782 camera attached.
  MCP server and CLI running on the dev Mac.
- **Main Flow**:
  1. Agent calls `list_cameras` MCP tool.
  2. MCP server calls `EnumerateCameras` RPC; daemon runs
     `camutil.list_cameras()` on the Pi and returns available devices.
  3. Agent calls `start_detection` on a camera.
  4. MCP server calls `OpenCamera` RPC; daemon opens the camera via its
     `CameraPipeline`. No `cv2.VideoCapture` executes on the Mac.
  5. Tag frames arrive over gRPC. Agent calls `get_tags` and receives results.
- **Postconditions**: Camera hardware is opened exactly once, on the daemon's
  side. A grep of client code confirms `VideoCapture` only in
  `daemon/camera_pipeline.py`.
- **Acceptance Criteria**:
  - [ ] `aprilcam cameras` calls `EnumerateCameras` RPC; no local probe.
  - [ ] `aprilcam tags` calls `OpenCamera` + `GetTags` RPC; no local
        `cv2.VideoCapture`.
  - [ ] MCP `list_cameras` calls `EnumerateCameras` RPC.
  - [ ] MCP `start_detection` uses `DaemonCapture` only; no `cv2.VideoCapture`
        on `cam_<index>` handles.
  - [ ] `vision/objects.py` does not open its own capture.
  - [ ] `grep -r "VideoCapture" src/aprilcam/ --include="*.py"` hits only
        `daemon/camera_pipeline.py` (zero device-index opens in client code).

---

## SUC-003: MCP server switches from one remote daemon to another at runtime

- **Actor**: AI agent with an active MCP session.
- **Preconditions**: MCP session connected to `vali.local`. Agent wants to
  retarget to `vidar.local`.
- **Main Flow**:
  1. Agent calls `connect_daemon(host="vidar.local")`.
  2. MCP server tears down session state: stops detection loops, stops live
     views, closes registries, clears `_cam_info` and frame/composite caches.
  3. MCP server closes the existing gRPC channel and connects to
     `vidar.local:5280`.
  4. MCP server calls `ListPlayfields` on the new daemon and reloads
     playfield registries.
  5. `connect_daemon` returns the new target address and the new daemon's
     camera list.
  6. `get_version` now reports the active target.
- **Postconditions**: Session is fully retargeted. Subsequent tool calls go
  to the new daemon.
- **Acceptance Criteria**:
  - [ ] `connect_daemon(host="vidar.local")` succeeds and returns a response
        identifying `vidar.local`.
  - [ ] After switching, `start_detection` / `get_tags` use the new connection.
  - [ ] `get_version` reports the active daemon host.
  - [ ] `connect_daemon(host=None)` re-runs discovery and auto-selects.
  - [ ] `connect_daemon(local=True)` connects to the local Unix socket.

---

## SUC-004: Image tools and live view work against the remote daemon

- **Actor**: AI agent or developer using `capture_frame`, `detect_circles`, or
  `start_live_view`.
- **Preconditions**: Daemon running on a Pi. MCP server connected via TCP.
- **Main Flow**:
  1. Agent calls `capture_frame(source_id="ov9782-pi")`.
  2. MCP server calls `CaptureFrame` gRPC RPC (already remote-safe) instead of
     the `AF_UNIX` `_frames_from_daemon` helper.
  3. Frame data arrives over gRPC; MCP processes it and returns base64 image.
  4. Agent calls `start_live_view(source_id="ov9782-pi")`.
  5. MCP server spawns `aprilcam view --daemon-host vali.local` subprocess.
  6. `view_cli` uses a host-aware `ImageStreamConsumer`; stream socket connects
     to `vali.local:<tcp_port>` (daemon now binds `0.0.0.0`).
- **Postconditions**: Frames flow from the remote Pi to the dev Mac over TCP.
- **Acceptance Criteria**:
  - [ ] `capture_frame` works against a TCP-connected daemon.
  - [ ] `detect_circles`, `detect_lines`, `crop_region` work via gRPC
        `CaptureFrame` (not `AF_UNIX`).
  - [ ] Daemon stream sockets bind `0.0.0.0`, not `127.0.0.1`.
  - [ ] Stream consumers accept a `host` parameter; use daemon host when TCP.
  - [ ] `start_live_view` passes `--daemon-host`/`--daemon-port` to subprocess.

---

## SUC-005: Calibration and config files are proxied via gRPC

- **Actor**: AI agent or developer running `calibrate_playfield`.
- **Preconditions**: Daemon running on Pi. MCP server on Mac.
- **Main Flow**:
  1. Agent calls `calibrate_playfield(source_id="ov9782-pi")`.
  2. MCP server fetches calibration data via `GetCalibration` RPC; receives
     JSON blob from the daemon.
  3. After calibration is computed (frames via gRPC), MCP calls `SetCalibration`
     to write the result back to the daemon.
  4. Daemon atomically writes `calibration.json` on the Pi and calls
     `reload_calibration` on the live pipeline.
  5. `GetPaths`/`SetPaths` and `GetCameraConfig`/`SetCameraConfig` follow the
     same proxy pattern.
  6. `ListPlayfields` returns all playfield defs from the daemon; MCP loads
     them without reading local files.
- **Postconditions**: All per-camera state lives on the Pi. MCP server has
  no local references to Pi filesystem paths.
- **Acceptance Criteria**:
  - [ ] `calibrate_playfield` round-trip works over TCP with no local file I/O.
  - [ ] `GetCalibration`/`SetCalibration` RPCs exist in proto and are handled
        by `daemon/grpc_server.py`.
  - [ ] `GetCameraConfig`/`SetCameraConfig` RPCs exist and are handled.
  - [ ] `GetPaths`/`SetPaths` RPCs exist and are handled.
  - [ ] `ListPlayfields` RPC exists and is handled.
  - [ ] `EnumerateCameras` RPC exists and is handled.
  - [ ] MCP server has no remaining `_cam_info["camera_dir"]` or `paths_file`
        local-path references that assume daemon files are local.

---

## SUC-006: Operator deploys the daemon to a Raspberry Pi

- **Actor**: Developer / operator provisioning a Pi for field use.
- **Preconditions**: `vali.local` reachable via SSH as `eric`. USB OV9782
  camera attached to the Pi.
- **Main Flow**:
  1. Operator runs `deploy/provision-pi.sh eric@vali.local` on the Mac.
  2. Script installs apt deps, adds `eric` to the `video` group.
  3. Operator copies the wheel to the Pi and installs via pipx.
  4. Operator copies `deploy/aprilcamd.service` to `/etc/systemd/system/`
     and runs `systemctl enable --now aprilcamd`.
  5. From the Mac, `aprilcam cameras` auto-discovers the daemon and shows
     the OV9782 camera.
- **Postconditions**: Daemon running as a systemd service on the Pi,
  advertising via mDNS, reachable over TCP from the Mac.
- **Acceptance Criteria**:
  - [ ] `deploy/provision-pi.sh` exists and is documented.
  - [ ] `deploy/aprilcamd.service` includes `User=eric`,
        `SupplementaryGroups=video`, `APRILCAM_DATA_DIR`,
        `After=avahi-daemon.service`, and the Sprint 013 `*Directory=`
        directives.
  - [ ] After provisioning, daemon starts and advertises `_aprilcam._tcp`.
  - [ ] `aprilcam cameras` on the Mac returns the Pi's camera list.
  - [ ] MCP golden path works against the deployed Pi.
  - [ ] `mss` import verified lazy (headless daemon starts without display).
