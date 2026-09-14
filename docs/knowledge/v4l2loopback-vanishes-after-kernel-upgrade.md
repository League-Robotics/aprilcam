# v4l2loopback vanishes after a kernel upgrade (CSI cameras fail to open)

Ported from the v1 knowledge doc (`archive/docs/knowledge/
v4l2loopback-vanishes-after-kernel-upgrade.md`) during sprint 040. The
underlying failure mode is a **kernel/DKMS module-build problem**, entirely
unrelated to which daemon version (v1 or v2) reads the resulting loopback
devices — nothing about this doc's diagnosis or fix changed for v2. Only the
service names, paths, and recovery command below were updated to match v2's
actual `deploy/` assets and service user.

**Symptom:** On a Pi CSI-camera host (validated on **vidar.local**), after
some time / a reboot, the CSI cameras stop opening. From a client:

```
$ aprilcam camera image -d vidar.local imx296
error: daemon at 'vidar.local' did not answer: no frame available from camera 'imx296'
```

or, depending on how far the failure goes, `camera list` shows the CSI
cameras `absent` entirely. Any USB/UVC camera on the same host is
unaffected — **only the CSI cameras break.** This is a host hardware-layer
problem, not an AprilCam bug.

## Root cause

The CSI cameras reach the daemon through a **`libcamera → v4l2loopback`
bridge** (see [`raspberry-pi-camera-setup.md`](raspberry-pi-camera-setup.md)
for the v2 architecture). `v4l2loopback` is a **DKMS** module. When the
kernel is upgraded (via `apt`), DKMS must rebuild the module for the new
kernel — but it **silently fails to** if the matching **kernel headers** are
not installed. After the next reboot into the new kernel there is no
`v4l2loopback.ko`, so:

- `modprobe v4l2loopback` (autoloaded via `/etc/modules-load.d/aprilcam.conf`)
  fails silently at boot.
- The loopback devices (`/dev/video70`, `/dev/video71`) are never created,
  so the `aprilcam-bridge@0`/`@1` units have nothing to write to (they
  themselves may still show `active` — nothing about the bridge script
  itself failed, its target device just isn't there).
- The daemon's `V4l2LoopbackBackend` for that camera has no device to read
  and reports the camera unusable — a clean failure, not a crash, but
  functionally identical to a dead camera from the outside.

The kernel itself still detects the sensors — `dmesg` shows
`imx296 10-001a: found IMX296LQ` — which makes it look like a camera problem
when it's really a **module-build** problem one layer down.

## Diagnosis (confirm before fixing)

```bash
# 1. Is the module even loaded?
lsmod | grep v4l2loopback                 # -> empty means NOT loaded

# 2. Which kernel is running, and did DKMS build for it?
uname -r                                  # e.g. 6.8.0-1060-raspi
dkms status | grep v4l2loopback           # lists kernels it built FOR
find /lib/modules/$(uname -r) -name 'v4l2loopback*'   # empty = not built for running kernel

# 3. Are the headers for the running kernel present? (the real gap)
ls -d /usr/src/linux-headers-$(uname -r)  # "No such file" = headers missing

# 4. Prove the kernel changed
grep -E 'linux-image|v4l2loopback' /var/log/apt/history.log | tail
```

The smoking gun: `dkms status` shows v4l2loopback built for the *previous*
kernel (e.g. `-1057`) but **not** the running one (e.g. `-1060`), and
`/usr/src/linux-headers-<running>` is missing.

## Fix

Install the headers for the running kernel — this **auto-triggers DKMS** to
build and install the module — then load it and restart the daemon and
bridges. Needs `sudo` (on vidar, SSH as `ros`, passwordless sudo — v2's
switch-over uses that account, not v1's `jtl`).

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y linux-headers-$(uname -r)
# ^ DKMS autoinstall runs here; watch for "dkms autoinstall ... succeeded for v4l2loopback"

sudo modprobe v4l2loopback                 # picks up options from /etc/modprobe.d/aprilcam.conf
lsmod | grep v4l2loopback                  # confirm loaded
ls -l /dev/video70 /dev/video71            # confirm loopback devices exist

sudo systemctl restart aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd
systemctl is-active aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd   # all -> active
v4l2-ctl -d /dev/video70 --get-fmt-video   # -> 1280x720 YUYV means frames flowing
```

Then verify end-to-end from a client:

```bash
aprilcam camera list -d vidar.local
aprilcam camera image -d vidar.local <slug> -o /tmp/check.jpg
```

## Prevent recurrence

The single-kernel `linux-headers-<version>` package does **not** track future
kernels, so this recurs on every kernel upgrade. Install the **meta** package
so new kernels always pull matching headers and DKMS rebuilds automatically:

```bash
sudo apt-get install -y linux-headers-raspi    # meta pkg, tracks the raspi kernel line
```

(Confirmed missing on vidar during the original, v1-era incident that
surfaced this — the box had only per-version headers for old kernels; not
re-verified as still missing under v2, since the module never needed
rebuilding during sprint 040's live sessions, but the same meta-package
mitigation applies either way.) After a kernel upgrade, sanity-check with
`dkms status | grep v4l2loopback` before rebooting into the new kernel.

## Reference — the config that must line up

`/etc/modules-load.d/aprilcam.conf` autoloads the module:

```
v4l2loopback
```

`/etc/modprobe.d/aprilcam.conf` supplies its options — **reconfirmed live,
unchanged, sprint 040 tickets 001/004**:

```
options v4l2loopback video_nr=70,71 \
  card_label=aprilcam-imx296-88000,aprilcam-imx296-80000 \
  exclusive_caps=1,1 max_buffers=16
```

`video_nr=70,71` → the loopback devices; `card_label` → the two IMX296
cameras (`-88000` = camera i2c@88000, position 0, `/dev/video70`; `-80000` =
camera i2c@80000, position 1, `/dev/video71`). `max_buffers=16` is not
incidental — see
[`raspberry-pi-camera-setup.md`](raspberry-pi-camera-setup.md#the-v4l2loopback-module-config-and-why-max_buffers16-matters)
for why a smaller value silently stalls the reader side. Both files under
`deploy/modprobe.d/`/`deploy/modules-load.d/` in this repo are the
source of truth `provision-pi.sh` installs from — a diff against vidar's
live `/etc/modprobe.d/aprilcam.conf`/`/etc/modules-load.d/aprilcam.conf`
should always be empty.

## Related

- [`raspberry-pi-camera-setup.md`](raspberry-pi-camera-setup.md) — the full
  v2 bridge architecture this failure mode breaks, and the rest of sprint
  040's live-hardware findings.
- Host/user note (v2): `vidar.local` = SSH user `ros` (Ansible-managed
  account, passwordless sudo); the AprilCam daemon and bridges themselves
  run as the dedicated `aprilcam` system user, never as `ros` or a human
  login. (v1-era note, no longer current: `vidar.local` was `jtl`,
  `vali.local` was `eric` — that install is retired, see sprint 040 ticket
  004.)
- A daemon restart (as the fix requires) is handled the same way it always
  was — clients simply reconnect once the daemon is back; there is no
  persistent client-side state to recover.
