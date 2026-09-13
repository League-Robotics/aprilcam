# AprilCam Pi Deployment Runbook (v2)

Deploy the v2 `aprilcam` daemon as a systemd service on a Raspberry Pi with
CSI cameras. Written for `vidar.local` (Raspberry Pi 5, Ubuntu 24.04
aarch64, Python 3.12, two IMX296 global-shutter CSI cameras), sprint 040.

**This document is offline, foundation work (ticket 001): every step below
is written and reviewed by inspection, but has not yet been run against
real hardware.** Ticket 003 validates the capture-path code; ticket 004
runs this runbook against `vidar.local` for real and records the outcome
back into its own ticket file (including honest "not verified" notes where
applicable) -- do not read this README as a claim that it has already
been executed successfully.

**v2 is simpler than v1 was.** The daemon takes **no flags** --
`python -m aprilcam.daemon` is exactly `daemon/__main__.py:main()`, with no
`--tcp`/`--unix`/`--force` (those were v1-only). No `APRILCAM_SYSTEM=1` or
equivalent env var is needed either: `config/directories.py`'s
`Directories` resolves `/etc/aprilcam`, `/var/lib/aprilcam`, `/run/aprilcam`
via `os.access`, not `euid == 0` -- systemd's `ConfigurationDirectory=`/
`StateDirectory=`/`RuntimeDirectory=` directives create and chown those
paths to the service user before `ExecStart` ever runs, which is all
`Directories` checks. If you have used v1's `deploy/` before, drop every
reference you remember to `APRILCAM_SYSTEM`, `--force`, `/tmp/aprilcamd.lock`,
and `APRILCAM_DATA_DIR` -- none of them exist or do anything in v2.

## Prerequisites

- Dev machine: Python 3.10+, `uv`, SSH key access to the Pi (passwordless
  sudo on the target account).
- Pi: Ubuntu 24.04 aarch64, two (or more) CSI cameras physically attached.
- **A working libcamera + PiSP install already on the Pi.** Ubuntu's stock
  libcamera (0.2.0) has no Raspberry Pi 5 PiSP support -- `cam -l` finds
  zero cameras even though the kernel detects the sensors. `vidar.local`
  already has a from-source libcamera 0.5.x + libpisp build installed
  under `/usr/local` (confirmed via SSH while writing this ticket). A
  genuinely fresh Pi needs that **one-time** build done by hand first --
  follow `archive/docs/knowledge/raspberry-pi-camera-setup.md`'s "One-time
  system build" section. `provision-pi.sh` below does not attempt this
  build itself; it only apt-installs the packaged GStreamer libcamera
  plugin as a fallback, and only when no working `cam -l` is already
  found (installing the stock package over a working from-source build
  would be pointless -- v1's own setup script explicitly retires it in
  that case).

## Step 1 -- Build the wheel (dev machine)

```bash
uv build --wheel
```

The wheel lands in `dist/aprilcam-<version>-*.whl`. Note the version --
you'll pin it explicitly in Step 4.

## Step 2 -- Provision the Pi

```bash
./deploy/provision-pi.sh <user>@vidar.local
```

This is idempotent (safe to re-run) and:

- installs `v4l-utils`, `gstreamer1.0-tools`, `pipx` via `apt-get`, plus
  the libcamera GStreamer plugin package as a fallback only (see
  Prerequisites above -- it does not touch a working from-source build);
- creates the dedicated system user `aprilcam` (`useradd --system
  --no-create-home --home-dir /var/lib/aprilcam --shell
  /usr/sbin/nologin`) -- not `ros`/`jtl`/any human login (see sprint.md's
  Design Rationale for why: AprilCam must not become a hidden dependency
  of an Ansible-managed account, and must not perpetuate v1's `jtl`
  checkout's identity);
- adds `aprilcam` and your own (invoking) account to the `video` group;
- creates `/var/lib/aprilcam` (owned by `aprilcam`) -- this doubles as
  `$HOME` for the pipx install in Step 4, ahead of `aprilcamd.service`'s
  own `StateDirectory=aprilcam` creating/re-chowning the same path later;
- installs `deploy/modprobe.d/aprilcam.conf` and
  `deploy/modules-load.d/aprilcam.conf` to `/etc/modprobe.d/aprilcam.conf`
  / `/etc/modules-load.d/aprilcam.conf` (only if the content actually
  changed), and loads/reloads `v4l2loopback` only if the config changed
  or the module was not already loaded -- never unconditionally, so
  re-running this script cannot yank the loopback device out from under
  an already-running bridge.

You can also run it directly on the Pi (`sudo ./deploy/provision-pi.sh
--local`) if you'd rather not depend on the dev machine having SSH access.

## Step 3 -- Copy the wheel, install with pipx (as the `aprilcam` user)

```bash
scp dist/aprilcam-*.whl <user>@vidar.local:/tmp/
ssh <user>@vidar.local
sudo -u aprilcam -H pipx install "/tmp/aprilcam-<version>-*.whl[daemon]"
```

`-H` sets `$HOME` to `aprilcam`'s recorded home (`/var/lib/aprilcam`,
created in Step 2) for the duration of the command, which is what makes
pipx's install location predictable: `/var/lib/aprilcam/.local/share/pipx/
venvs/aprilcam/bin/python` -- exactly what `deploy/aprilcamd.service`'s
`ExecStart=` hardcodes. If you install some other way (a different pipx
`--python`, a system-wide pipx install, etc.), update `ExecStart=` in
`deploy/aprilcamd.service` to match before installing the unit.

**Verify the import is clean on headless Linux** (no `$DISPLAY`) before
going further -- this is ticket 002's full audit, but a quick smoke test
here catches an obviously broken install early:

```bash
sudo -u aprilcam -H /var/lib/aprilcam/.local/share/pipx/venvs/aprilcam/bin/python -c "import aprilcam.daemon"
```

## Step 4 -- Install and enable the camera bridge units

**Order matters: module config (already active from Step 2) before the
bridge before the daemon** (sprint.md Migration Concerns) -- installing
the daemon unit first just means it starts with zero cameras (which it
does not fail on), but doing things out of order defeats the point of
verifying each layer independently.

```bash
scp deploy/aprilcam-camera-bridge <user>@vidar.local:/tmp/
scp deploy/aprilcam-bridge@.service <user>@vidar.local:/tmp/
ssh <user>@vidar.local <<'REMOTE'
sudo install -m 0755 /tmp/aprilcam-camera-bridge /usr/local/bin/aprilcam-camera-bridge
sudo install -m 0644 /tmp/aprilcam-bridge@.service /etc/systemd/system/aprilcam-bridge@.service
sudo systemctl daemon-reload
sudo systemctl enable --now aprilcam-bridge@0 aprilcam-bridge@1
REMOTE
```

(`@0`/`@1` -- vidar has two CSI cameras at positions 0 and 1, matching the
module config's `video_nr=70,71`. A third camera would be `@2`, reading
`/dev/video72`, with a matching addition to the module config's
`video_nr=`/`card_label=` lists.)

**Verify** (per SUC-001's acceptance criteria):

```bash
systemctl is-active aprilcam-bridge@0 aprilcam-bridge@1   # both -> active
v4l2-ctl -d /dev/video70 --get-fmt-video                  # 1280x720, 'YUYV'
v4l2-ctl -d /dev/video71 --get-fmt-video
```

## Step 5 -- Install and enable the daemon unit

```bash
scp deploy/aprilcamd.service <user>@vidar.local:/tmp/
ssh <user>@vidar.local <<'REMOTE'
sudo install -m 0644 /tmp/aprilcamd.service /etc/systemd/system/aprilcamd.service
sudo systemctl daemon-reload
sudo systemctl enable --now aprilcamd
REMOTE
```

**Verify** (per SUC-002's acceptance criteria):

```bash
systemctl status aprilcamd                    # active (running)
journalctl -u aprilcamd -n 50 --no-pager      # Directories resolution log
                                               # line (source=default for
                                               # all three roots), TCP
                                               # bind, mDNS advertising --
                                               # no traceback
```

Then, from the dev machine (or any host on the same LAN):

```bash
aprilcam daemon list                          # vidar's daemon discoverable by name
aprilcam camera list -d vidar.local           # both CSI cameras usable
```

## Step 6 -- Reboot persistence

```bash
ssh <user>@vidar.local sudo systemctl reboot
# wait for the Pi to come back...
ssh <user>@vidar.local systemctl is-active aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd
aprilcam daemon list          # rediscovered with no manual intervention
```

## Notes

- **The daemon's singleton guard.** v2's one-instance lock is
  `<socket_dir>/daemon.lock` (`/run/aprilcam/daemon.lock` under this
  unit's `RuntimeDirectory=`), acquired inside `daemon/lifecycle.py` --
  unrelated to `PrivateTmp` and, unlike v1, not a machine-global
  `/tmp/aprilcamd.lock` and not overridable with a `--force` flag (v2 has
  none). If some other `aprilcamd` process already holds the lock when
  `systemctl start aprilcamd` runs, the unit fails to start rather than
  taking over -- find and stop the stray process (`pgrep -fl "python.*-m
  aprilcam.daemon"`) before restarting the unit. In practice this only
  matters during ticket 004's v1-retirement step, when a stray v1
  `jtl`-owned instance could still be running.
- **Firewall.** The daemon binds `0.0.0.0:5280` (gRPC control plane) and
  mDNS-advertises over UDP 5353. On a host with `ufw`:
  ```bash
  sudo ufw allow 5280/tcp comment "AprilCam gRPC control"
  sudo ufw allow 5353/udp comment "AprilCam mDNS"
  ```
  This is unauthenticated, plaintext gRPC -- intentional for a trusted LAN
  (sprint 028); never expose port 5280 to the public internet.
- **`aprilcam[daemon]`'s compiled dependencies on aarch64/cp312 -- audited,
  sprint 040 ticket 002.** `pip install "aprilcam[daemon]"` into a
  throwaway venv on `vidar.local` (Ubuntu 24.04.4 LTS aarch64, Python
  3.12.3) resolved real manylinux `cp312`/`aarch64` wheels for every
  compiled daemon-extra package straight from PyPI --
  `opencv-contrib-python==5.0.0.93`, `grpcio==1.83.1`, `numpy==2.5.3`,
  `grpcio-reflection==1.83.1`, `msgpack==1.2.2`, `mss==10.2.0`,
  `websockets==17.1`, `fpdf2==2.8.8`, `cv2-enumerate-cameras==1.3.3` --
  with no sdist-from-source build for any of them. **No piwheels
  fallback is needed**; do not add `--extra-index-url
  https://www.piwheels.org/simple` unless a future dependency bump
  changes this.
  `import aprilcam.daemon.__main__` (the exact module `python -m
  aprilcam.daemon` runs) was also confirmed clean with `$DISPLAY` unset:
  no `mss` import happens as a side effect (mechanically enforced by
  `tests/test_daemon_extra_headless_import.py`), and `cv2` itself
  imports without error. One caveat worth carrying into ticket 004:
  `opencv-contrib-python`'s compiled extension has a hard (not
  lazily-`dlopen`ed) ELF dependency on `libGL.so.1`/`libX11.so.6`, which
  happened to already be present on vidar as automatic dependencies of
  its desktop/GNOME and ROS packages (`pemmican-desktop`,
  `gnome-remote-desktop`, `ros-kilted-*`) -- vidar is not a minimal
  Ubuntu Server install. A genuinely minimal headless install without
  those packages has not been tested and might need `libgl1`/`libx11-6`
  installed via apt for `import cv2` to succeed; this is *not* added to
  `provision-pi.sh` speculatively (this ticket's rule is "only add a
  fallback if resolution actually fails," and it did not fail here) --
  if ticket 004 hits an `ImportError: libGL.so.1: cannot open shared
  object file` on a leaner image, that is the fix.
- **v1 units still on vidar** (`/etc/systemd/system/aprilcamd.service`,
  `/etc/systemd/system/aprilcam-bridge@.service`, both currently disabled)
  are retired by ticket 004, after these v2 units are verified working --
  not by this ticket, and not by this script.

## Files in this directory

| File | Installed to | Purpose |
|------|---------------|---------|
| `aprilcamd.service` | `/etc/systemd/system/aprilcamd.service` | Supervises the daemon process. |
| `aprilcam-camera-bridge` | `/usr/local/bin/aprilcam-camera-bridge` | libcamera->v4l2loopback bridge script, one process per camera. |
| `aprilcam-bridge@.service` | `/etc/systemd/system/aprilcam-bridge@.service` | Templated unit, one instance per camera position (`@0`, `@1`, ...). |
| `modprobe.d/aprilcam.conf` | `/etc/modprobe.d/aprilcam.conf` | v4l2loopback module options (device numbers, card labels, buffer count). |
| `modules-load.d/aprilcam.conf` | `/etc/modules-load.d/aprilcam.conf` | Autoloads v4l2loopback at boot. |
| `provision-pi.sh` | (run, not installed) | Idempotent apt/user/group/module-config setup for a fresh Pi. |
