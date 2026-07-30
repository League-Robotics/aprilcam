---
status: pending
---

# Plan: Daemon runs on a remote Raspberry Pi; every client discovers it and connects over TCP

## Context

Today the AprilCam daemon, MCP server, and CLI all run on **one machine**, and —
critically — **multiple components open the camera hardware directly**, not just
the daemon. We are moving the daemon onto a **remote Raspberry Pi** (Ubuntu
24.04, aarch64 — `eric@vidar.local` Pi 5, `eric@vali.local` Pi 4, **passwordless
SSH as `eric`**) with the camera attached, while the **MCP server and CLI run on
the dev machine** and reach the camera **only through the daemon over TCP**.

Three stakeholder requirements shape this (in priority order):

1. **The daemon is the *sole* camera owner.** Nothing other than the daemon's
   `CameraPipeline` may open a camera. The MCP server and **every** CLI command
   (`tags`, `cameras`, `calibrate`, `view`, `web`, `tool`) must be **pure daemon
   clients** that fetch all camera data via gRPC. (`[[project_daemon_sole_camera_owner]]`)
2. **Clients discover daemons automatically.** A client browses mDNS/Bonjour
   (`_aprilcam._tcp`), and if exactly one daemon is present it selects it
   automatically. If several are present, an **environment variable** picks
   which one.
3. **The connection is retargetable** — the MCP server can switch from one
   remote daemon to another at runtime.

### What already works

- Daemon binds **TCP `[::]:5280`** (all interfaces) and advertises mDNS
  `_aprilcam._tcp` with TXT `host=<hostname>` (`daemon/server.py`, `daemon/mdns.py`).
- `DaemonControl` accepts `host`/`port`; `CaptureFrame` returns JPEG over gRPC;
  tag **world coords are computed by the daemon** and returned in `GetTags`/
  `TagFrame` (`client/control.py`, `proto/aprilcam.proto`).
- The daemon's `CameraPipeline` (`daemon/camera_pipeline.py:224`) is the intended
  single camera owner and already exists.

### What breaks / violates the invariant (the work)

- **MCP server opens cameras directly.** For legacy `cam_<index>` handles,
  `start_detection` (`mcp_server.py:1104`) and `stream_tags` (`:2769`) open
  `cv2.VideoCapture(index)` in‑process (teardown re‑opens at `:1192`/`:2876`).
  Only daemon‑slug handles go through `DaemonCapture` (RPC). `list_cameras`
  (`_handle_list_cameras:499`) **probes local hardware** via `camutil.list_cameras`.
  `vision/objects.py:281` opens its own capture.
- **No client‑side discovery.** Zeroconf is advertise‑only; there is **no
  `ServiceBrowser`** anywhere. No shared "resolve which daemon" logic — each
  command rolls its own connection; only `daemon`/`view` accept `--tcp-port`.
- **CLI commands open the camera locally.** `aprilcam tags`
  (`tags_cli.py:129` `cv.VideoCapture`) and `aprilcam cameras`
  (`cameras_cli.py:155` → local probe) hit hardware; `calibrate` selects via
  local `list_cameras` (`calibrate_cli.py:159`).
- **`ListCameras` RPC only returns *currently‑open* cameras** (`grpc_server.py:76`)
  — there is no daemon‑side *enumerate available devices*.
- **Streams are local‑only**: daemon stream sockets bind `127.0.0.1`
  (`daemon/stream.py`), client consumers hardcode `("localhost", port)`
  (`client/stream.py`), and `_frames_from_daemon` uses `AF_UNIX`.
- **Per‑camera files read/written from local disk**: `calibration.json`,
  `config.json`, `paths.json`, and playfield defs are accessed at the path the
  daemon returns — which only exists on the Pi.

After this work, **the daemon owns the camera and its files; everything else is a
discoverable gRPC client.** This satisfies the invariant *and* enables remote.

---

## Workstream 1 — Enforce "the daemon is the sole camera owner"

**Goal:** exactly one place opens a camera — `daemon/camera_pipeline.py`. Every
other path becomes a gRPC client.

- **New daemon RPC `EnumerateCameras(Empty) → repeated CameraDevice{index, name,
  ...}`** (`proto/aprilcam.proto` + `grpc_server.py`): the daemon runs
  `camutil.list_cameras()` **on the Pi** and returns available devices.
  (`ListCameras` keeps its "currently open" meaning.)
- **MCP server** (`server/mcp_server.py`):
  - Delete the `cam_<index>` exclusive‑capture branches in `start_detection`
    (~1104) and `stream_tags` (~2769) and their re‑open teardown in
    `stop_detection`/`stop_stream` (~1192/~2876). **All** detection sources
    become `DaemonCapture` (RPC) or a tag‑stream consumer — no in‑process device.
    Refactor so `AprilCam(...)` no longer needs a `cv2.VideoCapture()` placeholder
    as a frame source.
  - `_handle_list_cameras` (the `list_cameras` tool, used by `web_server` too):
    call the daemon `EnumerateCameras` RPC instead of `camutil.list_cameras`.
  - `vision/objects.py`: stop opening its own capture; consume daemon frames
    (audit the `get_objects` path — it should read the detection ring buffer).
- **CLI commands → daemon clients**:
  - `tags_cli.py`: replace `cv.VideoCapture` + local `detect_all_tags` with
    `OpenCamera` + `GetTags` RPC (daemon already detects).
  - `cameras_cli.py`: replace local probe with `EnumerateCameras` RPC.
  - `calibrate_cli.py`: camera **capture** already via daemon (`_DaemonCapture`),
    but selection uses local `list_cameras` (`:159`) → use `EnumerateCameras` RPC.
- **Audit & confine legacy standalone openers** — `stream.py`,
  `core/aprilcam.py` (`:478`, `:696`), `calibration/calibration.py:1552`,
  `camera/camera.py`, `camera/video_camera.py`, `config.py` probe. Ensure **none
  are reachable from a client entry point**; they are daemon‑internal or dead.
  Document `daemon/camera_pipeline.py` as the only sanctioned `VideoCapture`.

## Workstream 2 — Discover the daemon, auto‑select, disambiguate by env, switch at runtime

**Goal:** zero‑config client connection on a single‑daemon LAN; env var to choose
among many; runtime retarget.

- **Client‑side mDNS discovery (NEW)** — `src/aprilcam/client/discovery.py`:
  `discover_daemons(timeout≈1.0s) → [DaemonInfo{name, host, port, addresses}]`
  using a zeroconf `ServiceBrowser` on `_aprilcam._tcp.local.` (read the TXT
  `host` + service port/addresses the daemon already publishes).
- **Shared target resolver** — `resolve_daemon_target(config, cli_args)` with this
  precedence (**clients never spawn a daemon**):
  1. Explicit `--daemon-host`(`--daemon-port`) flag, or env
     `APRILCAM_DAEMON_HOST`(`/PORT`) → use it directly (**no discovery**).
  2. Else a **running local daemon** on the default Unix socket counts as a
     candidate (so on‑box CLI, e.g. on the Pi, talks to the systemd daemon) —
     **probe only, never spawn**.
  3. Else **browse mDNS**: **1 found → auto‑select**; **>1 found → require
     `APRILCAM_DAEMON_HOST`** and error listing the discovered daemons.
  4. **0 found → hard error**: "no aprilcam daemon found — start one
     (`systemctl start aprilcamd` / `aprilcam daemon start`) or set
     `APRILCAM_DAEMON_HOST`." No auto‑spawn.
- **`zeroconf` into base deps** (`pyproject.toml`) — it is currently only in the
  `daemon` extra; lightweight CLI clients need it to browse. Degrade gracefully
  (fall back to env/explicit/local‑socket) if the import is unavailable.
- **`DaemonControl`** (`client/control.py`): **remove auto‑spawn entirely** —
  `connect_default` connects (TCP or local unix) or raises a clear error; it
  never launches `python -m aprilcam.daemon`. The **only** daemon starters are
  `aprilcam daemon start` (explicit) and the systemd unit. This is a behavior
  change: the MCP server no longer brings a daemon up on first use — the daemon
  must already be running (documented in the deploy runbook).
- **Shared CLI plumbing** — one argparse group (`--daemon-host/--daemon-port`) +
  `connect_from_args(config, args)` helper (new, e.g. `cli/_daemon.py`) used by
  **all** client commands (`daemon`, `tags`, `cameras`, `calibrate`, `view`,
  `web`). No shared helper exists today.
- **`Config`** (`config.py`): add `daemon_host`/`daemon_port` + env
  `APRILCAM_DAEMON_HOST`/`APRILCAM_DAEMON_PORT`.
- **`aprilcam mcp` argv** (`mcp_server.py:main`, currently ignores argv): parse
  `--daemon-host/--daemon-port`.
- **Runtime switch — MCP tool `connect_daemon(host=None, port=5280, local=False)`**:
  `host=None` re‑runs discovery; on switch, **tear down session state** (stop
  detection loops + live views, `registry.close_all()`, clear `_cam_info`,
  playfield/path/frame/composite registries — they are daemon‑specific), close
  the old client, connect+probe the new target, reload playfields via
  `ListPlayfields` (Workstream 4), and return the new target + remote
  `EnumerateCameras`. Report the active target in `get_version`.

## Workstream 3 — Frame & tag streaming over TCP

**Goal:** image tools and live view work against a remote daemon.

- **Daemon stream sockets bind `0.0.0.0`** (`daemon/stream.py` `_bind_tcp_socket`,
  currently `127.0.0.1`).
- **One‑shot frame fetch via gRPC** — in `mcp_server.py`, `resolve_source()` /
  `_read_one_frame()` for daemon cameras call `client.capture_frame(cam_name)`
  (already remote‑safe) instead of the `AF_UNIX` `_frames_from_daemon` (also
  fixes its latent never‑populated `data_socket` bug). Fixes `detect_*`,
  `crop_region`, `get_frame`, `capture_frame` for remote.
- **Stream consumers take a host** — `client/stream.py` `ImageStreamConsumer` /
  `TagStreamConsumer` accept `host` (default `localhost`); callers pass the
  connected `DaemonControl`'s host and use `(host, tcp_port)` when TCP‑connected,
  unix `socket_path` when local.
- **Live view** — `start_live_view` passes `--daemon-host/--daemon-port` to the
  `aprilcam view` subprocess; `view_cli` uses the host‑aware consumer.

## Workstream 4 — gRPC file‑proxy RPCs (calibration / config / paths / playfields)

**Goal:** the MCP server stops touching the daemon's disk; the daemon reads/writes
its own per‑camera files. One code path local and remote.

**New RPCs** in `proto/aprilcam.proto` (regenerate `src/aprilcam/proto/*_pb2*.py`
with `grpcio-tools` from the `dev` extra):

| RPC | Request → Reply | Replaces (MCP local‑disk call) |
|-----|-----------------|--------------------------------|
| `GetCameraConfig` | `CameraRequest` → `{json}` | `load_camera_config(camera_dir)` |
| `SetCameraConfig` | `{cam_name, json}` → `StatusReply` | `save_camera_config(...)` |
| `GetCalibration` | `CameraRequest` → `{json, present}` | `load_calibration_from_camera_dir(...)` |
| `SetCalibration` | `{cam_name, json}` → `StatusReply` | `save_calibration_to_camera_dir(...)` + `ReloadCalibration` |
| `GetPaths` / `SetPaths` | `CameraRequest` / `{cam_name, json}` → … | `paths.json` reads / `_write_paths_json` |
| `ListPlayfields` | `Empty` → `repeated {name, json}` | `playfield_def_registry.load_all(local_dir)` |
| `EnumerateCameras` | `Empty` → `repeated CameraDevice` | (Workstream 1) local `camutil.list_cameras` |

- **Daemon handlers** (`daemon/grpc_server.py`): atomic file I/O **on the Pi**,
  reusing the existing `save_*`/`load_*` helpers in `calibration/calibration.py`
  and `camera/camera_config.py` (tmp + `os.replace`). `SetCalibration` writes then
  reloads the live pipeline.
- **MCP side** (`mcp_server.py`): `_handle_open_camera`, `calibrate_playfield`,
  `set_camera_playfield`, and the path tools call these RPCs instead of `Path`
  I/O; playfields load via `client.list_playfields()` on connect, not local disk.
- **Refactor**: split `load_calibration_from_camera_dir` / `load_camera_config`
  into pure **parse‑from‑dict** functions reused by daemon (file→dict) and MCP
  (RPC blob→dict). Audit out any remaining `_cam_info["camera_dir"]`/`paths_file`
  local‑path use.

## Workstream 5 — Raspberry Pi deployment (pinned wheel + pipx + systemd)

Confirmed on `vali.local`: aarch64, **Ubuntu 24.04.4 LTS, Python 3.12.3**,
**avahi active** (mDNS discovery will work); `pipx`/`aprilcam` not installed;
`eric` not in `video`. `vidar.local` resolves but its SSH key isn't in place yet.

- **Build (Mac):** bump version first (`[[feedback_bump_version]]`,
  `[[feedback_version_scheme]]` `0.YYYYMMDD.N`), then `python -m build --wheel`
  (or `uv build`) → pure‑Python `aprilcam-<ver>-py3-none-any.whl`. pip resolves
  **aarch64/cp312** wheels for opencv‑contrib, grpcio, numpy on the Pi.
- **Provision each Pi (as `eric`):** `apt install python3-venv python3-pip pipx
  v4l-utils libgl1 libglib2.0-0 avahi-daemon`; `sudo usermod -aG video eric`;
  `pipx ensurepath`; create `/home/eric/aprilcam-data/{cameras,playfields}`.
- **Install (pinned):** `scp` the wheel into `~/wheels`, then
  `pipx install "aprilcam[daemon]==<ver>" --pip-args "--find-links /home/eric/wheels"`.
- **systemd unit** `/etc/systemd/system/aprilcamd.service` (foreground daemon so
  systemd supervises; keep `--unix` for local CLI on the Pi):
```ini
[Unit]
Description=AprilCam daemon
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=eric
SupplementaryGroups=video
RuntimeDirectory=aprilcam
Environment=APRILCAM_DATA_DIR=/home/eric/aprilcam-data
Environment=APRILCAM_SOCKET_DIR=/run/aprilcam
Environment=APRILCAM_LOG_LEVEL=INFO
ExecStart=/home/eric/.local/share/pipx/venvs/aprilcam/bin/python -m aprilcam.daemon --tcp --tcp-port 5280 --unix
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
- **Seed calibration:** `rsync` the Mac's `data/aprilcam/{cameras,playfields}` to
  the Pi's `APRILCAM_DATA_DIR`, or calibrate fresh via the remote MCP. OV9782
  needs manual low exposure (`[[project_ov9782_exposure_tuning]]`).
- **Network:** open TCP **5280** on the LAN (ufw if active); stream ephemeral
  ports bind `0.0.0.0` (note firewall implications for streaming).

**Deployment risks to verify first:**
- **No camera attached to `vali` yet** — only `/dev/video10–31` (Pi codec/ISP
  nodes), **no `/dev/video0`**. Plug in the OV9782 before bring‑up.
- **`vidar` SSH key** — passwordless auth to `eric@vidar.local` is currently
  **rejected**; fix it before deploying there. (`vali` confirmed working.)
- **`mss` headless import** — `mss` is a `daemon` extra needing X11 libs on Linux;
  confirm `import mss` is **lazy** (screen‑capture only). If a top‑level import
  breaks the headless daemon, make it lazy or add a `pi`/`daemon-headless` extra
  excluding `mss`. **Blocks daemon startup if wrong.**
- **opencv‑contrib aarch64/cp312 wheel** — confirm pip finds it; fallback to
  piwheels (`--extra-index-url https://www.piwheels.org/simple`) or apt
  `python3-opencv`.
- `[[project_aprilcam_dual_install]]`: redeploy cleanly so stale installs don't
  serve old code.

**Security:** gRPC is insecure (no TLS), TCP binds all interfaces — acceptable on
a trusted lab LAN; restrict via firewall if needed. No TLS in v1.

---

## Files to change (by area)

- **proto / regen**: `proto/aprilcam.proto` (+`EnumerateCameras`, file‑proxy RPCs),
  regenerate `src/aprilcam/proto/aprilcam_pb2*.py`.
- **daemon**: `daemon/grpc_server.py` (new handlers), `daemon/stream.py`
  (bind `0.0.0.0`). `daemon/camera_pipeline.py` = the one sanctioned opener.
- **client**: `client/control.py` (remote/no‑spawn + new stubs),
  `client/stream.py` (host‑aware), **new** `client/discovery.py` (mDNS browse).
- **MCP server**: `server/mcp_server.py` (remove direct‑camera paths, RPC frame
  fetch, RPC files, `list_cameras`→enumerate RPC, argv parsing, active target,
  `connect_daemon`, `get_version` target), `server/web_server.py` (inherits).
- **CLI**: **new** `cli/_daemon.py` (shared connect/argparse), and
  `cli/tags_cli.py`, `cli/cameras_cli.py`, `cli/calibrate_cli.py`,
  `cli/view_cli.py` converted to daemon clients with discovery.
- **vision**: `vision/objects.py` (drop own capture).
- **config**: `config.py` (`daemon_host`/`daemon_port` + env).
- **shared parse refactor**: `calibration/calibration.py`, `camera/camera_config.py`.
- **packaging**: `pyproject.toml` (move `zeroconf` to base deps; version bump;
  optional `pi`/`daemon-headless` extra).
- **deploy assets (new)**: `deploy/aprilcamd.service`, `deploy/provision-pi.sh`,
  `deploy/README.md` runbook.

## Verification

1. **Local TCP simulation (no Pi):** run the daemon locally and connect a client
   over TCP; walk the golden path — `open_camera` → `create_playfield` →
   `get_tags/get_objects/where` → `capture_frame` + `detect_circles` →
   `calibrate_playfield` (→ `SetCalibration`) → `create_path`/`list_paths`
   (→ `SetPaths`/`GetPaths`). Exercises W1, W3, W4 without hardware.
2. **Discovery scenarios:** (a) one daemon advertising → CLI/`mcp` auto‑select it;
   (b) two daemons → command errors until `APRILCAM_DAEMON_HOST` is set, then
   targets the right one; (c) no daemon → **hard error** ("no aprilcam daemon
   found …"), no spawn; (d) on‑box CLI with a running local daemon → uses the
   Unix socket without spawning.
3. **Sole‑camera‑owner check:** with the daemon stopped, confirm **no** client
   (`aprilcam tags/cameras`, MCP `start_detection`/`list_cameras`) opens a local
   camera — they report "daemon unreachable", not a captured frame. Grep proves
   `VideoCapture` on a device exists only in `daemon/camera_pipeline.py`.
4. **Runtime switch:** `connect_daemon(host="vali.local")` → cameras list; then
   `connect_daemon(host="vidar.local")` → state reset + that Pi's cameras;
   `get_version` reports the active target.
5. **Unit tests:** discovery resolver (0/1/N daemons + env disambiguation),
   remote `DaemonControl` raises (not spawns) when unreachable, file‑proxy RPC
   round‑trips, `connect_daemon` session reset, stream‑consumer host selection.
6. **Live on the Pis:** deploy to `vali.local` (and `vidar.local` once its key is
   fixed); from the Mac, run the CLI (`aprilcam cameras`, `aprilcam tags`) and the
   MCP golden path against the auto‑discovered daemon, then switch between the two.
7. Bump version and run the suite before committing (`.claude/rules/git-commits.md`).

## Process / sequencing

CLASI project (no `.clasi/oop`) — run as a new sprint ("Remote daemon + single
camera owner over TCP"). Suggested ticket order: **W1 enumerate RPC + remove
direct‑camera paths → W2 discovery/resolver/`DaemonControl` + shared CLI helper →
W4 proto + file‑proxy RPCs (regen) + daemon handlers + MCP wiring → W3 streaming →
W2 `connect_daemon` tool → W5 deploy assets → live bring‑up on the Pis.** Confirm
whether to drive this through full sprint ceremony or an OOP bypass before
implementation.
