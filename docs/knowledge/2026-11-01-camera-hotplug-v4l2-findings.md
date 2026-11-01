---
date: 2026-11-01
tags: [camera, hotplug, v4l2, linux, macos, uvc, buzzkill, sprint-047]
---
# Camera hotplug: what the hardware actually does (and how to test it)

Findings from sprint 047, measured on buzzkill (Ubuntu, USB UVC cameras). The
design is in `src/aprilcam/camera/DESIGN.md` and `src/aprilcam/daemon/DESIGN.md`;
this is the evidence behind it.

## Linux / V4L2

- **A read fails fast after unplug** (it does not hang), but **`cv2.VideoCapture.isOpened()` stays true**
  on the dead handle. Health cannot come from `isOpened()`; the daemon watches
  presence separately (sysfs, 0.5 s) and drops the camera on that signal.
- **The `/dev/videoN` node changes on replug**. Never
  cache a path; reopen resolves the node again from the stable identity.
- **A reopen comes back at 640x480 with driver-default controls** unless the
  stored config is re-applied. `Camera.reopen()` now re-applies frame size
  first, then the gated controls, on every reopen. If a replugged camera is
  suddenly low-res, this is the first thing to check.
- **A second open of a node the daemon holds fails EBUSY.** A probe must never
  open a held camera with a second backend; held idle cameras are probed through
  their own handle. Concurrent *first* opens of the same camera also raced into
  EBUSY until the open was serialised (`_open_lock`).
- Presence comes from `/sys/class/video4linux` (no device open, no udev
  dependency); the unplug is visible there immediately.

## macOS

- AVFoundation applies device connect/disconnect only to the **main thread's**
  `NSRunLoop`. Polling from a worker thread returns the daemon-start snapshot
  forever, which is why cameras plugged in after start were invisible. The
  daemon's idle main thread now pumps its run loop and the presence poll runs
  on its own thread (`daemon.__main__._wait_for_shutdown`,
  `MacosAvfPresenceSource`). The macOS path has not had a hardware acceptance run like the Linux one.

## Running the acceptance script

`scripts/hotplug_acceptance.py` simulates unplug/replug of one real USB camera
by writing its USB id to `/sys/bus/usb/drivers/usb/{unbind,bind}`; it needs
root on the daemon host (Linux only, skips otherwise). It checks absent within
~2 s, the NO SIGNAL card and exit 3, real frames resuming in the same streams,
stored controls re-applied, probe not hitting EBUSY, and `camera close`.

```
uv run scripts/hotplug_acceptance.py --camera 2 --daemon buzzkill.home --ssh buzzkill
sudo python3 scripts/hotplug_acceptance.py --camera 2        # on the host itself
```

`--usb-id` is auto-detected; `--idle-close` also tests the idle reaper. The
camera is always re-bound in a `finally`. Result on buzzkill (2026-11-01):
absent in 0.39 s, real 1280x800 frames in the same stream 0.92 s after bind.
Do not run it against a camera someone is using.
