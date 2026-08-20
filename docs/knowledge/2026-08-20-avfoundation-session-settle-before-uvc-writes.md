---
date: 2026-08-20
tags: [camera, uvc, macos, avfoundation, opencv, uvc-util, exposure, flaky-test]
related-tickets: [030-005]
---

# A UVC control push right after opening a camera races macOS AVFoundation's own session init — drain a few frames first

## Problem

Ticket 030-005 was reopened because a first investigation pass had
concluded a real-hardware `UnsupportedSetting` on
`test_set_camera_config_two_real_cameras_configured_independently` was
"a genuine but intermittent, non-deterministic hardware/driver write
rejection" and shipped only a diagnostic, no behaviour change. Measured
in isolation immediately after that conclusion shipped: **pass, fail,
fail** — a real failure rate far higher than the first pass's own
"roughly 1 in 5" estimate, and this path is not only a test: it is
`Camera.apply_config`, which also backs `aprilcam camera config` against
real hardware.

The stakeholder pointed at two things v1 knew that v2 didn't:
`auto-exposure-priority` (a control `grep -rn "auto-exposure-priority"
src/` found nowhere in v2), and v1's own documented settle requirement
after a mode change. The obvious hypothesis: v2's UVC control set was
missing a real prerequisite control.

## Symptoms

`DaemonService.set_camera_config` pushing `{"auto_exposure": 1,
"exposure": 300, "gain": 10}` to `hd-usb-camera` — immediately after
`get_frame()` opened it for the first time in that process — failed with
`UnsupportedSetting`, sometimes on `exposure` (`uvc-util -s
exposure-time-abs=300 exited 13: unable to write new value`), sometimes
on `auto_exposure` itself. A fresh `pytest` invocation of the exact
failing test, run 8 times in a row with no code change: **4 failed, 4
passed** (50%). This only ever reproduced on `hd-usb-camera`, and only
when it was opened in the same session as `arducam-ov9782-usb-camera`
(`hd-usb-camera` opened and pushed alone, in isolation, in a long-lived
Python process: 20/20 passed).

## What Was Tried

1. **The stakeholder's `auto-exposure-priority` hypothesis, tested
   directly against both real cameras.** `uvc-util -N "HD USB CAMERA"
   -c` does not list `auto-exposure-priority` at all — only the arducam
   exposes it (`uvc-util -N "Arducam OV9782 USB Camera" -c` does). The
   camera that actually fails does not have the control the hypothesis
   named, so it cannot be what gates that camera's write. Pushing
   `auto-exposure-priority=0` to the *arducam* before the *hd-usb-camera*
   push appeared to help in a small sample (8/8 passed) — but the honest
   read is that the extra `uvc-util` subprocess call this requires
   simply cost enough wall-clock time to act as an accidental settle
   delay (see below), not that any real control-semantics link exists
   between one camera's AE-priority and a different camera's own write.
   **This hypothesis did not hold up.**
2. **Reproduced with the real `Camera`/`UvcUtilBackend` code path
   directly** (not just via `pytest`), first in a single long-lived
   Python process looping the exact dual-camera push 35 times: 4/35
   (~11%) failed, always on `hd-usb-camera`, always `auto_exposure`.
   Low, but real.
3. **Reproduced with a genuinely cold process per trial** (`python -c
   ...` subprocess per attempt, matching what a real `pytest`
   invocation — or a real `aprilcam` CLI process — actually is): 6/10
   (60%) failed. The failure rate is dramatically higher in a cold
   process than in a process that has already opened these cameras
   before. That gap was the real clue.

## What Worked

v1 already knew about this, and v2 never ported it.
`archive/src/aprilcam/daemon/camera_pipeline.py`'s `CameraPipeline.
start()`:

> Drain a few frames so AVFoundation finishes initialising its capture
> session before we apply UVC controls — otherwise the OS resets them.
>
> ```python
> for _ in range(5):
>     cap.read()
> ```

v2's `Camera.open()` (`camera/camera.py`) has no equivalent — it opens
the backend and returns immediately, and `DaemonService._camera_for`
can push a stored config through `apply_config` right after `open()`
with zero intervening reads. Adding the same 5-frame drain, in the same
place (between open and the first real UVC push), fixed it:

| Condition (cold-process trials) | hd-usb-camera pass rate |
|---|---|
| No mitigation | 4/10 (40%) |
| 5-frame drain, no sleep | 10/10, then 12/12 more (22/22) |
| No drain, flat 1.5 s sleep instead | 10/10 |
| AE-priority pushed to arducam only, no drain | 8/8 (see caveat above) |

A plain sleep with no reads worked just as reliably as the drain in this
sample — the mechanism is wall-clock settle time, not specifically the
act of reading a frame — but the drain is what v1 itself proved, costs
nothing a caller wasn't already going to spend (a camera about to have
its exposure configured is about to be used for frames anyway), and
needs no arbitrary duration to tune.

**Fix, in `camera/camera.py`:** `Camera.open()` (and a successful
`_attempt_self_heal()` reopen) now marks `_pending_open_settle`.
`apply_config()` drains `_OPEN_SETTLE_READS` (5) frames from the primary
capture backend — bypassing `Camera.read()`'s own failure-counting, a
`None` here is expected, not a fault — the first time a push after a
fresh open actually has a real (non-`None`) value to write. Gated on
`control_backend is not None` (the macOS/UVC-control scenario this quirk
is specific to) and on the push having something real to write, so no
headless test and no non-macOS platform pays this cost.

**Belt-and-braces, in `camera/backends/uvc_util.py`:** the stakeholder
separately approved a bounded retry regardless of root cause, as
insurance against whatever transient the settle drain does not happen
to cover. `UvcUtilBackend.set_property` now retries a failed **raw** CLI
write (`uvc-util -s ... exited nonzero`) up to 3 attempts with a 0.1 s
backoff. It deliberately does **not** retry a write the CLI *accepted*
but that then reads back wrong (the existing mismatch/repair path — a
silently clamped out-of-range value is not a transient condition a retry
can fix), and a genuinely unknown control still fails on the first call,
before any retry loop — so the two cases the ticket named as needing to
"fail fast, not spin through retries" (control genuinely absent; value
genuinely out of range) are unaffected.

**Verification:** the previously-flaky test, run as a fresh `pytest`
invocation 12 times in a row post-fix: **12/12 passed** (vs. 4/8 on the
identical methodology pre-fix).

## Why It Works

Opening a `cv2.VideoCapture` on macOS starts AVFoundation's own
asynchronous capture-session setup; it does not block until that setup
is fully settled. A UVC control write issued immediately afterward (over
`uvc-util`'s separate IOKit control channel, per this project's own
`2026-08-01-uvc-util-camera-control-macos.md` entry) can land while that
session setup is still in flight — the driver either rejects the write
outright (`exited 13`) or accepts it and then has its own
still-in-progress init sequence stomp the value back to a default a
moment later, which is what produced the confusing "the *mode* write
itself was rejected, and the gate read back as already-manual"
signature this ticket's own diagnostic reported. Reading a few frames
(or simply waiting) gives that async setup time to finish before any
UVC write is attempted, closing the race. This is a session-startup
phenomenon, not a per-model or per-control one, which is consistent with
it reproducing on `hd-usb-camera` (a completely different chipset from
the arducam ticket 023-001 originally validated `uvc-util` against) and
with the earlier investigation's small sample missing it: a *warm*
process (cameras already opened once before) does not hit this race
nearly as often as a cold one, and a first pass that ran its trials in
one long-lived process rather than fresh processes per attempt was
measuring the easier, warmer case.

## Future Guidance

- **A "fixed it, ran the flaky test once, it passed" verification is not
  evidence** for an intermittent real-hardware failure. This exact
  ticket was closed once already on a conclusion drawn from roughly 10
  attempts in a warm process; the true failure rate in the actually
  representative case (a fresh process, matching both `pytest` and a
  real `aprilcam` CLI invocation) was 3-6x higher. When investigating a
  reported-intermittent hardware failure, reproduce with fresh
  subprocesses per attempt, not a loop inside one long-lived process —
  the two can give meaningfully different rates for a session-startup
  race like this one.
- Never trust a hypothesis about *which* v1 quirk explains a v2 failure
  without checking it against the *specific hardware that actually
  failed* — `auto-exposure-priority` was a real, documented v1 control
  v2 never ported, but it happened not to be this bug's cause because
  the failing camera (`hd-usb-camera`) doesn't even expose that control.
  The right quirk (AVFoundation session settle) was sitting in the same
  v1 file (`camera_pipeline.py`) the whole time, just not the one first
  suspected.
- Opening a fresh `cv2.VideoCapture`/AVFoundation session on macOS and
  immediately pushing UVC controls through a *separate* channel
  (`uvc-util`) is a race, not a reliable sequence — always drain a few
  frames (or otherwise wait) after open, before the first control write,
  regardless of camera model. `camera/camera.py`'s `_OPEN_SETTLE_READS`
  (5) is v1's own proven number, re-confirmed live here.
- A bounded retry on a *raw* CLI rejection is cheap, real insurance for
  a transient of this shape — but only for the raw-rejection path, never
  for a readback mismatch after an accepted write (that failure mode is
  the well-understood "silently clamped an out-of-range value" case from
  `2026-08-01-uvc-util-camera-control-macos.md`, not a transient one).
