---
id: 009
title: Linux hardware acceptance script (unbind/bind)
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-004
depends-on:
- '006'
- '007'
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Linux hardware acceptance script (unbind/bind)

## Description

Hard gate on Linux (buzzkill or donald). Script under `scripts/` (and a
pytest `hardware` marker wrapper) that simulates plug/unplug with
`echo <usb-id> > /sys/bus/usb/drivers/usb/unbind` / `bind` (needs root;
usage documented) and, while a viewer/stream runs, asserts: camera absent
in `camera list` within ~2 s; test card served with placeholder flag;
real frames at the configured size within ~3 s of bind; stored config
(exposure, frame size) applied; daemon not restarted; idle close and
`camera close` reopen path also exercised. Run it on hardware and record
the result in the ticket.

## Plan

- Files: `scripts/hotplug_acceptance.py`, marker registration in pytest
  config, short usage note.

## Acceptance Criteria

- [x] Script exits 0 on buzzkill (or donald) with all assertions above passing; output pasted into the ticket.
- [x] Skipped automatically when no hardware / not root / not Linux.
- [x] Default test suite does not run it.

## Testing

- **Existing tests to run**: `uv run pytest -q -m "not hardware" tests/daemon`
- **New tests to write**: the hardware acceptance script itself (run manually on buzzkill).
- **Verification command**: `ssh buzzkill` then run `scripts/hotplug_acceptance.py --camera <N> --usb-id <id>`

## Hardware run (buzzkill, camera 2, usb 3-2.1.4, wheel aprilcam-0.20261031.4)

Command: `uv run python scripts/hotplug_acceptance.py --camera 2 --daemon buzzkill.home --ssh buzzkill [--idle-close]`
(pytest wrapper: `APRILCAM_HOTPLUG_ACCEPTANCE=1 APRILCAM_HOTPLUG_ARGS='...' pytest -m hardware`).
Bug found and fixed: concurrent first image+tag stream subscribe on a cold
daemon raced two device opens (tag stream died: "failed to open camera at
index 0"); `_camera_for` now serialised (`_open_lock`) + unit test.

```
[   0.61s] PASS  config pins a frame size (1280x800)
[   0.61s] config settings: {'auto_exposure': 1, 'exposure': 12, 'frame_height': 800, 'frame_width': 1280}
[   0.94s] camera 2 (arducam-ov9281-usb-camera) usb-id=3-2.1.4 node=/dev/video0
[   2.66s] PASS  baseline: real frames flowing before unplug
[   3.37s] baseline v4l2: 1280x800 {'auto_exposure': 1, 'exposure_time_absolute': 12}
[   4.86s] PASS  probe while held (subscribed) reports present+usable: '[2]    arducam-ov9281-usb-camera        present  usable    playfield=-                    uncalibrated  flatfield=absent  lens=absent'
[   4.86s] PASS  probe while held reported no EBUSY/busy error
[   4.86s] UNBIND 3-2.1.4
[   5.20s] PASS  unbind succeeded ()
[   5.59s] camera list shows absent after 0.39s
[   5.59s] PASS  camera list shows camera absent within ~2s (0.39s since unbind)
[   5.59s] PASS  image stream delivered a placeholder frame (0.0s after unbind)
[   5.59s] PASS  tag stream delivered camera_absent heartbeat (0.0s after unbind)
[   8.59s] PASS  placeholder frames keep flowing while absent (45 in 3 s)
[   8.59s] PASS  camera_absent heartbeats keep flowing while absent (3 in 3 s)
[   8.59s] PASS  no real frames while unplugged
[   8.59s] PASS  streams stayed open without error while absent ([])
[   8.86s] PASS  `camera image` while absent exits 3 (got 3; stderr='warning: camera arducam-ov9281-usb-camera: NO SIGNAL -- wrote test card, not a camera frame')
[   8.86s] PASS  `camera image` wrote the card (262289 bytes)
[   8.86s] BIND 3-2.1.4
[   9.97s] PASS  bind succeeded ()
[  10.89s] PASS  real frames resumed in the SAME image stream 0.92s after bind (limit 5.0s)
[  12.40s] PASS  resumed frames are 1280x800 (saw [(1280, 800)])
[  12.40s] PASS  tag stream resumed non-absent frames in the same stream
[  13.52s] PASS  camera list shows present again: '[2]    arducam-ov9281-usb-camera        present  usable    playfield=main-playfield       calibrated (pose, full)  flatfield=present  lens=measured  (stale, flatfield-stale)'
[  14.14s] after bind v4l2: 1280x800 {'auto_exposure': 1, 'exposure_time_absolute': 12}
[  14.14s] PASS  device format is 1280x800 (v4l2: 1280x800)
[  14.14s] PASS  control auto_exposure == 1 after replug (v4l2: 1)
[  14.14s] PASS  control exposure_time_absolute == 12 after replug (v4l2: 12)
[  14.45s] PASS  daemon not restarted (MainPID 2191040 -> 2191040)
[  14.45s] PASS  streams alive, no errors ([])
[  16.66s] PASS  probe while held idle reports present+usable: '[2]    arducam-ov9281-usb-camera        present  usable    playfield=-                    uncalibrated  flatfield=absent  lens=absent'
[  16.66s] PASS  idle probe reported no EBUSY/busy error
[  16.93s] PASS  `camera close` ok (closed camera 'arducam-ov9281-usb-camera' -- it reopens on next use)
[  17.39s] PASS  camera list shows closed: '[2]    arducam-ov9281-usb-camera        closed   usable    playfield=main-playfield       calibrated (pose, full)  flatfield=present  lens=measured  (stale, flatfield-stale)'
[  18.51s] PASS  `camera image` after close reopens it, exit 0 (0)
[  19.00s] PASS  camera list no longer closed: '[2]    arducam-ov9281-usb-camera        present  usable    playfield=main-playfield       calibrated (pose, full)  flatfield=present  lens=measured  (stale, flatfield-stale)'
[  19.53s] PASS  reopen applied frame size 1280x800 (v4l2: 1280x800)
[  19.53s] PASS  reopen applied auto_exposure == 1 (v4l2: 1)
[  19.53s] PASS  reopen applied exposure_time_absolute == 12 (v4l2: 12)
[  19.81s] journal: 9 lines, 2 warning/error lines
[  19.81s]   journal> 2026-11-01 16:55:12,537 WARNING aprilcam.camera.camera: arducam-ov9281-usb-camera: read failing, reopen attempt 1/10
[  19.81s]   journal> 2026-11-01 16:55:12,539 WARNING aprilcam.camera.camera: arducam-ov9281-usb-camera: reopen skipped -- hardware key not currently enumerated: camera hardware key is not currently enumerated (last known index 0)
[  19.81s] PASS  no error spam in the daemon journal (2 warning/error lines)
[  19.81s] ALL ASSERTIONS PASSED
```
