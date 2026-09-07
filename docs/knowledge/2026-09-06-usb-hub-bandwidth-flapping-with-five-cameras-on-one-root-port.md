---
date: 2026-09-06
tags: [camera, usb, hub, bandwidth, hotplug, macos, worker-backend, identity, sprint-039]
related-tickets: [039-003]
---

# Five cameras sharing one USB 2.0 root port: bandwidth starvation looks like a hardware fault, not a bus limit

Hit 2026-09-06 while bringing up two new 1080p cameras alongside the
three already in daily use (`arducam-ov9782-usb-camera`,
`arducam-ov9281-usb-camera`, `hd-usb-camera`) — all five landed on the
same physical USB hub, sharing one root port back to the host.

## Symptom

With all five cameras open, the daemon logged repeated ~15-second
`heartbeat stalled` warnings (`Camera.check_stall`,
`DEFAULT_STALL_THRESHOLD_S` in `camera/camera.py` is a 10s default —
these stalls ran noticeably longer than that threshold before the
watchdog even got a chance to react) against first one 1080p camera,
then the other, then occasionally one of the three original cameras. A
triggered reopen sometimes failed outright, and `system_profiler
SPUSBDataType`/Console showed the affected device actually leaving and
rejoining the USB bus — a real disconnect/reconnect cycle, not a stuck
read. Between one enumeration and the next, `cv2_enumerate_cameras`
sometimes reported the surviving cameras at *different* indices than
before the flap, because macOS renumbers what's left when a device
drops off a shared port.

## Cause

USB 2.0's ~35 MB/s usable bandwidth on a single root port is shared by
every device hanging off it. Two 1080p cameras plus three already-open
lower-resolution cameras exceeded what the hub's upstream port could
sustain — the OS's own USB stack resolves the overcommit by dropping
and re-enumerating whichever device is least able to keep its isochronous
stream serviced, not by throttling gracefully. This is a bus capacity
limit, not a failing camera, driver bug, or daemon defect — the same
five cameras split across two separate root ports (or run at a lower
resolution each) do not exhibit it.

The index renumbering this produces is exactly the class of incident
`2026-09-06-cameras-plugged-in-after-daemon-start-need-a-restart.md`
also documents symptoms of (macOS enumeration state changing under a
long-running daemon) — that entry is about *new* devices never being
seen without a restart; this one is about *existing* devices briefly
leaving and coming back with the survivors shifted to new slots. Read
both if you're debugging a camera list that doesn't match what's
physically plugged in.

## Mitigation

- **Separate physical port.** Splitting the five cameras across two
  independent root ports (not two ports on the same hub — a hub's
  downstream ports still share its single upstream link) removed the
  overcommit entirely. This is the fix that actually addresses the root
  cause; the others below just make individual cameras cheaper to run
  when a spare port isn't available.
- **Lower resolution.** Setting `frame_width`/`frame_height` lower on
  the 1080p cameras (`aprilcam camera config <camera>` — see
  `camera/camera.py`'s `_RESOLUTION_KEYS`, always routed through the
  primary capture backend) cuts each camera's isochronous bandwidth
  demand proportionally, buying headroom on a shared port without
  rewiring anything physically.
- Reducing frame rate was not tried here (resolution was the more direct
  lever for these two cameras) but should have a similar effect for the
  same reason — the constraint is sustained bytes/sec, not frame count.

## How this sprint's identity-safe reopen (039-003) changes the failure mode

Before 039-003, a reopen after one of these bus flaps could silently
open whatever camera now sat at the previously-resolved OpenCV/worker
index — the exact "wrong camera's frames under the old slug" bug that
ticket fixed. With the fix in place, a bus flap now surfaces as one of
two well-defined outcomes instead of a silent substitution:

- If the hardware key is not currently enumerated when
  `index_resolver()` is called (the device is mid-flap, not yet back on
  the bus), `OpenCVBackend.open()`/`WorkerBackend.open()` raise
  `CameraNotEnumeratedError` before opening anything. `Camera`'s
  self-heal treats this as budget-neutral (the `MAX_REOPENS` counter is
  not spent) and sets `present=False` — the watchdog keeps retrying
  indefinitely rather than giving up after 10 attempts, which matters
  because a bus flap this project has observed can take longer than 10
  quick reopen attempts to resolve on its own.
- If the hardware key *is* enumerated again by the time of the retry but
  at a different index than before (the renumbering this incident
  describes), the fresh `index_resolver()` call picks up the new index
  correctly — and, on macOS, the post-open AVFoundation `uniqueID` check
  would refuse the open outright if the resolver and the actual device
  ever disagreed, rather than completing silently against the wrong
  physical camera.

Net effect: a USB bandwidth flap during this kind of five-camera,
one-port setup now reads as "camera briefly reports not present, then
recovers on its own once the bus settles" — which is diagnosable and
expected — instead of "camera silently starts reporting a different
physical camera's frames under the old name," which is not.

## See also

- `2026-09-06-cameras-plugged-in-after-daemon-start-need-a-restart.md` —
  the companion macOS enumeration-staleness incident from the same day.
- `clasi/sprints/039-.../tickets/done/003-identity-safe-camera-open-reopen-re-resolve-index-and-verify-identity-every-time.md`
  for the full identity-safe reopen design and test coverage.
