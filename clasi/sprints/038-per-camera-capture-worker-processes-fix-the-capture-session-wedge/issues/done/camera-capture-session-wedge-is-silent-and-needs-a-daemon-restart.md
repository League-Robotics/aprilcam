---
status: done
sprint: 038
tickets:
- 038-001
- 038-002
- 038-003
- 038-004
- 038-005
- 038-006
---

# A wedged camera capture session is silent, self-reports healthy, and only a full daemon restart recovers it

## Description

A camera's AVFoundation capture session can stop yielding frames while
the daemon continues to report that camera `present`/`usable`. Every
read (`camera image`, `get_frame`, `get_tags`) then fails with
**"no frame available from camera 'N'"** indefinitely, and
`aprilcam camera view N` **hangs forever with no error at all** — it
connects, opens the window, subscribes, and waits for a first frame
that never arrives.

Observed twice in four days on this host, on different cameras and
different sensors, so this is not sensor-specific:

- 2026-08-23 — camera 5 (`arducam-ov9281-usb-camera`), after repeated
  config pushes + captures while a view streamed.
- 2026-08-26 — camera 2 (`hd-usb-camera`), ~8 hours after a clean
  daemon start. Cameras 1, 3 and 5 captured normally throughout and
  `system_profiler SPCameraDataType` still listed the device.

In both cases the only recovery was
`aprilcam daemon stop && aprilcam daemon start`. The daemon never
detects the condition and never self-heals.

Operational detail, including the misleading signals below, is in
`docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md`.

## Why it costs real time

The wedge presents as a hang, which reads to an operator as "the
camera is unplugged." On 2026-08-26 two agents independently chased a
suspected cable/hardware fault before the daemon was even suspected.
Nothing in `daemon status`, `camera list`, or `camera probe`
contradicts the unplugged theory — the camera claims to be fine.

Two signals actively point the wrong way:

- `camera probe` with any stream live reports **every** camera as
  `in use by a live subscriber`, skips them, and prints the skipped
  rows as `playfield=-`/`uncalibrated`. That looks like config loss
  but is only probe's transient view.
- Killing the hung `camera view` process does **not** clear the wedge.
  It releases the stream sockets, but the very next `camera image`
  fails identically. The hung view is a symptom, not the cause — an
  operator who stops there concludes the camera is dead.

## Three gaps worth fixing, independently landable

1. **`usable` should mean "delivers frames."**
   `camera/discovery.py::_probe_openable()` does `open()` then
   `close()` and never reads a frame, so a device whose capture
   session is wedged still probes `usable`. This is the single most
   misleading signal in the whole failure: the status table asserts
   health that was never tested. Usability should require a successful
   frame read (with the cost/latency tradeoff of doing that during
   probe considered explicitly — a probe that reads a frame is
   materially slower, and `probe` runs over every camera).

2. **`camera view` must not hang forever.** A first-frame timeout that
   fails with a diagnostic naming the camera and the likely cause
   (wedged session; suggest a daemon restart) turns an open-ended hang
   into a 10-second answer. This is the highest-leverage of the three
   for operator experience, and the cheapest — it is a client-side
   bound on an existing subscription, not new daemon machinery.

3. **The daemon should notice a stalled capture session.** If a camera
   that previously produced frames stops producing them for some
   threshold while still enumerated, the daemon should either
   re-open the session (self-heal, the ideal outcome — no restart, no
   lost streams on other cameras) or mark the camera `unusable` with a
   stated reason so the status table stops asserting health. Marking
   it is strictly better than the current silence even if automatic
   recovery proves unreliable.

## Notes / open questions

- Root cause of the wedge itself is **not** established. All that is
  confirmed is the signature (enumerates and opens fine, never yields
  a frame) and that a process restart clears it — which points at
  per-process AVFoundation session state rather than the device or the
  USB bus, since the device stays visible to `system_profiler` and
  other cameras on the same host keep working. Worth determining
  whether it correlates with UVC control writes during an active
  stream (the 2026-08-23 occurrence followed exactly that), because if
  so gap 3's self-heal may be treating a symptom of an ordering bug
  that is separately fixable.
- Gap 2 is worth doing regardless of whether the root cause is ever
  found: an unbounded wait on a first frame is a bad failure mode even
  if wedges become rare.
- Related but distinct:
  `clasi/issues/later/daemon-start-should-fail-loudly-without-capture-backend.md`
  covers the daemon starting with **no capture backend at all**. That
  issue's cameras never work; this issue's cameras work and then stop.
  Both surface as "cameras look absent/broken with no useful error,"
  so a fix for either should keep the two diagnoses distinguishable.

## Root cause established 2026-09-04 (with the new daemon log file)

Third and fourth occurrences, both on camera 2 (`hd-usb-camera`, 1920x1080),
both while an image stream (`camera view 2` / `playfield view 2`) was
subscribed for tens of minutes; the last one 2 h after a clean start, at
20:13:38. The log shows exactly what the daemon did: reads returned
`None`, and `Camera._attempt_self_heal` closed and reopened the backend
ten times in one minute (`read failing, reopen attempt n/10`); every
reopen "succeeded" (`isOpened()` true) and never delivered a frame, so the
camera went `unusable`. The device index had not changed (still 2).

**Decisive test, run by the stakeholder from Terminal.app while the
daemon was still wedged:**

```
python -c "import cv2; c=cv2.VideoCapture(2, cv2.CAP_AVFOUNDATION); print(c.isOpened()); print(c.read()[0], c.read()[1].shape)"
-> opened True / frame True (1080, 1920, 3)
```

A **fresh process** reads the camera fine at the very moment the daemon
cannot, through the same index and backend. The wedge is therefore
**process-local**: OpenCV's AVFoundation backend leaves the device's
capture session in a state that no new `VideoCapture` in the *same*
process can recover from (`release()` does not fully tear it down), and
only a process boundary clears it. Every in-process reopen strategy
(delays, backoff, fresh backend objects, re-resolved index) is therefore
futile for this failure. This is why a daemon restart was always the only
cure.

**The fix: per-camera capture worker processes.** The daemon spawns one
small worker per opened camera (`python -m aprilcam.camera.capture_worker
--index N ...`), which owns the `VideoCapture` and publishes frames to the
daemon over shared memory (a ring of a few frames; 6 MB each at 1080p,
so a pipe is the wrong transport) with a control channel for
`get/set_property` and settle reads. `Camera.read()` becomes "latest
frame from the worker"; self-heal becomes **kill and respawn the worker**
(which is what the stakeholder's test did), with the existing failure
counting and budget on top. TCC camera access is inherited by children
of the daemon, so a worker has the same grant the daemon has. Bonus: a
worker crash no longer takes the daemon or the other cameras down, and
the probe's `usable` can finally mean "delivers frames".

Gaps 1 and 2 from the original write-up still apply (probe should read a
frame; `camera view` must not hang forever). Gap 3 is now precisely
"respawn the worker".
