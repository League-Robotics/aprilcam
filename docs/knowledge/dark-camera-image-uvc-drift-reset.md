# Dark camera image while tags still detect → check UVC readback, then `camera config --reset`

Diagnosed live 2026-08-22/23 on camera 5 (`arducam-ov9281-usb-camera`).

## Symptom

`camera view` / `playfield view` nearly black (measured mean luma
18/255, max 110), yet tag detection keeps working — AprilTag/ArUco
markers carry enough local contrast even when the scene is badly
underexposed. The daemon's `frame_dark` advisory shows in the viewer
status line.

## Diagnosis

Compare what the daemon *believes* against what the hardware *has*:

```sh
aprilcam camera config <n> --dump         # daemon's live config
cat ~/.config/aprilcam/cameras/<slug>/config.json   # on-disk config
uvc-util -N "<device name>" -g exposure-time-abs    # hardware truth
uvc-util -N "<device name>" -g gain
uvc-util -N "<device name>" -g auto-exposure-mode
```

In this incident: config (live *and* disk) said `exposure: 300`;
hardware read back `exposure-time-abs = 3`, `gain = 0`. The daemon
never re-asserts controls it thinks are applied, so drift persists
indefinitely.

## Cause

UVC controls are shared mutable state with multiple writers. Known
stompers on this host: the real-hardware test suite
(`test_calibration_hardware.py` etc. push configs and can abort
mid-run on the AVFoundation settle race — see
`2026-08-20-avfoundation-session-settle-before-uvc-writes.md`), and
the v1 daemon/viewer running concurrently from
`/Volumes/Proj/proj/RobotProjects/AprilTags`.

## Fix

```sh
aprilcam camera config --reset <n>   # re-applies on-disk config, live, no restart
```

Verify with the `uvc-util -g` readbacks and a fresh
`aprilcam camera image <n> -o /tmp/check.jpg`.

Durable fix is an open design question — see
`clasi/issues/later/daemon-cannot-detect-uvc-control-drift-camera-left-dark.md`.
Known-good values per camera: exposure 300 (camera 5, OV9281);
exposure 6 / gain 2 / wb 4600 (camera 3, OV9782 — see
`2026-08-23-v1-known-good-arducam-ov9782-exposure-settings.md`).
