# Running AprilCam v2 on a Raspberry Pi 5 (CSI cameras, Ubuntu 24.04)

Hard-won setup and operational lessons for running the **v2** daemon on a Pi 5
with **CSI camera modules** — validated end to end on **vidar.local** (Pi 5,
Ubuntu 24.04 aarch64, two **IMX296** global-shutter modules) during sprint 040
(tickets 001-004, 006, 007). Ported from the v1 knowledge doc
(`archive/docs/knowledge/raspberry-pi-camera-setup.md`) and rewritten against
v2's actual deploy assets, module paths, and service user — read
`deploy/README.md` for the step-by-step runbook this doc assumes.

> TL;DR: a per-camera `libcamerasrc → v4l2loopback` **bridge** process
> (`deploy/aprilcam-camera-bridge`, one instance per camera under
> `aprilcam-bridge@<position>.service`) owns each CSI camera; the AprilCam
> **daemon reads the loopback devices via a `v4l2-ctl` subprocess**
> (`camera/backends/v4l2_loopback.py`). GStreamer/libcamera must never run
> inside the daemon process — this is an intentional, re-affirmed decision
> (sprint 040 Design Rationale), not something still under evaluation.

## Why the bridge is a separate, out-of-process thing

- **Ubuntu's stock libcamera has no Pi 5 PiSP support.** It ships only the
  VC4 IPA, not the PiSP IPA the Pi 5 needs, so `cam -l`/`libcamerasrc` find
  zero cameras out of the box even though the kernel detects the sensors
  (`dmesg` shows `imx296`). A from-source libcamera 0.5.x + libpisp build
  under `/usr/local` is a one-time prerequisite — see "One-time libcamera
  build" below.
- **CSI cameras are not directly V4L2-capturable.** The kernel exposes many
  ISP-pipeline `/dev/video*` nodes; none of them is a plain webcam node.
  Capture has to go through libcamera (`libcamerasrc` in GStreamer terms).
- **Running libcamera/GStreamer inside the daemon process is a proven
  crash/hang source** (v1 paid for this in segfault reports:
  `libcamerasrc`'s IPA-proxy fork interacting badly with gRPC's
  `pthread_atfork` handlers, plus a GLib-pipeline-vs-asyncio-event-loop
  hang). v2's architecture keeps this decoupling exactly as v1 proved it out
  — sprint 040's Design Rationale reaffirms it explicitly rather than
  re-litigating it: *"the daemon reads a v4l2loopback device; it does not
  link libcamera/GStreamer in-process."* The bridge and the daemon share
  nothing but a kernel device node (`/dev/video70`/`/dev/video71`).

```
[CSI cam i2c@88000] --libcamerasrc--> [aprilcam-bridge@0] --v4l2sink--> /dev/video70 ─┐
[CSI cam i2c@80000] --libcamerasrc--> [aprilcam-bridge@1] --v4l2sink--> /dev/video71 ─┤
                                                                                       ▼
                                    aprilcamd (v4l2-ctl subprocess read, no GStreamer/libcamera)
                                    → detection → gRPC (TCP 5280) + mDNS
```

**Confirmed live, sprint 040 ticket 003**: `V4l2LoopbackBackend` (the
`v4l2-ctl --stream-to=-` subprocess reader) produces correct, non-channel-
swapped 1280x720 BGR frames at ~30fps from a real bridge feed — this had
never been exercised against real hardware before this sprint.
`list_loopback_cameras()`'s `/sys/class/video4linux/video*/name` scan (not
the `cam` subprocess — the daemon's asyncio child-watcher would reap a `cam`
subprocess before it returns, an unchanged v1 lesson) also correctly found
both real card labels on the first try. **No source defect was found in
either module** — see `docs/design/design.md` §9 for the closed-gap entry.
`opencv-contrib-python`'s own V4L2 backend still cannot read a v4l2loopback
device reliably (`cv2.VideoCapture` reports a `select()` timeout on every
read) — this is why the daemon shells out to `v4l2-ctl` instead of using
`cv2.VideoCapture(dev, cv2.CAP_V4L2)` directly, unchanged from the reasoning
`camera/backends/v4l2_loopback.py`'s own module docstring records.

## The v4l2loopback module config, and why `max_buffers=16` matters

`/etc/modprobe.d/aprilcam.conf` (installed by `deploy/provision-pi.sh` from
`deploy/modprobe.d/aprilcam.conf`):

```
options v4l2loopback video_nr=70,71 \
  card_label=aprilcam-imx296-88000,aprilcam-imx296-80000 \
  exclusive_caps=1,1 max_buffers=16
```

`/etc/modules-load.d/aprilcam.conf` autoloads the module at boot (just the
bare word `v4l2loopback`). `video_nr=70,71` picks the loopback device
numbers; `card_label` is what `list_loopback_cameras()`'s `/sys` scan keys
off (the `aprilcam-` prefix is a configurable constant,
`APRILCAM_LIBCAM_LOOPBACK_LABEL`, default `aprilcam-`).

**`max_buffers` below ~16 starves the reader, not the writer.** With only 3
buffers, the `libcamerasrc → v4l2sink` writer happily pushes 15-30fps but the
streaming-mmap *reader* (the daemon's `v4l2-ctl` capture) starves to
0.2-0.6fps. Isolated stage by stage in v1's bring-up (each measured
independently): writer alone into `v4l2sink` ran 15fps fine; the reader at
`max_buffers=3` dropped to 0.6fps; the identical reader at `max_buffers=16`
recovered to 30fps. `vidar.local`'s config already carries `max_buffers=16`
and sprint 040 reconfirmed it live — do not "simplify" this value down on a
future Pi without re-measuring, it will silently reintroduce the stall.

To change it live without a reboot: stop the daemon and both bridge units,
`sudo rmmod v4l2loopback`, `sudo modprobe v4l2loopback` with the options
above, then restart the bridges and daemon.

## Install: use `deploy/`, not an ad hoc checkout

v2 ships real deploy assets (sprint 040 ticket 001) — `deploy/README.md` is
the authoritative runbook (wheel build → `provision-pi.sh` → pipx install →
module config → bridge units → daemon unit, in that order). Two v1-era facts
that no longer apply:

- **Service user is `aprilcam`, not `eric`/`jtl`.** A dedicated
  `useradd --system --no-create-home --shell /usr/sbin/nologin aprilcam`
  system user, in the `video` group, created idempotently by
  `provision-pi.sh` — never a human login account. This was a deliberate
  sprint 040 decision (see sprint.md Design Rationale): AprilCam must not
  become a hidden dependency of the Ansible-managed `ros` account, and must
  not perpetuate v1's `jtl`-owned checkout's identity.
- **No `APRILCAM_SYSTEM=1` or any other env-var gate is needed.** v2's
  `config/directories.py` `Directories` cascade decides whether a root is
  usable via `os.access`, not `euid == 0` — systemd's
  `ConfigurationDirectory=`/`StateDirectory=`/`RuntimeDirectory=` directives
  already provision `/etc/aprilcam`, `/var/lib/aprilcam`, `/run/aprilcam`
  owned by the `aprilcam` user before `ExecStart` runs, which is all
  `Directories` checks. Drop any memory of v1's `--force`/`--tcp`/`--unix`
  daemon flags too — v2's `python -m aprilcam.daemon` takes none.

Install path: `aprilcam[daemon]==<pinned version>` via `pipx` under the
`aprilcam` user (`sudo -u aprilcam -H pipx install ...`), from a wheel built
on the dev machine (`uv build --wheel`) and `scp`'d over — not a Pi-side
`git clone` + venv (that was v1's method; the Pi has no `uv`, and a bespoke
venv install would be a second, undocumented install method to maintain
alongside every other daemon host's documented `pipx install` path).

## ROS coexistence and switch-over

Vidar's CSI cameras were previously owned by an Ansible-managed ROS camera
stack: `ros-cameras.service` (the libcamera-owning node), plus
`camera-color-relay.service` and `web-video-server.service` (both
`PartOf=`/`BindsTo=ros-cameras.service`), plus `camera-watchdog.timer` (a
periodic health check that no-ops when `ros-cameras` is not active). A CSI
sensor can only be opened by one libcamera application at a time, so ROS and
AprilCam cannot both hold a camera concurrently — sprint 040 proved the
switch-over procedure live, in both directions, including a real defect in
the naive version of it.

**Switch from ROS to AprilCam:**

```bash
sudo systemctl disable --now ros-cameras.service camera-watchdog.timer \
    camera-color-relay.service web-video-server.service
sudo systemctl enable --now aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd
```

**Switch back from AprilCam to ROS:**

```bash
sudo systemctl disable --now aprilcamd aprilcam-bridge@0 aprilcam-bridge@1
sudo systemctl enable --now ros-cameras.service camera-watchdog.timer \
    camera-color-relay.service web-video-server.service
```

### The `BindsTo=` gotcha: disable all four ROS units, not two

Sprint 040's original plan disabled only `ros-cameras.service` and
`camera-watchdog.timer`, reasoning that `camera-color-relay.service` and
`web-video-server.service` would stay down passively via `BindsTo=`. That
reasoning is correct for *stopping* them but wrong for keeping them down
**across a reboot**: `BindsTo=` is a start-time dependency in both
directions, so at boot, systemd starting either of those two still-*enabled*
dependent units pulls `ros-cameras.service` back up even though
`ros-cameras.service` itself is individually disabled. Confirmed live
(ticket 004, Findings item 9): after a reboot with only two of the four
units disabled, `ros-cameras.service` came back `active`, its
`camera_node` promptly failed with `Pipeline handler in use by another
process` (racing AprilCam's already-running bridge for the same sensor), and
`ros-cameras` crash-looped. AprilCam's own units were unaffected throughout
— the race hurt ROS, not AprilCam — but the goal of "exactly one stack up
after reboot" failed. **The fix: disable all four units, not two.** A second
reboot with all four disabled came up clean, no race, confirmed twice.

### Ansible re-enable risk

`ros-cameras.service` and its bound units are Ansible-managed. **A future
Ansible run against vidar may re-enable and restart them**, since
`systemctl enable/disable` state is exactly the kind of drift an Ansible
playbook is likely to reconcile back to its declared state — this project's
switch-over only ever touches `systemctl enable`/`disable`/`start`/`stop`
state, never the underlying Ansible-managed unit files, so Ansible has no
reason to see anything wrong to "fix," but a playbook that unconditionally
re-asserts "these ROS units are enabled+active" would still stomp the
switch-over. **Recovery if this happens**: re-run the "Switch from ROS to
AprilCam" sequence above — it is idempotent and does not depend on ROS's
prior state. If Ansible keeps re-fighting the switch every run, the durable
fix is on the Ansible side (exclude vidar's camera units from that
playbook's scope, or make the desired camera-owner state itself an
Ansible-managed variable) — out of this project's ability to fix from
`deploy/`, and not attempted here.

## The camera bridge watchdog: `v4l2-ctl` exits 0 on a dead stream

`aprilcam-camera-bridge`'s job is noticing when its `libcamerasrc` pipeline
has died and restarting it (`Restart=always` on
`aprilcam-bridge@.service`). The obvious check — poll
`v4l2-ctl --stream-mmap --stream-count=1` and treat a non-zero exit as a
stall — **does not work**: against a v4l2loopback device with no live
producer, `v4l2-ctl --stream-mmap` prints an "unsupported stream type"-style
error to stdout but **exits 0**. Confirmed live and fixed in sprint 040
ticket 006. Worse, a single fix is not enough — two independent, genuinely
different failure modes exist and need two independent checks:

- **`frame_arrived`** — a bounded capture piped through a byte count
  (`timeout 6 v4l2-ctl -d "$DEV" --stream-mmap --stream-count=1
  --stream-to=- 2>/dev/null | wc -c`; zero bytes means stalled, regardless
  of exit code). This correctly detects a producer that **never started
  streaming at all** (e.g. `libcamerasrc` crashing before ever negotiating a
  format).
- **`gst_crashed`** — grep gst-launch's own captured stdout/stderr for its
  fault handler's `Caught SIGSEGV` line. This is the *only* reliable signal
  for the specific "crashed mid-stream, GStreamer's fault handler spins
  forever instead of exiting" failure mode: live testing found the loopback
  device keeps appearing to deliver **fresh, live-paced, changing frame
  content for minutes after the SIGSEGV** (most likely explanation: the
  `queue`/`videoconvert`/`v4l2sink` elements and/or already-in-flight
  libcamera/ISP completions run in threads independent of the one that hit
  the crashing assertion), so `frame_arrived` alone never fires for this
  case — confirmed by a 2.5-minute live observation where every poll
  returned different, correctly-sized frame data from a pipeline that had
  already SIGSEGV'd.

Both checks are shipped together in `deploy/aprilcam-camera-bridge`; each
covers the failure the other misses. If you ever see the old, single-signal
log line (`stalled (libcamerasrc likely crashed)` judged by exit status
alone) on a host, it is running a stale copy of the script.

## Telling a dead sensor from a software bug

Camera i2c@88000 (`/dev/video70`) on vidar has a **real physical fault**,
confirmed live (ticket 003, reproduced four times): `libcamerasrc`
consistently SIGSEGVs 1-20s after start, always preceded by `Camera frontend
has timed out! Please check that your camera sensor connector is attached
securely.` This reproduces even bypassing GStreamer/the bridge/AprilCam
entirely:

```bash
cam -c1 --capture=3
```

If this crashes/times out the same way directly against the `cam` tool, the
fault is in the camera/cabling, not in AprilCam's bridge or capture code —
worth confirming with this exact command before spending time debugging the
bridge script or the daemon. The sibling camera (i2c@80000, `/dev/video71`)
worked reliably throughout the same session, including with the faulty
camera's bridge unit crash-looping next to it (`aprilcam-bridge@1`'s
`NRestarts` stayed at 0 the whole time) — a hardware fault on one camera is
expected to have zero effect on the other; if it does affect the other,
that *would* be a real defect worth investigating.

**`camera list` is a cached/last-probe view; `camera probe` forces a live
read.** Both report `usable` immediately after a fresh install even for a
camera whose bridge has already died — this is intentional, not a bug
(`list` is meant to be a cheap, non-blocking view). Use
`aprilcam camera probe -d <host>` when you need to know what's true *right
now*.

## The sshd wedge after a `libcamerasrc` SIGSEGV

Observed once, sprint 040 ticket 006: after repeated deliberate
`libcamerasrc` crash tests, `vidar.local`'s `sshd` began accepting TCP
connections and completing SSH key exchange, then hanging forever at
`we sent a publickey packet, wait for reply` — indistinguishable from the
outside from a merely slow host. `ping`/`nc -z` to the same host succeeded
throughout, ruling out a network problem; this is specific to `sshd`'s own
responsiveness on vidar. Recovery required a **physical power cycle** — a
`systemctl restart sshd` was never reachable to even try. The causal link to
the crash testing (vs. cumulative resource exhaustion from a long session of
repeated crash tests) is **not established** — flagged here as an open
risk, not a solved problem. Practical takeaway: keep deliberate crash/SIGSEGV
testing against real CSI hardware minimal and spaced out, and know that a
wedged `sshd` after such testing may need a physical power cycle, not
further SSH retries.

## `.local` hostname resolution: IPv6 link-local vs IPv4

`aprilcam camera list -d vidar.local` (or any `-d <host>.local` command) can
time out for 5s and fail even though the daemon is healthy and reachable.
Root cause (ticket 007): a multi-homed resolver (macOS's, via mDNS/Bonjour)
can list a host's IPv6 **link-local** addresses ahead of its IPv4 address
for a `.local` name, and a raw hostname handed to `grpc.insecure_channel`
lets gRPC's own C-core resolver pick the unreachable link-local candidate
and wait out the full connect timeout instead of falling back. **Fixed
client-side only**: `client/discovery.py` now resolves the hostname itself
via `socket.getaddrinfo` and prefers an `AF_INET` candidate before handing
gRPC a target, with graceful fallback (an IP literal, an unresolvable
hostname, or an IPv6-only host all behave exactly as before). The **daemon's
bind is deliberately unchanged** — `daemon/rpc.py`'s `build_server` binds
`0.0.0.0:5280` (IPv4 only), an explicit, still-current sprint 028 decision;
this fix does not reopen it. `aprilcam daemon list` (mDNS service-browse,
no `-d`) was never affected — it already reports the browsed literal
address, never a hostname.

## `libGL.so.1`/`libX11.so.6`: a hard dependency on minimal images

`opencv-contrib-python`'s compiled extension (`cv2.abi3.so`) has a **hard,
non-lazy ELF dependency** on `libGL.so.1`, `libX11.so.6`, and
`libglib-2.0.so.0` — confirmed via `ldd` (sprint 040 ticket 002). This
resolved cleanly on vidar because it happens to already carry those
libraries as automatic dependencies of desktop/GNOME and ROS packages
(`pemmican-desktop`, `gnome-remote-desktop`, `ros-kilted-*`) — **vidar is a
Raspberry Pi desktop image, not a minimal Ubuntu Server install.** A
genuinely minimal headless Pi has not been tested and might fail
`import cv2` with `ImportError: libGL.so.1: cannot open shared object file`.
If that happens, the fix is `apt-get install libgl1 libx11-6` — this is
**not** pre-emptively added to `provision-pi.sh` (nothing has actually
failed to justify it yet); add it only if a real install hits this.

**aarch64/cp312 wheel resolution — confirmed fine, no piwheels needed.**
Every compiled `daemon`-extra dependency (`opencv-contrib-python`, `grpcio`,
`numpy`, `grpcio-reflection`, `msgpack`, `mss`, `websockets`, `fpdf2`,
`cv2-enumerate-cameras`) resolved a real manylinux `cp312`/`aarch64` wheel
straight from PyPI on Ubuntu 24.04.4 aarch64 / Python 3.12.3 — no sdist
build, no `--extra-index-url https://www.piwheels.org/simple` fallback
needed. Revisit only if a future dependency bump changes this.

## One-time libcamera build (still needed on a genuinely fresh Pi)

Ubuntu ships no PiSP-capable libcamera package. A fresh Pi needs a
from-source libcamera 0.5.x + libpisp build with the GStreamer plugin,
installed to `/usr/local`, before any of the above works — this is a
one-time prerequisite `deploy/provision-pi.sh` does **not** attempt itself
(it only apt-installs the stock GStreamer libcamera plugin as a fallback,
and only when no working `cam -l` is already found — installing the stock
package over a working from-source build would be pointless and v1's own
setup explicitly avoided it). `vidar.local` already had this build in place
under `/opt/src/{libcamera,libpisp}` before sprint 040 started. If a future
Pi needs this from scratch:

```bash
sudo apt-get install -y git meson ninja-build pkg-config \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  python3-yaml python3-ply python3-jinja2 libyaml-dev libssl-dev libdw-dev libudev-dev

git clone https://github.com/raspberrypi/libpisp.git /opt/src/libpisp
meson setup /opt/src/libpisp/build /opt/src/libpisp --prefix=/usr/local
sudo ninja -C /opt/src/libpisp/build install

git clone https://github.com/raspberrypi/libcamera.git /opt/src/libcamera
meson setup /opt/src/libcamera/build /opt/src/libcamera --prefix=/usr/local \
  -Dpipelines=rpi/vc4,rpi/pisp -Dipas=rpi/vc4,rpi/pisp \
  -Dgstreamer=enabled -Dcam=enabled -Dv4l2=enabled
sudo ninja -C /opt/src/libcamera/build install
sudo ldconfig

sudo apt-get remove -y gstreamer1.0-libcamera   # stale distro 0.2.0 plugin
sudo ln -sf /usr/local/lib/gstreamer-1.0/libgstlibcamera.so \
            /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlibcamera.so
```

Verify: `cam -l` lists the cameras, and a plain
`gst-launch-1.0 libcamerasrc ! video/x-raw,format=NV12,width=1280,height=720 !
videoconvert ! jpegenc ! multifilesink location=/tmp/t.jpg max-files=1`
writes a JPEG.

## Verify

```bash
# from the Pi:
systemctl is-active aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd
v4l2-ctl -d /dev/video70 --get-fmt-video      # 1280x720 YUYV = frames flowing
journalctl -u aprilcamd -n 50 --no-pager      # Directories resolution, no traceback

# from another machine on the LAN:
aprilcam daemon list                          # vidar's daemon discoverable via mDNS
aprilcam camera list -d vidar.local           # both CSI cameras, live probe: `camera probe`
aprilcam camera image -d vidar.local <slug> -o /tmp/check.jpg
```

## USB/UVC cameras coexist with CSI (libcamera loopback) cameras

A Pi can have a USB/UVC camera and one or more CSI cameras (bridged via
`v4l2loopback`, see above) attached and registered **at the same time** —
this is the expected "going forward" setup per the stakeholder, not an edge
case (sprint 042). Each resolves through its own chain and gets its own
`hardware_key`:

- **CSI cameras** resolve through the libcamera path: `_probe_libcamera()`
  reads `/sys/class/video4linux/video*/name` for the labelled loopback
  device and stamps `hardware_key=f"libcamera:{camera_id}"` (e.g.
  `libcamera:aprilcam-imx296-88000`) — never a fallback tier.
- **USB/UVC cameras** resolve through the normal cv2-enumeration chain
  (vid:pid+location, then location-only, then name+resolution), unchanged
  by this sprint.

**Symptom (pre sprint 042): a Pi with any working CSI camera silently
ignored every USB camera.** `aprilcam camera probe -d <host>` (or `camera
list`) would show only the CSI camera(s), with a plugged-in, working
UVC camera (confirmed present via `lsusb`/`v4l2-ctl` on the Pi itself)
never appearing at all — not as unusable, just absent. **Cause**:
`camera/identity.py`'s `resolve_all()` was an either/or gate — as soon as
`_probe_libcamera()` found any loopback device, it returned only the
libcamera-resolved identities and never looked at the cv2-enumerated
table at all. **Fix (sprint 042 ticket 001)**: both enumeration sources
share one `/dev/videoN` device-index domain on Linux, so `resolve_all()`
now merges them (a union of both sources' keys) instead of choosing one
over the other; a loopback device that happens to also show up in the
cv2 table still resolves exactly once, under its `libcamera:` identity.
See `src/aprilcam/camera/DESIGN.md` §4 "Identity resolution algorithm"
and §6's sprint 042 entry for the full mechanism.

**How to check**: `aprilcam camera probe -d <host>` forces a live
re-enumeration (unlike `camera list`'s cached view — see "Telling a dead
sensor from a software bug" above) and should list every attached CSI
*and* USB camera, each exactly once. Live numbers/slugs for a specific
mixed-hardware host (e.g. vidar's CSI cameras plus a USB camera) are
recorded by sprint 042 ticket 003's live-verification pass, not
duplicated here.

## Related

- [`v4l2loopback-vanishes-after-kernel-upgrade.md`](v4l2loopback-vanishes-after-kernel-upgrade.md)
  — the DKMS/kernel-header failure mode that makes the loopback devices
  disappear after an unrelated kernel upgrade; the underlying module-build
  problem is unrelated to which daemon version reads the devices, so it
  applies to v2 exactly as it did to v1.
- `deploy/README.md` — the authoritative, step-by-step install runbook this
  doc assumes throughout.
- `docs/design/design.md` §9 and `src/aprilcam/camera/DESIGN.md` §6 — the
  design-doc record of what sprint 040 verified/closed on the capture-path
  side.
