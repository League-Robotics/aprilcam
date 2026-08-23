---
title: Operating the Daemon
blurb: Install, run, configure, and troubleshoot the AprilCam daemon — the single process that owns every camera and does all vision.
order: 5
updated: 2026-08-23
tags: [daemon, cli, config, mdns, systemd, ops]
---

# Operating the AprilCam Daemon

The daemon is the single long-running process that **owns every camera**, runs
AprilTag/ArUco detection, calibration homography, and frame capture, and
answers all clients over gRPC — the [MCP server](mcp-server.md), the
[robot direct API](robot-direct-api.md), and the `aprilcam camera view`
window. Clients never touch hardware or do pixel work themselves.

This page is about *running* the daemon. For the gRPC/stream contract see
[Daemon Wire Protocol](daemon-interface.md); for detection tuning see
[Tag Detection Under Variable Lighting](lighting-and-detection.md).

## Install

Install with the `daemon` extra on the host the cameras are plugged into —
it carries the capture/enumeration stack (`cv2-enumerate-cameras`, `mss`, …):

```bash
pipx install 'aprilcam[daemon]'
```

Thin clients elsewhere use the base install (see
[install tiers](overview.md#install-tiers)).

> **A daemon without the extra starts but sees no cameras.** The failure is
> silent: `daemon status` says "running", `camera list` shows every camera
> `absent`/`unusable`. This bites especially when a pipx install *and* a repo
> checkout coexist — `aprilcam daemon start` spawns the daemon with **the same
> interpreter the CLI itself runs under** (`sys.executable -m aprilcam.daemon`),
> so whichever `aprilcam` is on `PATH` decides which venv the daemon gets. From
> a checkout, `uv run aprilcam daemon start` guarantees the repo venv. See
> [this knowledge entry](https://github.com/League-Robotics/aprilcam/blob/master/docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md).

## Run

```bash
aprilcam daemon start          # spawn the daemon in the background
aprilcam daemon status         # running/stopped, cameras, resolved dirs
aprilcam daemon stop           # SIGTERM; quiet no-op if not running
aprilcam daemon restart        # stop (if running), then start
aprilcam daemon list           # every discovered daemon, local and remote
python -m aprilcam.daemon      # run in the foreground (logs on stderr)
```

You start the daemon **explicitly**. Clients never auto-spawn one: a client
that can't find a daemon fails fast (`no local daemon answered`) instead of
launching one. Key behaviors, all from
[`cli/daemon.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/cli/daemon.py),
[`client/lifecycle.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/lifecycle.py), and
[`daemon/lifecycle.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/daemon/lifecycle.py):

- **Singleton.** The daemon holds an exclusive `flock` on
  `<socket_dir>/daemon.lock`. A second `start` is refused with
  `a daemon is already running: pid <N>` — checked *before* spawning, without
  disturbing the running daemon.
- **Ready means ready.** The control socket is not created until the initial
  camera enumeration has finished, so the moment `daemon start` returns
  (it polls the socket, up to 10 s), `camera list` answers correctly. A start
  that fails reports the daemon's stderr tail:
  `daemon process exited before becoming ready: …`.
- **`start`/`stop`/`restart` are local-only by design.** They accept `-d HOST`
  syntactically but refuse any value with a message pointing at SSH — process
  lifecycle never crosses the network. `daemon status -d HOST` *is* allowed:
  it returns the remote daemon's RPC-answered view.
- **Bare `status` also scans for orphans.** If daemon-shaped processes exist
  on this machine besides the lock holder, `status` prints an
  `orphan processes (local scan, …)` line — report only, never auto-killed.
- **Restart re-reads disk, drops the rest.** Configuration comes back from
  `config_dir` (installed playfields and camera links survive), but anything
  only ever pushed over RPC — live `camera config` overrides, client-registered
  tag mounts — is gone, and open views/streams die and must be reopened.
- **Empty is success.** Starting with zero cameras and zero configuration is
  normal, not an error.

## Connecting (local and remote)

The daemon listens on three things at once
([`daemon/__main__.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/daemon/__main__.py)):

- a **Unix socket** at `<socket_dir>/control.sock` (local clients),
- **TCP `0.0.0.0:5280`** (remote clients; unauthenticated, LAN-scoped), and
- an **mDNS advertisement** of that TCP port (below).

If port 5280 is already taken, the TCP bind failure is logged at WARNING and
the daemon continues unix-socket-only — it never fails to start over the
network, it just isn't advertised.

Every daemon-talking CLI command takes `-d`/`--daemon HOST` (or `HOST:PORT`;
a bare host implies 5280). Target resolution, highest wins:

1. an explicit `-d HOST` flag;
2. `APRILCAM_DAEMON_HOST` in the environment;
3. the local daemon on the default Unix socket.

```bash
aprilcam camera list                    # the local daemon
aprilcam camera list -d vidar.local     # a specific remote daemon
aprilcam daemon list                    # fan out: every daemon on the LAN
aprilcam camera list --all              # fan out a read-only command
```

`daemon list` and `--all` merge a local lock-probe (instant, no network) with
a bounded ~2 s mDNS browse; an unreachable daemon is listed and flagged, never
silently dropped, and "found nothing" is an empty result, not an error.

## Configuration

Resolution lives in
[`config/directories.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/config/directories.py):
a **two-directory model** plus a runtime socket dir, each resolved once at
startup with cascade *flag > env > system default > per-user fallback*.

| Root | System default | Fallback | Env override | Owner |
|------|----------------|----------|--------------|-------|
| `config_dir` | `/etc/aprilcam` (if it exists and is readable) | `~/.config/aprilcam` | `APRILCAM_CONFIG_DIR` | **You.** Hand-authored; the daemon only reads it. |
| `state_dir` | `/var/lib/aprilcam` (if writable/creatable) | `~/.local/share/aprilcam` | `APRILCAM_STATE_DIR` | **The daemon.** Regenerable state. |
| `socket_dir` | `/run/aprilcam` | `$XDG_RUNTIME_DIR/aprilcam`, else `$TMPDIR/aprilcam-<uid>` | *(none — deliberately not configurable)* | Runtime plumbing: `control.sock`, `daemon.lock`, stream sockets. |

What lives where:

```
config_dir/
  playfields/<name>.json          # playfield definitions (surveyed features)
  cameras/<slug>/config.json      # per-camera settings, playfield link, position
state_dir/
  cameras/registry.json           # persistent camera identity -> number map
  cameras/<slug>/calibration.json # daemon-written homography (regenerable)
```

Which roots were chosen is never a mystery: every resolved root and its
cascade tier is logged at INFO on startup, and

```bash
aprilcam config
```

prints the resolved roots, every environment variable the cascade consults
(set/unset), every config/calibration file location (exists/missing), and —
most valuable — every **config-file load error**. At boot the daemon loads
everything under `config_dir` skip-and-warn: one malformed file is logged and
skipped, never refuses startup. The flip side is that a bad file (classic
case: a v1-shaped `config.json` with no `"name"` field) makes its camera
silently never appear; `aprilcam config` turns that into a five-second
diagnosis.

Environment variables actually read by v2 code:

| Variable | Read by | Effect |
|----------|---------|--------|
| `APRILCAM_CONFIG_DIR` | daemon + clients | Overrides `config_dir`. |
| `APRILCAM_STATE_DIR` | daemon + clients | Overrides `state_dir`. |
| `APRILCAM_DAEMON_HOST` | clients | Default `-d` target for every command. |
| `APRILCAM_LOG_LEVEL` | daemon | `DEBUG`/`INFO`/`WARNING`/`ERROR`, default `INFO`. |
| `XDG_RUNTIME_DIR` | both | Socket-dir fallback when `/run/aprilcam` is unavailable. |

(`APRILCAM_DAEMON_PORT` appears in the design docs but no v2 code path reads
it — the port is the fixed constant 5280.)

The daemon and its clients must resolve the **same** `socket_dir` to find each
other; since it is derived, not configured, that just means launching both
from environments with the same uid and `XDG_RUNTIME_DIR`. The spawned daemon
inherits the CLI's environment, so the `APRILCAM_*` variables always agree
across a `daemon start`.

## Cameras: identity and discovery

On first sight of a camera the daemon assigns it a **persistent number** and a
readable **slug** (the slugified device name, e.g.
`arducam-ov9782-usb-camera`), stored in `state_dir/cameras/registry.json`.
The number — not the volatile OS device index — is the stable handle
`aprilcam camera list` shows and every command accepts. Identity across
re-plugs is resolved by a fallback chain (hardware unique ID / USB serial,
then VID:PID+port, then name+resolution) in
[`camera/identity.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/camera/identity.py);
cameras with a real serial survive port moves, identical serial-less models
are told apart by port.

The daemon enumerates actively once at startup, then **watches passively**
(a ~2 s enumeration poll that never opens a device), so a hotplugged camera
shows up in `camera list` within a few seconds. `aprilcam camera probe`
forces a deep re-probe on demand; it never opens a camera that is currently
streaming, and anything it couldn't interrogate is listed under `skipped:`.

## mDNS advertisement

Once the TCP port is bound, the daemon registers `aprilcam-<hostname>` under
`_aprilcam._tcp.local.` with TXT properties `version` and `host`
([`daemon/mdns.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/daemon/mdns.py)).
The advertised address is the host's primary *routable* IPv4 (found via a UDP
route probe — deliberately not `gethostname()` resolution, which on Ubuntu
often maps to the useless loopback alias `127.0.1.1`). Advertising is never
load-bearing: any mDNS failure is logged at WARNING and the daemon runs on;
clients can always reach it explicitly with `-d HOST`. The advertisement is
withdrawn during shutdown, before the listener stops accepting RPCs.

## Logs

The daemon logs to **stderr only** — there is no log directory and no log
file in v2 (that's deliberate: under systemd stderr *is* journald; by hand it
is your terminal). Consequences:

- `aprilcam daemon start` backgrounds the daemon and its output is not
  captured anywhere (the stderr tail is only reported if startup *fails*).
- For interactive debugging run it in the foreground:

```bash
aprilcam daemon stop
APRILCAM_LOG_LEVEL=DEBUG python -m aprilcam.daemon
```

## systemd

The repo ships no unit files yet, but the daemon is designed to run as a
system service: as root, the cascade resolves the FHS roots
(`/etc/aprilcam`, `/var/lib/aprilcam`, `/run/aprilcam`) automatically, and
logs land in journald. A minimal unit is just:

```ini
[Service]
ExecStart=/path/to/venv/bin/python -m aprilcam.daemon
Restart=on-failure
```

where the venv has the `daemon` extra installed. Do not wrap
`aprilcam daemon start` in a unit — that command backgrounds a child and
returns, which is the opposite of what systemd wants.

## Troubleshooting

- **Every camera `absent`/`unusable` after a start/restart** — the daemon is
  running from a venv without the `daemon` extra (see [Install](#install)).
  Check `pgrep -fl "python.*-m aprilcam.daemon"` to see which interpreter it
  got; stop and restart from the right environment.
- **A camera you configured never appears** — its `config.json` was rejected
  at boot (skip-and-warn). Run `aprilcam config` and read the `load errors`
  section; the classic cause is a v1-shaped document with no `"name"` field.
- **`no frame available` from one camera, device still on USB** — the daemon's
  in-process capture session for that device is wedged; `camera probe` may
  flip it back to `usable` but reads still fail. Only a daemon restart
  recovers it; reopen any views afterwards.
- **Image nearly black but tags still detect** (`dark` flag in
  `camera list`) — UVC controls drifted under the daemon (another writer, or a
  lighting change made the tuned exposure wrong). Compare
  `aprilcam camera config <n> --dump` against hardware readback, then
  `aprilcam camera config --reset <n>` re-applies the on-disk config live, no
  restart. Exposure is ambient-dependent — see
  [lighting and detection](lighting-and-detection.md) and
  [the UVC-drift entry](https://github.com/League-Robotics/aprilcam/blob/master/docs/knowledge/dark-camera-image-uvc-drift-reset.md).
- **A client errors `Method not found!` after upgrading** — the running
  daemon predates the code answering it. `aprilcam daemon restart` (any
  long-lived process holds imported code in memory; restart to pick up
  changes).
- **`daemon start` fails with `a daemon is already running: pid N`** — one is
  already up (use `restart`), or an orphan holds the lock: check
  `aprilcam daemon status`'s orphan-process line and kill the stray pid
  yourself — the CLI reports, never kills.
- **`daemon list` finds nothing across the network** — the remote daemon may
  have lost the 5280 bind (port taken → no advertisement; grep its stderr for
  `mdns:`/`TCP` warnings), or multicast is blocked. `-d HOST` still works
  regardless of mDNS.
