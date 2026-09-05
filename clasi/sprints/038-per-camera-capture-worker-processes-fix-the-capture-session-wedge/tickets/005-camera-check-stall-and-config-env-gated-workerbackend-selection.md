---
id: '005'
title: Camera.check_stall and config/env-gated WorkerBackend selection
status: open
use-cases:
- SUC-001
depends-on:
- '004'
github-issue: ''
issue: camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Camera.check_stall and config/env-gated WorkerBackend selection

## Description

Wire `WorkerBackend` (ticket 004) into the running daemon, gated so it
never changes runtime behavior until deliberately enabled, and add the
one small `Camera`-level addition the stall-detection watchdog
(ticket 006) needs:

1. **Backend selection.** `daemon/service.py`'s `_backend_factory`
   default (`_default_backend_factory` -> `OpenCVBackend`) becomes
   config/env-selectable to a `WorkerBackend`-constructing factory
   instead. `resolve_capture_backend` itself and its libcamera branch
   are unchanged — only what `backend_factory(index)` constructs for
   the default, non-libcamera case. Default stays `OpenCVBackend` so
   this ticket landing does not, by itself, change what any camera
   does at runtime.
2. **`Camera.check_stall()`.** A new method on `Camera` (see the
   sprint's `design/camera-DESIGN.md` overlay's "`Camera.check_stall()`
   — the proactive half of self-heal" note) that duck-types an optional
   `heartbeat_age() -> Optional[float]` on the primary backend — `None`
   for any backend without the concept (`OpenCVBackend`,
   `V4l2LoopbackBackend`: unconditional no-op), a real age in seconds
   for `WorkerBackend`. When the age exceeds a threshold, calls the
   exact same `_attempt_self_heal()` `Camera` already uses for a
   read-failure streak — **no new respawn code path**, no change to
   `_attempt_self_heal` itself, no duplicated failure-counting.

## Acceptance Criteria

- [ ] A config/env setting selects `WorkerBackend` construction in
      `daemon/service.py`'s `_backend_factory`; the unset/default case
      is byte-for-byte `OpenCVBackend`, unchanged.
- [ ] `_probe_openable` (`camera/discovery.py`) and `_camera_for`
      (`daemon/service.py`) agree on which backend a camera gets — both
      already go through the same `resolve_capture_backend`/
      `backend_factory` seam, so this should require no special-casing
      beyond the shared factory itself.
- [ ] `Camera.check_stall()` is a no-op (returns `False`, never touches
      the backend) for any backend lacking `heartbeat_age`.
- [ ] `Camera.check_stall()` against a fake backend whose
      `heartbeat_age()` reports a stale age triggers exactly one
      `_attempt_self_heal()` call (verify via the existing
      `_reopens`/`_consecutive_failures` bookkeeping or an injected
      spy), respecting the existing `MAX_REOPENS` budget — an already-
      exhausted camera does not get a bonus reopen attempt from this
      path beyond what `_attempt_self_heal` already allows.
- [ ] `check_stall()` does not interfere with or duplicate
      `Camera._read_lock`'s existing concurrency discipline — calling
      it concurrently with an in-flight `read()` must not deadlock or
      corrupt `_consecutive_failures`/`_reopens` state (reuse the
      existing lock rather than adding a new one, unless a documented
      reason requires otherwise).
- [ ] No test in this ticket requires real camera hardware; the
      backend-selection config test uses fakes for both branches.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/ tests/aprilcam/daemon/ -m "not camera and not integration"`
- **New tests to write**: config/env selects `WorkerBackend` vs.
  `OpenCVBackend` correctly; `check_stall()` no-op on a backend without
  `heartbeat_age`; `check_stall()` triggers self-heal exactly once on a
  stale-heartbeat fake backend; `check_stall()` respects the exhausted-
  budget contract (no bonus reopen past `MAX_REOPENS`).
- **Verification command**: `uv run pytest tests/aprilcam/camera/ tests/aprilcam/daemon/ -m "not camera and not integration"`

## Implementation Plan

**Approach**: Add the config/env gate (naming and exact mechanism —
`config/` document field vs. environment variable — decided at
implementation time, consistent with how this project's other
platform-conditional choices, e.g. `_default_control_backend_factory`,
are already gated) around which factory `DaemonService.__init__`
passes as `backend_factory`. Add `Camera.check_stall(threshold_s)` in
`camera/camera.py`, using `getattr(self._backend, "heartbeat_age",
None)` the same way `_describe_set_failure`/`get_property` already
duck-type an optional backend capability.

**Files to modify**:
- `src/aprilcam/daemon/service.py` (backend-selection gate)
- `src/aprilcam/camera/camera.py` (`Camera.check_stall`)

**Testing plan**: Unit tests for both changes using fake backends —
one exposing `heartbeat_age`, one not — no real camera hardware, no
real subprocess (that's exercised in ticket 004's own tests).

**Documentation updates**: None per-ticket — covered by the sprint's
already-approved `design/camera-DESIGN.md`/`design/daemon-DESIGN.md`
overlay content, applied to the canonical docs at sprint close.
