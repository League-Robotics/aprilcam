---
status: in-progress
sprint: '047'
tickets:
- 047-008
- 047-010
---

# Cameras plugged in after the daemon starts are invisible until a restart (macOS run-loop staleness)

## Description

2026-09-06: two newly attached cameras were visible in Photo Booth and
`system_profiler SPCameraDataType`, and a fresh Python process running
`cv2_enumerate_cameras.enumerate_cameras()` listed both, but the
running daemon's `camera list`/`camera probe` never showed them, not
even as absent. A daemon restart made both appear. Full write-up:
`docs/knowledge/2026-09-06-cameras-plugged-in-after-daemon-start-need-a-restart.md`.

Cause: cv2-enumerate-cameras' macOS backend pumps the *current*
thread's `NSRunLoop` once before querying
`AVCaptureDeviceDiscoverySession`, but AVFoundation delivers device
connect/disconnect notifications on the **main** thread's run loop.
`discovery.watch()` and `probe()` enumerate from worker threads, so the
device set is frozen at daemon start. This breaks UC-25.S-5's passive
hotplug watch on macOS.

### Needed

- Pump the main run loop (or perform enumeration on the main thread)
  before each enumeration pass on darwin, then verify a camera plugged
  in after start appears in `camera list` within the watch interval.
