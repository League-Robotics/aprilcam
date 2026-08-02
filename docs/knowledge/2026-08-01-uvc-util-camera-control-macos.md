---
date: 2026-08-01
tags: [camera, uvc, macos, avfoundation, opencv, uvc-util, exposure, calibration]
related-tickets: [023-001]
---

# `cv2.VideoCapture.set()` does not control UVC cameras on macOS — use `uvc-util`

## Problem

Ticket 023-001 needed to wire `DaemonService.set_camera_config` so a
pushed `settings` document (exposure, gain, white balance, auto-modes)
actually reaches camera hardware, not just the config-document round
trip. The existing code (`camera/backends/opencv_backend.py`) already
had a `set_property`/`get_property` surface built on
`cv2.VideoCapture.set()`/`.get()`, unverified against real hardware
since sprint 020. The open question: is that generic OpenCV path
trustworthy, or does this project need the `uvc-util` subprocess adapter
archived v1 evidence (`archive/src/aprilcam/camera/DESIGN.md`,
`archive/camera_setup.sh`) already used?

## Symptoms

Pushing a manual-exposure `CameraConfig` to camera 3
(`arducam-ov9782-usb-camera`, an Arducam OV9782 USB Camera, macOS
AVFoundation backend) through `Camera.apply_config` — which calls
`OpenCVBackend.set_property`, i.e. `cv2.VideoCapture.set()` — failed
outright for **every one** of the five controls this project cares
about:

```
error: unsupported or rejected setting: 'auto_exposure'
error: unsupported or rejected setting: 'exposure'
error: unsupported or rejected setting: 'gain'
error: unsupported or rejected setting: 'auto_white_balance'
error: unsupported or rejected setting: 'white_balance_temperature'
```

This is not the ambiguous "reports success but nothing changes" failure
mode — `cv2.VideoCapture.set()` itself returns `False` for all five, on
this exact hardware, every time. `Camera.apply_config`'s existing
hard-error contract correctly turned each into `UnsupportedSetting`
rather than silently no-opping, but the push could never succeed through
that path at all.

## What Was Tried

1. **Trusted `cv2.VideoCapture.set()` as-is** (the pre-023-001 state,
   unverified since sprint 020). Result: hard failure on every control,
   confirmed live against camera 3, per above.
2. Considered whether this matched archived v1 evidence. v1's own
   record (`archive/camera_setup.sh`'s header comment) was explicit that
   **`uvcc`** — a *different* generic macOS UVC tool — does not work
   ("UVCAssistant claims the USB interface and every libusb control
   transfer stalls"), but said nothing one way or the other about
   OpenCV's `cap.set()` specifically. So this had to be verified live,
   not assumed from that prior evidence alone (see ticket 023-001's
   Description for the reasoning this validate-first approach was
   scoped from).

## What Worked

Built `camera/backends/uvc_util.py` (`UvcUtilBackend`), a control-only
adapter (not a full `CaptureBackend` — no `open`/`read`/`close`/
`frame_size`, since pixels still come from `OpenCVBackend`) that shells
out to the native `uvc-util` CLI:

```python
subprocess.run(
    [binary, "-N", device_name, "-s", f"{ctrl_name}={formatted}"],
    shell=False,   # always an argument list, never a shell string
    capture_output=True,
    text=True,
    timeout=5.0,
)
```

`Camera` gained an optional `control_backend` parameter; when present it
becomes the **exclusive** source for `apply_config`'s settings surface
(`get_property`/`set_property`/`settable_names`), replacing the primary
capture backend for those calls. `DaemonService`'s `_default_control_backend_factory`
wires a real `UvcUtilBackend` unconditionally on macOS (`sys.platform ==
"darwin"`) — not behind a try/except that would swallow a missing
binary into a silent fallback to the already-proven-broken OpenCV path.

Verified live against camera 3 through the real `DaemonService` (not
just the backend in isolation): pushing `exposure=2000` then
`exposure=6` via `aprilcam camera config` changed the captured frame's
mean grayscale brightness from **5.6** to **52.2** and back to **5.6**
— a ~9x swing, decisively real. `uvc-util -N "Arducam OV9782 USB
Camera" -S exposure-time-abs` confirmed the hardware control value
matched what was pushed at every step.

### Gotcha 1 — `uvc-util` can report success while silently clamping

Pushing an out-of-range value (`exposure-time-abs=99999`, real max
5000) had `uvc-util` exit `0` — but the control landed at `5000`, not
rejected and not left at its prior value. A naive
"exit code 0 ⇒ succeeded" backend would report the push as accepted
while the requested value silently didn't take (exactly the failure
mode `errors.UnsupportedSetting`'s docstring exists to kill), or, if
verified only by comparing against the *requested* value, would
correctly reject the push but leave the hardware clamped at `5000` —
neither the requested value nor the original one. That's a real,
observed live failure of the "leave the camera exactly as it was" rule
(UC-03.S-3), not a hypothetical.

**Fix**: `UvcUtilBackend.set_property` snapshots the control's value
*before* writing, then after writing reads it back and compares against
what was requested (with a small tolerance for legitimate step-size
snapping). On a mismatch it immediately writes the pre-call value back
(unverified — a raw repair write, not itself re-checked, to avoid
unbounded recursion) *before* returning `False`. This makes "this call
returned `False`" mean "the hardware is unchanged", unconditionally,
matching `Camera.apply_config`'s own external rollback loop, which only
reverts keys it saw a `True` for and has no visibility into a
within-single-key clamp like this.

### Gotcha 2 — a frame captured immediately after a push can still show the old exposure

`UvcUtilBackend`'s own readback (querying the *control*) confirms the
new value immediately — but a frame read from `OpenCVBackend` right
after the push can still visually reflect the *previous* exposure. The
sensor needs roughly a frame or two (empirically, 0.5s was enough on
this hardware) to actually apply a new exposure/gain to its pixel
output, even though the UVC control itself already reads back correctly.
A live test comparing frame brightness before/after a push without a
settle delay got backwards/misleading results (the "bright" push
measured darker than the "dark" push) until a `time.sleep(0.5)` was
added between the config push and the frame capture used for
comparison. See `tests/aprilcam/daemon/test_service.py`'s
`_SETTLE_S` constant and the real-hardware tests that use it.

## Why It Works

macOS's AVFoundation capture backend (what OpenCV uses on macOS) only
implements a small, unreliable subset of `cv2.VideoCapture`'s generic
property setters — this is a known, long-standing OpenCV/macOS
limitation, not specific to this camera model. UVC exposure/gain/white-
balance controls are set over a USB control-transfer channel that is
architecturally separate from the isochronous video-streaming pipe
AVFoundation's capture session owns; `uvc-util` (a native macOS UVC CLI)
talks to that control channel directly via IOKit, bypassing
AVFoundation/OpenCV's incomplete property-setter surface entirely. This
is also why `uvc-util` queries/writes work **concurrently with another
process holding the camera's AVFoundation capture session open** —
e.g. while the real daemon is running and streaming from the camera —
without needing to stop the daemon or close the capture handle first.
(Contrast: opening a **second** `cv2.VideoCapture` on the same OS index
from another process *does* conflict — AVFoundation capture sessions are
exclusive per process. `uvc-util`'s control channel is not the same
resource.)

## Future Guidance

- On macOS, never trust `cv2.VideoCapture.set()`/`.get()` for
  exposure/gain/white-balance on UVC cameras without live verification
  against the actual camera model — it may report failure outright (as
  here) or, on other hardware, report success while doing nothing. Use
  `uvc-util` (`camera/backends/uvc_util.py`'s `UvcUtilBackend`) instead;
  it is the proven mechanism for this project's real cameras.
- `uvc-util -N <device_name>` needs the **OS-reported device name**
  (`CameraIdentity.device_name` / `CameraRecord.device_name`), not the
  daemon's stable slug. On this dev machine, `uvc-util -d` lists:
  `Brio 501`, `Arducam OV9782 USB Camera`, `HD USB CAMERA`,
  `USB2 Video`. Resolve the `uvc-util` binary itself via `shutil.which`
  against `PATH` — never hardcode `~/.local/bin/uvc-util` even though
  that is where it happens to live on this machine.
- Any backend that shells out to a CLI tool for a hardware "set" must
  verify by reading the value back, and must be prepared for the tool
  to succeed-but-clamp rather than cleanly reject an invalid request —
  a bare "exit code 0 ⇒ trust it" is not enough, and neither is
  "readback mismatch ⇒ just report failure" without also repairing the
  now-changed hardware state back to where it was.
- A live test that captures a frame to observe the effect of a settings
  push needs a short settle delay (≥0.5s empirically) between the push
  and the capture — the control can read back correctly before the
  sensor's actual pixel output has caught up.
- Camera 3 (`arducam-ov9782-usb-camera`) is the real, currently-
  calibrated `main-playfield` camera on this rig. Its baseline hardware
  settings (as installed before ticket 023-001, and restored after
  every real-hardware test/experiment): `auto-exposure-mode=1` (manual),
  `exposure-time-abs=6`, `gain=2`, `auto-white-balance-temp=false`,
  `white-balance-temp=4600`. Any experiment against it should restore
  these afterward — `tests/aprilcam/daemon/test_service.py`'s
  `_CAMERA_3_BASELINE_SETTINGS` constant carries this forward for future
  real-hardware tests to reuse.
