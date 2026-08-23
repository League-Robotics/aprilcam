---
title: v1's known-good UVC settings for arducam-ov9782-usb-camera (camera 3)
tags: [camera, uvc, exposure, calibration, arducam, ov9782]
---

# v1's known-good UVC settings for `arducam-ov9782-usb-camera`

## The problem

v2's persisted config for `arducam-ov9782-usb-camera` ("camera 3", the
color arducam) had `exposure: 100` — badly overexposed, `aprilcam
playfield view` came back nearly all-white. `exposure: "100"` (a
string) is what the type-coercion fix in commit `b492cc8` found and
fixed the *type* of, but nobody had verified `100` was ever the right
*value* — it silently carried forward from whatever earlier session
set it.

## The known-good reference

v1 (the sibling `/Volumes/Proj/proj/RobotProjects/AprilTags` checkout,
which has been running this exact physical camera successfully for
hours) has the real values at
`config/aprilcam-old/cameras/arducam-ov9782-usb-camera/config.json`:

```json
"settings": {
  "controls": {
    "auto-exposure-mode": "1",
    "auto-exposure-priority": "0",
    "auto-white-balance-temp": "false",
    "exposure-time-abs": "6",
    "gain": "2",
    "white-balance-temp": "4600"
  },
  "program": "uvc-util"
}
```

Mapped onto v2's `CameraConfig.settings` field names and verified
live against real hardware (2026-08-23 — full color image, all 10
ArUco markers plus AprilTag 1 detected stably across repeated polls):

```json
{
  "auto_exposure": 1,
  "exposure": 6,
  "gain": 2,
  "auto_white_balance": 0,
  "white_balance_temperature": 4600
}
```

v1's `auto-exposure-priority: "0"` has no equivalent field in v2's
`CameraConfig.settings` schema — not carried over, not seen to matter.

**Note the boolean trap**: v2's config loader rejects a JSON boolean
for `auto_white_balance` ("must be a number, got a boolean") — push
`0`, not `false`.

## `global-shutter-camera` is not `arducam-ov9281-usb-camera`

v1's config also has a `global-shutter-camera` entry
(`device_name: "Global Shutter Camera"`, `1920x1080`) — checked while
looking for a reference for camera 5 (`arducam-ov9281-usb-camera`,
1280x800). These are different physical devices; v1 has no config for
our actual OV9281. Camera 5's exposure was tuned empirically instead
(see the sprint 031 close-out session, 2026-08-22/23 — `exposure: 300`
worked well visually and for detection, an unrelated value from a
different sensor, don't cross-apply it to camera 3 or vice versa).

## Known gotcha: config pushes often don't persist to disk

`aprilcam camera config <file>` reports success and the live daemon
genuinely applies the new settings (verified: the very next capture is
correctly exposed), but the on-disk
`<config_dir>/cameras/<slug>/config.json` frequently does **not**
reflect the push — seen on both a linked and an unlinked camera, both
for setting new values and for clearing a field to `null`. A daemon
restart silently reverts to the stale on-disk value with no warning.
Tracked in `clasi/issues/camera-config-clearing-a-field-does-not-persist-to-disk.md`
(despite the name, now covers more than clearing). Until that's fixed:
after any live config push you actually want to keep, verify the
on-disk file directly and hand-edit it if the push didn't take.
