---
date: 2026-10-31
tags: [linux, uvc, v4l2, usb, bandwidth, identity, deploy, buzzkill]
---
# Moving the daemon to a Linux host with USB (UVC) cameras: three traps

Hit 2026-10-31 moving the daemon from gala (macOS) to **buzzkill** (Ubuntu
26.04 x86, `deploy/aprilcamd.service`, wheel 0.20261031.x). The three
cameras that worked on gala (`arducam-ov9281-usb-camera`,
`hd-usb-camera`, `arducam-ov9782-usb-camera`) came up on Linux with wrong
names, unstable identities, and only one of them delivering frames.
vidar never showed any of this: its cameras were CSI modules behind
v4l2loopback, and its one UVC webcam ran alone.

## 1. V4L2 card names are not the camera's name

Recent uvcvideo kernels name a node `"<iProduct>: <entity>"`, truncated
to 31 characters: `"Arducam OV9782 USB Camera: Ardu"`,
`"HD USB CAMERA: HD USB CAMERA"`. `cv2-enumerate-cameras` reports that
string, so slugs came out as `arducam-ov9782-usb-camera-ardu` and matched
none of the config/calibration directories carried over from macOS
(AVFoundation reports the plain iProduct string).

**Fix (9e2e226):** `camera/identity.py` reads the USB device behind
`/sys/class/video4linux/videoN/device` (the link points at the UVC
*interface*; its parent is the USB device) and uses its `product` file
as the name. Check on a new host with:

```bash
for n in /sys/class/video4linux/video*; do
  u=$(readlink -f $n/device/..); echo "$(basename $n) $(cat $u/product) $(cat $u/serial)"
done
```

## 2. `/dev/videoN` is not an identity

Before the fix the Linux hardware key was `vidpid:<vid>:<pid>@dev-videoN`.
Both Arducams share VID:PID `0c45:6366`, and `/dev` numbering changes with
plug order and reboots, so registry numbers could shuffle. The same fix
reads `serial` from `/sys` (`UC762`, `UC852`; the HD camera reports a
generic `01.00.00`, which is still unique on this host) and falls back to
VID:PID plus the USB *port path* (`3-2.1.2`), not the node. A camera can
now move ports and keep its number, config, and calibration.

A registry written before such a key change never matches again. Stop
the daemon and replace `cameras/registry.json` (seed it with the new keys
and the old numbers) rather than letting it allocate new numbers.

## 3. USB 2.0 bandwidth: YUYV by default, and cheap cameras over-reserve

OpenCV's V4L2 backend opens a UVC camera in whatever format the driver
defaults to, which was uncompressed **YUYV**. A 720p YUYV stream reserves
most of a USB 2.0 link, so with three cameras on one hub only the first
opened. The kernel says so plainly (macOS never did, see
`2026-09-06-usb-hub-bandwidth-flapping-with-five-cameras-on-one-root-port.md`):

```
usb 3-2.1.2: Not enough bandwidth for new device state.
usb 3-2.1.2: Not enough bandwidth for altsetting 11
```

The daemon reports this only as `no frame available from camera ...`, so
check `dmesg` first.

**Fix (9e2e226):** `OpenCVBackend.open()` requests MJPG on V4L2 before the
frame size. That was **not enough**: even in MJPG, two Arducams at
1280x800 plus the HD camera did not fit on one 480 Mbit link, because UVC
cameras reserve an isochronous altsetting sized for their worst case, not
their actual JPEG rate. **The real fix is physical:** put each camera on its
own xHCI controller (`lsusb -t`; buzzkill has five). Going through a hub
chain into one root port does not work for this set at native resolution.

## Related: MJPG resets the size to 640x480

After switching to MJPG, every camera opened at 640x480. The flat-field
gain maps were then skipped ("shape (800, 1280, 3) does not match frame
shape (480, 640, 3)"), and the intrinsics (1280x800, 1920x1080) no longer
matched. Pin the native size in each camera's config `settings`
(`frame_width`/`frame_height`). Push it with
`aprilcam camera config <file> -d HOST` after a `--dump`.

## Other host notes

- On Ubuntu 26.04 / Python 3.14, the pipx install of `aprilcam[daemon]`
  pulled opencv-contrib 5.0.0, which imported without extra `libgl1`
  packages.
- gala's Chrome (its network-service helper) held UDP 5353, so gala's own
  daemon could not advertise and `aprilcam daemon list` failed with
  EADDRINUSE. The `EnableMediaRouter=false` Chrome policy turns off its
  Cast discovery; it applies after Chrome restarts.
- A dual-homed host advertises only its default-route address over mDNS
  (`daemon/mdns.py:_primary_routable_ipv4`). Robots on the other network
  must use the host name for that interface (`buzzkill-rg.home`).
