---
id: '006'
title: Daemon stall-detection watchdog (check_stalled_cameras)
status: done
use-cases:
- SUC-001
depends-on:
- '005'
github-issue: ''
issue: camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon stall-detection watchdog (check_stalled_cameras)

## Description

Close gap 3 from
`clasi/issues/camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md`:
"the daemon should notice a stalled capture session" even when nobody
is actively reading that camera (no live stream, no recent one-shot
RPC). `Camera.read()`'s self-heal (including ticket 005's
`check_stall()`) is demand-driven — it only runs when something calls
it. Add `DaemonService.check_stalled_cameras()`, a sibling pass to the
existing `reassert_controls()` UVC-drift watchdog, driven by the same
background thread on the same interval
(`start_control_watchdog`/`CONTROL_READBACK_INTERVAL_S`): for every
held-open camera, call `Camera.check_stall()` (ticket 005) and log/
collect which cameras were found stalled and respawned, exactly
mirroring `reassert_controls`'s "never let one camera's exception stop
the pass" discipline.

Also add the periodic-retry-when-exhausted behavior: once
`MAX_REOPENS` is spent for a camera, `Camera.usable` stays `False`
exactly as it does today — but this watchdog pass should periodically
re-attempt a respawn for an exhausted worker-backed camera on a slow
interval, since a camera that was unplugged/replugged, or whose worker
was killed for an unrelated reason, should not require a full daemon
restart to try again (see `design/camera-DESIGN.md`'s Open Questions —
this ticket decides the exact retry interval/mechanism, an
implementation-time decision the architecture explicitly deferred).

## Acceptance Criteria

- [x] `check_stalled_cameras()` calls `Camera.check_stall()` on every
      held-open camera in one pass, same iteration/locking shape as
      `reassert_controls()` (snapshot `self._cameras.items()` under the
      lock, then act outside it).
- [x] One camera's `check_stall()` raising an unexpected exception does
      not stop the pass from checking every other held-open camera
      (mirrors `reassert_controls`'s existing per-camera try/except).
- [x] `check_stalled_cameras()` runs from the *same* background thread
      and interval as `reassert_controls()` (no second watchdog thread
      introduced — see the sprint's `design/daemon-DESIGN.md` overlay's
      rejected-alternative on this point).
- [x] A camera whose `check_stall()` is a no-op (any non-worker
      backend, or a healthy worker) produces no self-heal side effect
      from this pass — verified against a fake `OpenCVBackend`-style
      camera to confirm the extension is fully backward compatible.
- [x] An exhausted (`MAX_REOPENS` spent) worker-backed camera gets a
      periodic retry attempt from this pass, on an interval
      independent of the synchronous `_attempt_self_heal` budget, and a
      successful retry clears the exhausted state exactly the way a
      normal successful reopen already does (`_exhausted = False`,
      `on_reopen` fires) — no new "exhausted" bookkeeping duplicated in
      `DaemonService`.
- [x] No test in this ticket requires real camera hardware.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/ -m "not camera and not integration"` (confirm `reassert_controls`/watchdog-thread tests are unaffected)
- **New tests to write**: `check_stalled_cameras()` calls `check_stall()`
  on every held-open fake camera; one camera's exception doesn't stop
  the pass; a no-op backend produces no side effect; an exhausted fake
  worker-backed camera gets a periodic retry and clears its exhausted
  state on success; the watchdog thread runs both passes on one
  interval (no second thread spawned).
- **Verification command**: `uv run pytest tests/aprilcam/daemon/ -m "not camera and not integration"`

## Implementation Plan

**Approach**: Add `check_stalled_cameras()` alongside
`reassert_controls()` in `daemon/service.py`, reusing the identical
snapshot-under-lock-then-iterate-outside-lock shape and per-camera
try/except. Extend `start_control_watchdog`'s `_loop` closure to call
both passes each tick rather than introducing a second thread/timer.
Decide the exhausted-camera retry interval/mechanism here (a named
constant, documented rationale) — the architecture explicitly left
this as an implementation-time decision.

**Files to modify**:
- `src/aprilcam/daemon/service.py` (`check_stalled_cameras`,
  `start_control_watchdog`'s loop)

**Testing plan**: Unit tests using fake `Camera`/backend objects
injected into a `DaemonService` test fixture — no real camera
hardware, no real subprocess.

**Documentation updates**: None per-ticket — covered by the sprint's
already-approved `design/daemon-DESIGN.md` overlay content, applied to
the canonical doc at sprint close.
