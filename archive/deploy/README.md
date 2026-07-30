# AprilCam Pi Deployment Runbook

Deploy the `aprilcam` daemon to a Raspberry Pi (Ubuntu 24.04 aarch64, Python 3.12).

**Target Pi hostnames**: `vali.local`, `vidar.local`

---

## Prerequisites

- Dev machine: Python 3.12+, `uv` (or `build`), SSH access to the Pi.
- Pi: Ubuntu 24.04 aarch64, user `eric`, camera physically attached.
- SSH key auth to `vidar.local` is not yet in place — password auth is
  required. Set up `~/.ssh/authorized_keys` on the Pi first for a
  smoother workflow.

---

## Step 1 — Build the wheel

On your dev machine, from the repo root:

```bash
# Using uv (preferred):
uv build --wheel

# Or using the standard build tool:
python -m build --wheel
```

The wheel lands in `dist/aprilcam-<version>-*.whl`.

---

## Step 2 — Copy wheel to the Pi

```bash
ssh eric@vali.local "mkdir -p ~/wheels"
scp dist/aprilcam-*.whl eric@vali.local:~/wheels/
```

---

## Step 3 — Provision the Pi

Run the provision script once per Pi. It installs apt packages, adds the
user to the `video` group, and creates the data directory.

```bash
./deploy/provision-pi.sh eric@vali.local
```

Packages installed: `python3-venv python3-pip pipx v4l-utils libgl1
libglib2.0-0 avahi-daemon`.

**Log out and back in** on the Pi after provisioning for the `video` group
membership to take effect (required for camera device access).

---

## Step 4 — Install with pipx

SSH to the Pi and run:

```bash
pipx install "aprilcam[daemon]==0.20260619.7" \
    --pip-args "--find-links ~/wheels --extra-index-url https://www.piwheels.org/simple"
```

Replace `0.20260619.7` with the actual wheel version from step 1.

**opencv-contrib fallback**: If pip cannot find `opencv-contrib-python` for
aarch64/cp312, `piwheels.org` provides pre-built wheels. The
`--extra-index-url https://www.piwheels.org/simple` flag in the command
above enables this fallback automatically.

**Verify import on headless Linux** (no display attached):

```bash
python -c "import aprilcam.daemon"
```

This must succeed without a display. The `mss` screen-capture library is
imported lazily inside the daemon — it is only initialised when explicitly
used by a client, so the daemon starts cleanly on headless servers.

---

## Step 5 — Install and enable the systemd service

Copy the service file to the Pi:

```bash
scp deploy/aprilcamd.service eric@vali.local:~/
ssh eric@vali.local "sudo mv ~/aprilcamd.service /etc/systemd/system/ && \
    sudo systemctl daemon-reload && \
    sudo systemctl enable aprilcamd && \
    sudo systemctl start aprilcamd"
```

Check status:

```bash
ssh eric@vali.local "systemctl status aprilcamd"
ssh eric@vali.local "journalctl -u aprilcamd -n 50 --no-pager"
```

**One-daemon-per-machine (sprint 018)**: the unit's `ExecStart` always
passes `--force`, so `systemctl start|restart aprilcamd` takes over the
machine-global lock (`/tmp/aprilcamd.lock`) from any other `aprilcamd`
instance rather than refusing to start — systemd's daemon is meant to
always win. Do **not** add `PrivateTmp=true` to this unit; a private
`/tmp` would give the daemon its own lock file and silently defeat that
guard (see the comment in `deploy/aprilcamd.service`).

---

## Step 6 — Seed calibration data

**Option A — rsync from existing data dir**:

```bash
rsync -av data/aprilcam/ eric@vali.local:/var/lib/aprilcam/
```

Config files (`cameras/*/config.json`, `playfields/*.json`) that land under
`/var/lib/aprilcam` this way still work via the legacy-layout fallback, but
should be relocated to `/etc/aprilcam` with `aprilcam migrate-dirs` (see
"Directory layout" below) so future config writes go to the correct root.

**Option B — remote calibrate via MCP**:

Connect to the Pi daemon from a dev machine MCP client and run the
`calibrate_playfield` tool. The daemon writes `calibration.json` to
`/var/lib/aprilcam` (state) and `config.json` to `/etc/aprilcam` (config)
directly.

---

## Directory layout (FHS)

Since sprint 019, config and state live in **separate FHS roots**,
provisioned by the systemd unit's `ConfigurationDirectory=`/
`StateDirectory=` directives and selected automatically by the daemon
because the unit sets `Environment=APRILCAM_SYSTEM=1` (required — see
"Why `APRILCAM_SYSTEM=1`" below):

| Category | Path | Contents | systemd directive |
|----------|------|----------|--------------------|
| Config (admin-owned) | `/etc/aprilcam` | Per-camera `config.json`, `playfields/*.json` | `ConfigurationDirectory=aprilcam` |
| State (app-generated) | `/var/lib/aprilcam` | `calibration.json`, `registry.json`, `hosts.json`, `info.json`, `paths.json` | `StateDirectory=aprilcam` |
| Runtime (ephemeral) | `/run/aprilcam` | Control socket, stream sockets, pidfile | `RuntimeDirectory=aprilcam` |
| Logs | `/var/log/aprilcam` | `aprilcamd.log` | `LogsDirectory=aprilcam` |

`APRILCAM_CONFIG_DIR` (new in sprint 019) overrides the config root the same
way `APRILCAM_DATA_DIR` overrides the state root; both are left unset in
`deploy/aprilcamd.service` so they resolve to the FHS defaults above via the
systemd-provisioned directories.

### Why `APRILCAM_SYSTEM=1`

`Config._default_dirs()` only auto-selects FHS paths when `os.geteuid() ==
0` **or** `APRILCAM_SYSTEM == '1'`. The unit runs as a static non-root
`User=eric` (no `DynamicUser=`), so without `Environment=APRILCAM_SYSTEM=1`
the daemon would resolve XDG paths under `/home/eric` instead of the
`/etc/aprilcam` / `/var/lib/aprilcam` directories systemd just provisioned —
silently defeating the `ConfigurationDirectory=`/`StateDirectory=`
directives. The flag is required for this unit's `User=` configuration.

### Migrating an existing Pi deployment

Older deployments (prior to sprint 019) pinned
`Environment=APRILCAM_DATA_DIR=/home/eric/aprilcam-data` in the unit,
co-locating config and state under one non-FHS tree. Current units no
longer set that override. To upgrade a Pi that still has data under
`/home/eric/aprilcam-data`, run these steps **in order**:

1. **Migrate config files while the old override is still in effect.** SSH
   to the Pi and, with the unit still running its old
   `APRILCAM_DATA_DIR=/home/eric/aprilcam-data` configuration (or by setting
   that variable one-off for the CLI invocation), run:

   ```bash
   ssh eric@vali.local
   APRILCAM_DATA_DIR=/home/eric/aprilcam-data \
   APRILCAM_CONFIG_DIR=/etc/aprilcam \
       aprilcam migrate-dirs
   ```

   This scans `/home/eric/aprilcam-data/cameras/*/config.json` and
   `/home/eric/aprilcam-data/playfields/*.json` and moves them into
   `/etc/aprilcam` (creating `sudo mkdir -p /etc/aprilcam` first if it
   doesn't already exist as a plain directory — normally
   `ConfigurationDirectory=` creates it on service start, but `migrate-dirs`
   runs standalone here, before the unit is updated). Use `--dry-run` first
   to preview the plan:

   ```bash
   APRILCAM_DATA_DIR=/home/eric/aprilcam-data \
   APRILCAM_CONFIG_DIR=/etc/aprilcam \
       aprilcam migrate-dirs --dry-run
   ```

2. **Manually relocate the remaining state files.** `migrate-dirs` never
   touches state files by design. Move them yourself:

   ```bash
   sudo mkdir -p /var/lib/aprilcam
   sudo rsync -av --remove-source-files \
       /home/eric/aprilcam-data/ /var/lib/aprilcam/ \
       --exclude 'cameras/*/config.json' --exclude 'playfields'
   sudo chown -R eric:eric /var/lib/aprilcam
   ```

   (State files include `calibration.json`, `registry.json`, `hosts.json`,
   `info.json`, `paths.json` under each camera's directory.)

3. **Remove the override and update the unit.** Copy the current
   `deploy/aprilcamd.service` (no `Environment=APRILCAM_DATA_DIR=...` line,
   `Environment=APRILCAM_SYSTEM=1` present) to the Pi and reload:

   ```bash
   scp deploy/aprilcamd.service eric@vali.local:~/
   ssh eric@vali.local "sudo mv ~/aprilcamd.service /etc/systemd/system/ && \
       sudo systemctl daemon-reload"
   ```

4. **Restart and verify.**

   ```bash
   ssh eric@vali.local "sudo systemctl restart aprilcamd"
   ssh eric@vali.local "systemctl status aprilcamd"
   ssh eric@vali.local "journalctl -u aprilcamd -n 50 --no-pager"
   ```

   Then, from an MCP client, confirm the daemon opens cameras and
   `get_tags` returns `world_xy` coordinates — proof calibration data was
   found under the new FHS locations (`/etc/aprilcam` config,
   `/var/lib/aprilcam` state).

### Mac dev migration

The repo's `.env` used to pin `APRILCAM_DATA_DIR=./data/aprilcam`, so a dev
checkout's daemon always read/wrote the repo-local `data/aprilcam/` tree
regardless of CWD-launch location. That override has been removed (sprint
019) — a fresh checkout with no `.env` override now resolves to XDG
defaults: config under `~/.config/aprilcam`, state under
`~/.local/share/aprilcam`, both empty on a machine that has never run
`aprilcam migrate-dirs`.

If you have existing calibrated cameras under this repo's
`data/aprilcam/`, migrate them once:

```bash
APRILCAM_DATA_DIR=./data/aprilcam APRILCAM_CONFIG_DIR=~/.config/aprilcam \
    aprilcam migrate-dirs
```

This moves `data/aprilcam/cameras/*/config.json` and
`data/aprilcam/playfields/*.json` into `~/.config/aprilcam`. State files
(`calibration.json`, `registry.json`, `hosts.json`, `info.json`,
`paths.json`) are not touched by `migrate-dirs`; move them manually into
`~/.local/share/aprilcam` if you want a fully XDG-native layout, or leave
them under `./data/aprilcam` and set
`APRILCAM_DATA_DIR=./data/aprilcam` in your local (untracked) `.env`
override to keep reading state from the repo tree.

---

## Step 7 — Verify discovery

From a machine on the same LAN, verify mDNS discovery works:

```bash
# Should print the Pi's hostname and port 5280
python -c "
from aprilcam.client.discovery import browse_mdns
results = browse_mdns(timeout=5.0)
print(results)
"
```

---

## Firewall note

The daemon binds on **all interfaces** (`0.0.0.0:5280`) for gRPC control.
Tag/image stream producers use **ephemeral TCP ports** (also `0.0.0.0`).

On Ubuntu with `ufw`:

```bash
sudo ufw allow 5280/tcp comment "AprilCam gRPC control"
# Ephemeral stream ports are short-lived — either allow the full range
# or restrict access to the trusted LAN subnet:
sudo ufw allow from 192.168.1.0/24 to any comment "AprilCam LAN"
```

**Security note**: The gRPC channel uses insecure transport (no TLS).
This is intentional for trusted LAN operation. Do not expose port 5280
to the public internet.

---

## OV9782 camera exposure tuning

The OV9782 global-shutter camera requires **manual exposure** for reliable
tag detection. Auto-exposure washes out tags at high frame rates.

Key settings (configure via `aprilcam cameras` or `config.json`):
- `auto_exposure_mode`: must be `"1"` (manual), not `"0"` (full auto)
- `exposure`: start at `8`–`10`; cliff to washout occurs around `12`–`18`

See `project_ov9782_exposure_tuning` memory entry for full details.

---

## Known risks and workarounds

| Risk | Mitigation |
|------|-----------|
| `vidar.local` SSH — no passwordless auth yet | Set up `~/.ssh/authorized_keys` on vidar before automating deploys |
| opencv-contrib wheel not on PyPI for aarch64/cp312 | Use `--extra-index-url https://www.piwheels.org/simple` (included above) |
| Camera not detected after provisioning | Log out and back in for `video` group membership; verify with `v4l2-ctl --list-devices` |
| `mss` import failure on headless Pi | `mss` is lazy-imported; run `python -c "import aprilcam.daemon"` to confirm — if it fails, check for `DISPLAY` env var requirements in `mss` version |
| Daemon won't start — duplicate instance | Check `journalctl -u aprilcamd`; since sprint 018 the singleton guard is a machine-global lock at `/tmp/aprilcamd.lock` (not the per-run pidfile) — the systemd unit's `--force` flag makes `systemctl restart aprilcamd` take over automatically, so this should only occur on a manual (non-systemd) start without `--force` |
