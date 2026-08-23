---
status: pending
---

# `camera probe` does not detect hot-plugged cameras, despite being the command for exactly that

## Description

Plug in a new USB camera while the daemon is running and it is invisible
to AprilCam. `aprilcam camera probe` — documented as **"force a deep
camera re-probe"** — does not find it. The only way to pick it up is to
restart the daemon.

Observed 2026-08-21, connecting an Arducam OV9281 to a machine already
running the daemon:

```
$ aprilcam camera probe          # after connecting the OV9281
  [1] brio-501 ... [4] usb2-video          # 4 cameras, no OV9281

$ aprilcam daemon stop && aprilcam daemon start
daemon started (5 camera(s))              # OV9281 appears
```

`probe` is precisely the command an operator reaches for here. Its name,
its help text, and the absence of any other candidate all point at it.
That it silently returns a stale list is worse than not having the
command: the operator concludes the camera is broken or unsupported and
starts debugging hardware.

## The enumeration itself is fine — the daemon is the problem

Verified directly, with the OV9281 attached and the daemon running:

- `cv2_enumerate_cameras.enumerate_cameras()` returns **5** cameras,
  including `Arducam OV9281 USB Camera` at index 1201, path
  `0x21141110c456366`.
- `aprilcam.camera.identity.resolve_all()` returns **5**, correctly
  assigning the distinct slug `arducam-ov9281-usb-camera`.
- The daemon reports **4** until restarted.

So neither the enumeration library nor v2's identity resolution is at
fault. Both handled a genuinely awkward case correctly, worth recording:
**the two Arducams report identical USB vendor/product ids**
(`VendorID_3141 ProductID_25446`), differing only in the
AVFoundation unique id that encodes USB location
(`0x21141400c456366` vs `0x21141110c456366`). Identity did not collide
them.

The daemon enumerates once at startup and holds that list. `probe`
re-probes the cameras already in its registry rather than re-running
discovery.

## Proposed fix

- `probe_cameras()` re-runs device discovery, not merely a re-probe of
  known registry entries: a camera present on the bus and absent from
  the registry is added; a registry entry no longer on the bus is marked
  absent rather than dropped (the registry deliberately retains records
  for cameras that come and go — see `CameraRegistry.resolve()`).
- Decide and document whether the daemon should also detect hot-plug
  events on its own, or whether an explicit `probe` remains the only
  refresh point. Explicit-only is defensible for a daemon whose stated
  design is that an idle daemon does near-zero work — but if that is the
  answer, `probe` must actually do it, and `daemon status`/`camera list`
  should not imply the list is live.
- Test: with the registry pre-populated with N cameras and discovery
  reporting N+1, `probe` surfaces the new one without a restart.

## Related

- `camera-probe-reports-everything-uncalibrated-and-renders-skipped-cameras-as-data.md`
  — the other probe defect found in the same bring-up. Both make `probe`
  actively misleading rather than merely incomplete, and both are worth
  fixing together: `probe` is documented as the deep, authoritative
  check and is currently the least trustworthy of the camera commands.
- `sprint-020-followups-socket-collision-and-cli-polish.md` — records
  that `CameraRegistry.resolve()` deliberately never renames a camera's
  slug on reconnect; relevant background for how registry entries and
  live devices relate.
