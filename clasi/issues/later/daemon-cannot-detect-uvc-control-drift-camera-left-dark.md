---
status: pending
---

# Daemon cannot detect UVC control drift — camera left dark while config says otherwise

## Description

Observed live 2026-08-22/23: camera 5 (`arducam-ov9281-usb-camera`)
produced near-black frames (mean luma 18/255) in both `camera view`
and `playfield view`, while tag detection kept working off the
markers' local contrast. The daemon's live config and the on-disk
config both said `{"auto_exposure": 1, "exposure": 300}` — but
`uvc-util -g exposure-time-abs` read back **3** (and gain 0) from the
hardware. The daemon believed its config was in force and never
re-asserted it.

Likely stomp source: the sprint-032 close ran the full hardware test
suite twice; `test_calibration_hardware.py` runs push configs at real
cameras and several aborted mid-run on the known AVFoundation
settle-race (`UnsupportedSetting: auto_exposure`), leaving hardware
state wherever the abort happened. The v1 daemon running concurrently
on the same host is another candidate writer. Either way: **UVC
controls are shared mutable hardware state with multiple writers, and
the daemon holds a cache it never revalidates.**

Operational remedy (documented in
`docs/knowledge/dark-camera-image-uvc-drift-reset.md`): compare
`uvc-util -g` readback against the config; `aprilcam camera config
--reset <camera>` re-applies the on-disk document live.

## Possible durable fixes (design question, pick at planning time)

- Periodic (or on-subscriber-connect) readback of the controls the
  config sets, re-asserting on mismatch — bounded, logged, at most
  every N seconds.
- Re-assert stored config whenever a stream subscription starts.
- Hardware-test hygiene: fixtures that snapshot and restore camera
  controls even on failure (does not defend against non-test writers).
- At minimum: surface drift in `camera list` / frame status (the
  existing `frame_dark` advisory fired, but nothing pointed at
  exposure drift as the cause).
