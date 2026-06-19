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

---

## Step 6 — Seed calibration data

**Option A — rsync from existing data dir**:

```bash
rsync -av data/aprilcam/ eric@vali.local:~/aprilcam-data/
```

**Option B — remote calibrate via MCP**:

Connect to the Pi daemon from a dev machine MCP client and run the
`calibrate_playfield` tool. The daemon writes calibration.json and
playfield.json directly to `~/aprilcam-data/`.

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
| Daemon won't start — duplicate instance | Check `journalctl -u aprilcamd`; remove stale pidfile if needed: `rm /run/aprilcam/aprilcamd.pid` |
