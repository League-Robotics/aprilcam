# v4l2loopback vanishes after a kernel upgrade (CSI cameras fail to open)

**Symptom:** On a Pi CSI-camera host (validated on **vidar.local**), after some
time / a reboot, the CSI cameras stop opening. From a client:

```
$ aprilcam view BA
Error: could not open camera 6: <_InactiveRpcError ...
  details = "CameraPipeline: failed to open camera index 6" ...>
$ aprilcam view BB
Error: could not open camera 7: ... "failed to open camera index 7" ...
```

USB cameras on the same host (e.g. the C920 at index 0) still work — **only the
CSI cameras break.** Live tag detection / world coords from other hosts are
unaffected. This is a **host hardware-layer** problem, not an AprilCam bug and
not the CLI code-resolution bug fixed in sprint 017.

## Root cause

The CSI cameras reach the daemon through a **`libcamera → v4l2loopback`
bridge** (see [`raspberry-pi-camera-setup.md`](raspberry-pi-camera-setup.md)
for the architecture). `v4l2loopback` is a **DKMS** module. When the kernel is
upgraded (via `apt`), DKMS must rebuild the module for the new kernel — but it
**silently fails to** if the matching **kernel headers are not installed**.
After the next reboot into the new kernel there is no `v4l2loopback.ko`, so:

- `modprobe v4l2loopback` (autoloaded via `/etc/modules-load.d/aprilcam.conf`)
  fails silently at boot.
- The loopback devices (`/dev/video70`, `/dev/video71`) are never created, so
  the `aprilcam-bridge@0/@1` feeder services have nothing to write to.
- The daemon's camera indices 6/7 fall through to raw CSI **metadata** nodes
  (`/dev/video6` = `rp1-cfe-fe_stats`, `/dev/video7` = `rp1-cfe-fe_config`),
  which OpenCV cannot open → `failed to open camera index 6/7`.

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
build and install the module — then load it and restart the daemon. Needs
`sudo` (on vidar, SSH as `jtl`).

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y linux-headers-$(uname -r)
# ^ DKMS autoinstall runs here; watch for "dkms autoinstall ... succeeded for v4l2loopback"

sudo modprobe v4l2loopback                 # picks up options from /etc/modprobe.d/aprilcam.conf
lsmod | grep v4l2loopback                  # confirm loaded
ls -l /dev/video70 /dev/video71            # confirm loopback devices exist

sudo systemctl restart aprilcamd
systemctl is-active aprilcam-bridge@0 aprilcam-bridge@1   # both -> active
v4l2-ctl -d /dev/video70 --get-fmt-video   # -> 1280x720 YUYV means frames flowing
```

Then verify end-to-end from a client: `aprilcam view BA` / `aprilcam view BB`
should open.

## Prevent recurrence

The single-kernel `linux-headers-<version>` package does **not** track future
kernels, so this recurs on every kernel upgrade. Install the **meta** package so
new kernels always pull matching headers and DKMS rebuilds automatically:

```bash
sudo apt-get install -y linux-headers-raspi    # meta pkg, tracks the raspi kernel line
```

(Confirmed missing on vidar during the July 2026 incident — the box had only
per-version headers for old kernels.) After a kernel upgrade, sanity-check with
`dkms status | grep v4l2loopback` before rebooting into the new kernel.

## Reference — the config that must line up

`/etc/modules-load.d/aprilcam.conf` autoloads the module:

```
v4l2loopback
```

`/etc/modprobe.d/aprilcam.conf` supplies its options (note `max_buffers=16` —
the fix from the earlier CSI bring-up; a smaller value stalled the CSI feed):

```
options v4l2loopback video_nr=70,71 \
  card_label=aprilcam-imx296-88000,aprilcam-imx296-80000 \
  exclusive_caps=1,1 max_buffers=16
```

`video_nr=70,71` → the loopback devices; `card_label` → the two IMX296 cameras
(`-88000` = CSI index 6 = client code `BA`; `-80000` = CSI index 7 = code `BB`).

## Related

- [`raspberry-pi-camera-setup.md`](raspberry-pi-camera-setup.md) — the full
  bridge architecture this failure mode breaks.
- Host/user note: `vidar.local` = SSH user `jtl` (has sudo + camera access);
  `vali.local` = `eric` (no sudo, no camera).
- A daemon restart (as the fix requires) is exactly the scenario sprint 016's
  MCP registry-restore handles — a persistent MCP server recovers on its own.
