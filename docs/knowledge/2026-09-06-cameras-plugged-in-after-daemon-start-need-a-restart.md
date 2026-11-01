# Cameras plugged in after the daemon starts never appear until the daemon restarts (macOS)

Hit 2026-09-06 with two new cameras ("Streaming Camera", a Sonix
0x0c45:0x636b bridge, and "Global Shutter Camera", ELP 0x32e4:0x15b1).

## Symptom

A camera plugged in while the daemon is running shows in Photo Booth
and in `system_profiler SPCameraDataType`, but `aprilcam camera list`
and `aprilcam camera probe` never list it — not as `absent`, not at
all. The daemon log says nothing about it. A fresh Python process in
the same venv running `cv2_enumerate_cameras.enumerate_cameras()` sees
the camera immediately.

## Cause

Enumeration goes through cv2-enumerate-cameras' macOS backend, which
queries an `AVCaptureDeviceDiscoverySession`. AVFoundation delivers
device connect/disconnect notifications through the **main thread's
NSRunLoop**. The backend pumps the *current* thread's run loop once
before enumerating, which is enough in a short-lived CLI process but
not in the daemon: `discovery.watch()` and `probe()` run on worker
threads, whose run loops never receive the notifications, so the
discovery session keeps returning the device set from daemon startup.
The 2-second passive watch (UC-25.S-5) therefore cannot see hotplug on
macOS, and a forced probe does not help because it uses the same
enumeration.

> Superseded by sprint 047 (`MacosAvfPresenceSource` + main-thread run-loop
> pump; see `2026-11-01-camera-hotplug-v4l2-findings.md`). Kept for the
> diagnosis.

## Fix / workaround

Restart the daemon (see
`daemon-restart-launches-pipx-build-no-cameras.md` for which build
must start). The restarted daemon enumerated both new cameras and
registered them as new slots.

A real fix would pump the main run loop before enumerating, or run
enumeration on the daemon's main thread. Not yet ticketed.

## Diagnostic that settles it in one step

```bash
~/.local/pipx/venvs/aprilcam/bin/python -c "from cv2_enumerate_cameras import enumerate_cameras
for c in enumerate_cameras(): print(c.index, repr(c.name), c.vid, c.pid, c.path)"
```

If this lists the camera and `aprilcam camera list` does not, the
daemon's device list is stale; restart it.

## See also

- `2026-09-06-usb-hub-bandwidth-flapping-with-five-cameras-on-one-root-port.md`
  — a related but distinct incident from the same day: *existing*
  cameras briefly leaving and rejoining the bus (and coming back at
  shifted indices) under USB bandwidth pressure, rather than a *new*
  camera never being seen at all.
