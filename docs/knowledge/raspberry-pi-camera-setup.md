# Running AprilCam on a Raspberry Pi 5 (CSI cameras, Ubuntu 24.04)

Hard-won setup for bringing AprilCam up on a Pi 5 with **CSI camera modules**
(validated on **vidar.local**: Pi 5, Ubuntu 24.04, two **IMX296** global-shutter
modules). Captures the working architecture and every gotcha so we don't
rediscover them. To be formalized as Ansible later; for now this + the companion
script (`scripts/setup-vidar-cameras.sh`) is the reproducible path.

> TL;DR: a per-camera `libcamerasrc → v4l2loopback` **bridge** owns each CSI
> camera; the AprilCam **daemon reads the loopback devices via plain V4L2**.
> GStreamer/libcamera must NOT run inside the daemon. Use **pip
> `opencv-contrib-python`** in the daemon venv, not system cv2.

## Why it's not plug-and-play

- **Ubuntu's stock libcamera (0.2.0) has no Pi 5 support.** It ships only the
  VC4 IPA (`ipa_rpi_vc4.so`), not the **PiSP** IPA (`ipa_rpi_pisp.so`) the Pi 5
  needs. The kernel detects the sensors (`dmesg` shows `imx296`), but
  `cam`/`libcamerasrc` find **zero** cameras. (No `rpicam-apps`/`picamera2`
  packages on Ubuntu either.)
- **CSI cameras are not V4L2-capturable** the OpenCV way. The kernel exposes
  ~35 ISP-pipeline `/dev/video*` nodes; none is a plain webcam. Capture must go
  through **libcamera** (GStreamer `libcamerasrc`).

## The architecture (and why)

```
[CSI cam 0] --libcamerasrc--> [bridge proc] --v4l2sink--> /dev/video70 ─┐
[CSI cam 1] --libcamerasrc--> [bridge proc] --v4l2sink--> /dev/video71 ─┤
                                                                        ▼
                                          AprilCam daemon (plain V4L2, CAP_V4L2)
                                          → detection → gRPC + TCP streams
```

Running GStreamer+libcamera **inside the gRPC daemon segfaults / hangs** — three
distinct failures, all real, all fixed by *keeping gst+libcamera out of the
daemon process*:

1. **libcamera forks its IPA proxy** while gRPC's `pthread_atfork` handlers are
   active → **segfault**. (`GRPC_ENABLE_FORK_SUPPORT=0` stops this one.)
2. With fork support off, opening `libcamerasrc` via OpenCV inside the daemon
   **hangs** (GLib pipeline vs the daemon's asyncio/gRPC event loop).
3. The **pip `grpcio` wheel ↔ system OpenCV 4.6 ABI clash** (both pull
   libstdc++/abseil) → **segfault in `cv2.aruco`** (`DetectorParameters`
   property set). Fixed by using **pip `opencv-contrib-python`** in the venv
   instead of the distro cv2.

The bridge sidesteps #1/#2 (no libcamera/gst in the daemon); pip-opencv fixes #3.

## Gotchas (each cost real time)

- **Daemon must read with `CAP_V4L2`, not `CAP_ANY`.** With `CAP_ANY` OpenCV
  picks its GStreamer backend for `/dev/video*` — re-introducing gst in the
  daemon → core-dump. `cv2.VideoCapture(dev, cv2.CAP_V4L2)` reads via ioctls.
- **Enumerate loopback devices via `/sys`, not the `cam` subprocess.** Running
  `cam -l` from inside the daemon returns an empty list (the asyncio
  child-watcher reaps the subprocess early). AprilCam's libcamera backend in
  `loopback` mode scans `/sys/class/video4linux/*/name` for the `aprilcam-`
  label prefix instead.
- **Loopback device permissions + system (not user) service.** The daemon needs
  the `video` group to open `/dev/video7x`. A systemd **user** service can't add
  supplementary groups, so the daemon and bridges run as **system** services
  with `SupplementaryGroups=video`. A udev rule keeps the loopback nodes
  `video`-group rw.
- **`aprilcam[daemon]` pulls `opencv-contrib-python`** — keep it; do **not**
  rely on system cv2 in the venv (ABI clash #3). The daemon needs only V4L2 +
  aruco from OpenCV (no GStreamer — that lives in the bridge process), so the
  pip wheel is sufficient.
- **OpenCV 4.6 (Ubuntu) uses the old aruco API.** AprilCam's
  `vision/aruco_compat.make_aruco_detector` already handles 4.6 ↔ 4.7+; pip
  opencv (4.13) uses the new API. Either works.
- **Socket-dir agreement.** Client and daemon must resolve the same
  `APRILCAM_SOCKET_DIR`; on this Pi it's `/run/user/<uid>/aprilcam`.
- **Remote clients use TCP, not the daemon's unix stream sockets** (those paths
  are on the Pi). The stream consumers prefer TCP when the daemon host is not
  local.

## One-time system build: libcamera 0.5.x with PiSP → /usr/local

Ubuntu has no package, so build the **Raspberry Pi fork** of libcamera (+
libpisp) with the GStreamer plugin and install to `/usr/local`:

```bash
sudo apt-get install -y git meson ninja-build pkg-config \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  python3-yaml python3-ply python3-jinja2 libyaml-dev libssl-dev libdw-dev libudev-dev

# libpisp (PiSP support library)
git clone https://github.com/raspberrypi/libpisp.git /opt/src/libpisp
meson setup /opt/src/libpisp/build /opt/src/libpisp --prefix=/usr/local
sudo ninja -C /opt/src/libpisp/build install

# libcamera (rpi fork) with the rpi/pisp pipeline + IPA + gstreamer plugin
git clone https://github.com/raspberrypi/libcamera.git /opt/src/libcamera
meson setup /opt/src/libcamera/build /opt/src/libcamera --prefix=/usr/local \
  -Dpipelines=rpi/vc4,rpi/pisp -Dipas=rpi/vc4,rpi/pisp \
  -Dgstreamer=enabled -Dcam=enabled -Dv4l2=enabled
sudo ninja -C /opt/src/libcamera/build install
sudo ldconfig

# Make the new 0.5.x gst plugin win over the distro 0.2.0 one
sudo apt-get remove -y gstreamer1.0-libcamera   # the stale 0.2.0 plugin
sudo ln -sf /usr/local/lib/gstreamer-1.0/libgstlibcamera.so \
            /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstlibcamera.so
```

Verify: `cam -l` lists the cameras, and
`gst-launch-1.0 libcamerasrc ! video/x-raw,format=NV12,width=1280,height=720 ! videoconvert ! jpegenc ! multifilesink location=/tmp/t.jpg max-files=1`
writes a JPEG.

> On vidar this was already built under `/opt/src/{libcamera,libpisp}`; I just
> reconfigured the prefix to `/usr/local`, enabled the gstreamer plugin, and
> installed.

## The rest: `scripts/setup-vidar-cameras.sh`

The companion script does the reproducible remainder (idempotent): v4l2loopback
(dkms + kernel headers + persistent module options + udev perms), the aprilcam
venv (with pip opencv), the bridge launcher + `aprilcam-bridge@.service`
template, and the `aprilcamd.service`, then enables and starts everything.

```bash
# on the Pi, with the repo cloned to ~/aprilcam:
bash ~/aprilcam/scripts/setup-vidar-cameras.sh
```

Daemon env (set in `aprilcamd.service`): `APRILCAM_CAMERA_BACKEND=libcamera`,
`GRPC_ENABLE_FORK_SUPPORT=0`, and `PATH` including `/usr/local/bin` (for `cam`).

## Verify

```bash
aprilcam cameras                 # -> the two CSI (imx296-88000, imx296-80000)
                                 #    plus any USB/UVC camera (e.g. C920)
# from another machine on the LAN:
APRILCAM_DAEMON_HOST=vidar.local aprilcam cameras
```

`systemctl status aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd` — all active.

## USB/UVC cameras + remote live view

vidar also has a **USB webcam** (Logitech C920). USB cameras are the *reliable*
live-view path — plain `uvcvideo` V4L2 devices OpenCV opens directly
(`cv2.VideoCapture(index)`), with **no libcamera/bridge** in the way.

- **Mixed backend.** With `APRILCAM_CAMERA_BACKEND=libcamera` the daemon
  enumerates the CSI cameras (libcamera loopback) **and** real USB/UVC cameras
  side by side. `camutil._list_v4l2_usb_cameras()` scans
  `/sys/class/video4linux/video*/device/driver` for `uvcvideo` (one capture node
  per device); `CameraPipeline` routes a libcamera index through the loopback and
  any other index straight to `cv2.VideoCapture(index)`. The C920 shows up as
  e.g. `[16] HD Pro Webcam C920` (its `/dev/video16` index).

- **Remote live view from another machine:**

  ```bash
  aprilcam view 16 --host vidar.local      # 16 = the number from `aprilcam cameras`
  ```

  `view` resolves the typed number against the **daemon's** enumeration (not a
  local registry — meaningless for a remote daemon), opens the camera
  daemon-side, and streams JPEG frames over TCP to a tkinter window on the
  client. Verified end-to-end Mac ⇄ vidar.

- **Tcl/Tk on uv-managed Python (client side).** Standalone CPython builds (uv)
  ship Tcl/Tk but record a build-time path that doesn't exist, so `tk.Tk()` dies
  with *"Can't find a usable init.tcl"*. `view` now auto-points `TCL_LIBRARY`/
  `TK_LIBRARY` at `<prefix>/lib/tcl8.x` before creating the window.

## ⚠️ The big v4l2loopback gotcha: `max_buffers` starves the reader

**Symptom:** the CSI live view stalls at **~0.2–0.6 fps** even though
`libcamerasrc` is healthy. **Cause:** `v4l2loopback` was loaded with
**`max_buffers=3`**. With only 3 buffers, the streaming-mmap **reader** (the
daemon's `v4l2-ctl` capture) starves to a crawl while the `libcamerasrc →
v4l2sink` **writer** happily pushes 15–30 fps. **Fix:** load v4l2loopback with
**`max_buffers=16`** (in `/etc/modprobe.d/aprilcam.conf`; the setup script now
does this). After the fix the loopback reads a full 30 fps and the CSI cameras
stream live (the daemon then caps at `detection_fps`, ~4–5 fps actual on the Pi
5 doing AprilTag/ArUco detection at 1280×720 per camera).

How it was isolated (each stage measured independently):

| Stage | Rate |
|---|---|
| `libcamerasrc → NV12 1280×720 → fakesink` | 15 fps (30 fps native) — fine |
| `+ videoconvert → YUY2 → fakesink` | 15 fps — fine |
| writer `→ v4l2sink /dev/video70` (no reader) | 15 fps — fine |
| **reader `v4l2-ctl --stream-mmap /dev/video70`** (max_buffers=3) | **0.6 fps — culprit** |
| same reader, **max_buffers=16** | **30 fps — fixed** |

To change it live (without a reboot): stop the daemon + both bridges,
`sudo rmmod v4l2loopback`, `sudo modprobe v4l2loopback video_nr=70,71
card_label=aprilcam-imx296-88000,aprilcam-imx296-80000 exclusive_caps=1,1
max_buffers=16`, then restart the bridges + daemon.

> The USB C920 path never hit this — it's a real UVC device read directly with
> OpenCV, not through the loopback.

## Replacing the old ROS camera stack

Vidar previously served the cameras via ROS (`ros-cameras.service`,
`camera_ros`, `web_video_server`). AprilCam and ROS can't both own the cameras;
the ROS stack was disabled (`systemctl disable --now ros-cameras.service
camera-color-relay.service web-video-server.service`).
