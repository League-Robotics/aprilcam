---
title: Operating the Daemon
blurb: Install, run, configure, and troubleshoot aprilcamd — the process that owns the cameras and does all vision.
order: 40
updated: 2026-06-21
tags: [daemon, cli, config, systemd, ops]
---

# Operating the AprilCam Daemon

`aprilcamd` is the single long-running process that **owns every camera**, runs
AprilTag/ArUco detection and homography, and serves per-frame data to all
clients (the [MCP server](mcp-server.md), the [`DaemonControl`
library](robot-direct-api.md), and the `aprilcam view` window). It is the sole
camera opener and the sole vision authority — clients never touch hardware or
OpenCV.

This page is about *running* the daemon. For the gRPC/stream contract, see the
[Daemon Wire Protocol](daemon-interface.md).

## Install

The daemon needs OpenCV, so install it with the `daemon` extra on the host the
cameras are plugged into:

```
pipx install 'aprilcam[daemon]'
```

Thin clients elsewhere use the base install (no OpenCV). See
[install tiers](overview.md#install-tiers).

## Run

```
aprilcam daemon start          # start in the background
aprilcam daemon status         # running/stopped + open cameras
aprilcam daemon stop
aprilcam daemon restart
aprilcam daemon start -v       # foreground, INFO logs (-vv for DEBUG)
python -m aprilcam.daemon      # run in the foreground directly
```

You start the daemon **explicitly** — on the camera host (`aprilcam daemon
start`) or as a systemd service (below). Clients do **not** auto-spawn it: an MCP
server, `DaemonControl`, the CLI, or the viewer that can't find a daemon fails
fast with `DaemonNotFoundError` ("No aprilcam daemon found …") instead of
launching one. The daemon refuses to start a second instance (exclusive `flock`
on its pidfile) and runs until `Shutdown` or a kill signal — it does **not** exit
when idle.

## Connecting (local and remote)

The daemon listens on three things at once:

- a **Unix socket** at `<socket_dir>/control.sock` (local clients),
- **TCP `0.0.0.0:5280`** (remote clients across the network), and
- an **mDNS / Bonjour** advertisement (`_aprilcam._tcp`, TXT `host=<hostname>`)
  so clients can find it with no configuration.

Every client (the [MCP server](mcp-server.md), the CLI, the
[`DaemonControl` library](robot-direct-api.md), the viewer) resolves a target in
this order — and **never spawns** a daemon:

1. an explicit **`--host` / `--port`** flag, or **`APRILCAM_DAEMON_HOST` /
   `APRILCAM_DAEMON_PORT`**;
2. a running **local** daemon on the default Unix socket;
3. **mDNS discovery** — exactly one daemon → use it; several → pick one with
   `APRILCAM_DAEMON_HOST`; none → a clear `No aprilcam daemon found` error.

`--host` / `--port` are **global** flags, accepted before *or* after the
subcommand. They also accept a host *letter* once you have run `aprilcam probe`
(below). `.local` mDNS names resolve transparently (the client resolves them to
an IP before dialing gRPC):

```
aprilcam cameras                       # auto-discover the daemon on the LAN
aprilcam --host vidar.local cameras    # talk to a specific remote daemon
aprilcam cameras --host 192.168.1.40   # …by hostname or IP
```

Set `APRILCAM_DAEMON_HOST=vidar.local` (in `~/.aprilcam`, `/etc/aprilcam.env`,
or `.env`) to make every client target a remote daemon by default.

### `aprilcam probe` and short codes

`aprilcam probe` discovers every reachable daemon (local + mDNS), enumerates
their cameras, and saves a host store at `<data_dir>/hosts.json` with **stable**
host and camera numbers. Each camera then has a compact **base-26 code**: a
single letter for a **local** camera (`C` = local camera 3), or host-letter +
camera-letter for a **remote** one (`FB` = host F, camera 2).

```
aprilcam probe                         # A  vidar  [remote] -> AA imx296-88000,  AB imx296-80000
aprilcam cameras --host vidar.local    # [6] AA imx296-88000   [7] AB imx296-80000
aprilcam --host A cameras              # address a host by its letter
aprilcam view AB                       # open vidar's 2nd camera by its code
```

## Cameras

The daemon assigns each camera a **persistent enumeration number** on first
sight and stores it in `<data_dir>/cameras/registry.json`. That number — not
the volatile OS device index — is the stable handle shown by `aprilcam cameras`
and accepted everywhere a camera number is taken:

```
aprilcam cameras               # [1] Brio 501  [3] Arducam OV9782  [4] Samizdat
aprilcam cameras --details     # also shows slug + os-index for debugging
aprilcam view 3                # open the live view for camera 3
```

Run `aprilcam probe` to also tag each camera with a short alpha code (see
[Connecting](#connecting-local-and-remote)) — convenient for addressing cameras
across multiple hosts.

Each camera gets a directory `<data_dir>/cameras/<slug>/` holding:

| File | Owner | Contents |
|------|-------|----------|
| `config.json` | developer (static) | device name, resolution, UVC `settings`, `camera_position`, `static_marker_ids`, linked `playfield` |
| `calibration.json` | daemon (regenerable) | homography, `corner_pixels`, `static_markers`, `tags_used`, intrinsics |
| `info.json` | daemon | runtime pointers (e.g. the paths file) |
| `paths.json` | MCP server | persistent waypoint paths for the viewer |

The split matters: `config.json` is hand-owned and never overwritten by
calibration; `calibration.json` is regenerated by `aprilcam calibrate` /
`calibrate_playfield` and must not carry the config-owned keys.

## Calibrate

Calibration maps pixels → real-world cm and is normally done once per camera on
the daemon host:

```
aprilcam calibrate 3                       # calibrate camera 3 from its linked playfield
```

After that, clients get world coordinates automatically (`open_camera` →
`playfield_id`). Agents can also calibrate via the MCP `calibrate_playfield`
tool. Field dimensions come from the linked playfield definition, not from
arguments.

## Configuration

All paths and tunables come from `Config` (`src/aprilcam/config.py`), resolved
in priority order (highest wins):

| Priority | Source |
|----------|--------|
| highest | `APRILCAM_*` environment variables |
| | `.env` (walking up from CWD) |
| | `.aprilcam` project dotfile (walking up from CWD) |
| | `~/.aprilcam` user dotfile |
| lowest | `/etc/aprilcam.env`, `/etc/aprilcam/aprilcam.env` |

Key variables:

| Key | Default | Description |
|-----|---------|-------------|
| `APRILCAM_DATA_DIR` | FHS `/var/lib/aprilcam` · XDG `~/.local/share/aprilcam` | Cameras, calibrations, playfields. |
| `APRILCAM_SOCKET_DIR` | FHS `/run/aprilcam` · XDG `$XDG_RUNTIME_DIR/aprilcam` (falls back to a per-user temp dir, e.g. `$TMPDIR/aprilcam-<uid>` on macOS) | Control socket, stream sockets, pidfile. |
| `APRILCAM_LOG_DIR` | FHS `/var/log/aprilcam` · XDG `~/.local/state/aprilcam` | `aprilcamd.log`. |
| `APRILCAM_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `APRILCAM_DETECTION_FPS` | `10` | Detection loop frame-rate cap. |
| `APRILCAM_STATIC_DESKEW` | `1` | Homography-derived static-camera deskew (`0` to disable). |
| `APRILCAM_SYSTEM` | `auto` | Force FHS (`1`) or XDG (`0`) layout. Auto = FHS when running as root. |

### Directory layout (FHS vs XDG)

AprilCam picks a layout from whether it runs as root (`euid == 0`) or
`APRILCAM_SYSTEM=1`:

| Path | FHS (root / `APRILCAM_SYSTEM=1`) | XDG (user) |
|------|----------------------------------|------------|
| Data | `/var/lib/aprilcam` | `~/.local/share/aprilcam` |
| Socket / runtime | `/run/aprilcam` | `$XDG_RUNTIME_DIR/aprilcam` (or per-user temp) |
| Logs | `/var/log/aprilcam` | `~/.local/state/aprilcam` |

> **All clients and the daemon must agree on `APRILCAM_SOCKET_DIR`.** A client
> and daemon that resolve different socket directories cannot find each other.
> If you launch the daemon and clients from different environments, set
> `APRILCAM_SOCKET_DIR` explicitly in `/etc/aprilcam.env` or a shared `.env`.

### systemd

Under `DynamicUser=yes` the daemon is not root, but systemd prepares the FHS
directories via `StateDirectory=`, `RuntimeDirectory=`, and `LogsDirectory=`.
Set `APRILCAM_SYSTEM=1` (in the unit's `Environment=` or `/etc/aprilcam.env`)
so the config loader uses those FHS paths.

## Logs

Daemon stderr goes to `<log_dir>/aprilcamd.log`. For interactive debugging run
it in the foreground: `aprilcam daemon start -vv` or `python -m aprilcam.daemon`.

## Troubleshooting

- **A client prints `No aprilcam daemon found`** — no daemon was discovered and
  none was configured. Start one on the camera host (`aprilcam daemon start`, or
  the systemd service) or point the client at it with `--host` /
  `APRILCAM_DAEMON_HOST`. Clients never start a daemon for you.
- **`aprilcam daemon start` reports "did not start within 20 seconds"** — the
  client's readiness probe could not reach the socket. Most often a
  socket-directory mismatch (client and daemon resolved different
  `APRILCAM_SOCKET_DIR`) or a slow cold start. Confirm with
  `aprilcam config | grep socket_dir` on both sides; check `aprilcamd.log`.
- **A second `daemon start` right after `stop` bails with "already running"** —
  shutdown is asynchronous (it stops camera pipelines first). Wait for the
  process to exit (poll `aprilcam daemon status`) before starting again.
- **A camera that was unplugged keeps returning a dead handle** — the capture
  thread exits but the camera stays registered. `close_camera` then
  `open_camera`, or restart the daemon.
- **`ModuleNotFoundError: No module named 'cv2'` from the daemon** — the daemon
  host was installed without the `daemon` extra. Reinstall with
  `pipx install 'aprilcam[daemon]'`. (The MCP server and other thin clients
  correctly run without OpenCV — that error from a *client* means a tool is
  doing pixel work it should delegate to the daemon.)
- **After a code change, behavior doesn't update** — a running daemon/MCP/view
  process holds imported code in memory; restart it to pick up changes.
