---
title: Real camera hardware is enumerable but not capturable from an agent sandbox session (macOS TCC)
tags: [camera, hardware-tests, macos, tcc, permissions, sandbox, apriltag]
---

# Real camera hardware is enumerable but not capturable from an agent sandbox session

## The problem

Ticket 035-002 needed a real-hardware sweep against camera 5
(`arducam-ov9281-usb-camera`) to tune `vision.detector.DetectorConfig`
for AprilTag 1's marginal detection rate on that sensor. The automated
agent session working the ticket could not get a single real frame from
any attached camera — every open attempt failed.

## What was actually happening (confirmed, not assumed)

`aprilcam.camera.identity.resolve_all()` — pure AVFoundation device
enumeration — sees every camera physically attached to the host,
including camera 5:

```
{0: CameraIdentity(slug='brio-501', ...),
 1: CameraIdentity(slug='arducam-ov9281-usb-camera', ...),
 2: CameraIdentity(slug='hd-usb-camera', ...),
 3: CameraIdentity(slug='arducam-ov9782-usb-camera', ...),
 4: CameraIdentity(slug='usb2-video', ...)}
```

But opening any of them for actual capture from the agent session's own
process fails, verbatim:

```
OpenCV: not authorized to capture video (status 0), requesting...
OpenCV: camera failed to properly initialize!
[ WARN:0@0.xxx] global cap.cpp:480 open VIDEOIO(AVFOUNDATION): backend is
generally available but can't be used to capture by index
RuntimeError: failed to open camera at index 1
```

The enumerate-succeeds/open-fails split is the diagnostic signature of a
macOS TCC (Transparency, Consent, and Control) camera-capture
authorization gap for *this specific requesting process*, not a real
hardware absence, a busy-device conflict, or a code bug:

- Device enumeration (`AVCaptureDevice` listing) does not require camera
  authorization on macOS — only actually *starting a capture session*
  does. That is exactly the boundary `resolve_all()` (enumeration only)
  vs. `Camera.open()`/`OpenCVBackend.open()` (real capture) sits on, and
  it is exactly where success flips to failure above.
- An unattended/sandboxed automation session (an agent-launched `uv run
  python`/`pytest` subprocess with no interactive GUI, no addressable
  app bundle a human can find in System Settings > Privacy & Security >
  Camera to grant) has no route to satisfy the one-time authorization
  prompt macOS requires before the *first* successful capture from a new
  requesting process/bundle identity.

## Consequence for `@pytest.mark.camera` hardware tests

Every hardware test module in `tests/aprilcam/daemon/` already assumes
some sessions will have no working camera (`_skip_if_arducam_not_attached`
and friends) — but their skip messages are written for "no camera is
attached," which is misleading here: the camera *is* attached and even
independently calibrated in persisted state in some cases. A camera-5
specific hardware test should distinguish, in its own skip message,
"not attached" (`resolve_all()` has no matching slug) from "attached but
not authorized to capture" (enumerated, but `Camera.open()` raises) —
see `tests/aprilcam/daemon/test_detector_tuning_hardware.py`'s
`camera_5` fixture for the pattern. The second case should point a
future reader at camera permissions, not at "go check the USB cable."

## What this does *not* mean

This is not evidence against whatever real-hardware hypothesis a ticket
is investigating (e.g. ticket 035-002's "does `DetectorConfig` tuning
help AprilTag 1 on camera 5" question) — it is purely an environment
limitation on *this* automated session's ability to gather the data.
Do not fabricate or guess real-hardware numbers to route around it;
write the test so it skips cleanly with a clear, specific message, and
say plainly in the ticket/commit that the real measurement is a
stakeholder step at the physical rig (a session with genuine, already-
authorized camera access — e.g. an interactive terminal a human is
sitting at, which has presumably already been through the one-time TCC
prompt for that terminal app).

## If you need this fixed rather than worked around

Granting camera access to the process class running these agent
sessions (if it has a stable, addressable bundle identity at all) is a
macOS System Settings action a human must perform interactively — it is
out of scope for any agent to do on its own, and attempting to script
around a TCC prompt would defeat the purpose of that permission gate.
