---
status: pending
---

# Camera reopen reuses a stale OpenCV index and can silently open a different physical camera

## Description

Observed 2026-09-06 while two new cameras on an overloaded USB 2.0 hub
kept dropping off the bus. The daemon opens a camera by the OpenCV
index resolved at first open and the stall watchdog reopens with that
same index. AVFoundation indices are positional, so when any camera
leaves the bus every index above it shifts:

- `global-shutter-camera` (index 5) left the bus. Its reopen retried
  index 5 forever: `OpenCV: out device of bound (0-4): 5`, "stall
  watchdog respawn FAILED ... retried every 60s".
- `streaming-camera` (index 4) then left the bus while
  `global-shutter-camera` came back at index 4. The watchdog reopened
  slot 7 at index 4 and **served the Global Shutter Camera's frames as
  `streaming-camera`**. `aprilcam camera image 7` returned the other
  camera's view and `camera tags 7` reported its tags. Nothing in the
  output or log indicated the substitution.

The second case is the serious one: a camera slot silently delivering
another camera's pixels defeats every identity guarantee
`camera/identity.py` exists to provide. v1's identity fallback chain
handled exactly this (CLAUDE.md: "camera identity across re-plugs").

### Needed

1. Every open and reopen re-resolves the index from the record's
   `hardware_key` via a fresh `resolve_all()` immediately before
   opening; never reuse a cached index.
2. If the hardware key is not currently enumerated, report
   `present=False`/`usable=False` and keep retrying the identity
   lookup, not the old index.
3. After open, verify the opened device matches the expected identity
   where the backend can tell (AVFoundation uniqueID); refuse and log
   loudly on mismatch.
4. Test: enumeration snapshot with a device removed between open and
   reopen must not open the shifted device.

Related: `camera-probe-reports-a-held-open-camera-as-usable-without-checking-it.md`.
