---
id: '002'
title: Camera read synchronization
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Camera read synchronization

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint, even one that does
not depend on it (e.g. ticket 001).

## Description

Today, nothing synchronizes calls to `camera.camera.Camera.read()`.
That has been safe by accident: every existing caller (`get_frame`,
`DetectionEngine.run_burst` via `get_tags`/`get_tag`) is a single
synchronous read per RPC call, and while the gRPC server's
`ThreadPoolExecutor` (`daemon/rpc.py`, `max_workers=10`) already makes
concurrent RPCs against the *same* camera theoretically possible today,
nothing has yet exercised that gap under load. Tickets 003/004 add a
**persistent background reader thread per camera** once a stream
subscriber exists — a real, continuous second reader that coexists
with one-shot RPC reads for the rest of the sprint. This ticket adds a
read lock local to `Camera` so the two never race on the underlying
capture handle, before that persistent reader exists.

This sprint deliberately does **not** redirect one-shot queries to
reuse a live producer's latest frame (`sprint.md` Design Rationale 4) —
a one-shot `get_frame`/`get_tags` call against a camera with an active
stream still performs its own independent capture. The lock built here
is what makes that safe rather than racy; it is not a merge of the two
read paths.

## Acceptance Criteria

- [x] `Camera.read()` acquires an instance-local lock for the duration
      of the underlying backend read, so two threads calling `read()`
      concurrently on the same `Camera` never interleave calls to the
      underlying `CaptureBackend`.
- [x] No behavior change for any existing single-threaded caller —
      `get_frame`/`get_tags`/`DetectionEngine.run_burst` produce
      identical results to before this ticket when called from a
      single thread.
- [x] The lock is scoped to `Camera.read()` only (not `open()`/
      `close()`/`apply_config()`, which are unaffected unless already
      guarded elsewhere) — keep the change minimal and targeted.
- [x] `uv run mypy` clean. Zero `typing.Protocol`.
- [x] Full existing suite (1099 tests, headless baseline) still passes
      (baseline was actually 1122 by the time this ticket ran, since
      ticket 001 landed first; 1125 pass now — baseline + 3 new
      concurrency tests).

## Implementation Plan

**Approach.** Add a `threading.Lock` (or equivalent) as a `Camera`
instance attribute, held for the duration of the backend `read()` call
inside `Camera.read()`. Keep the change small and local —
`camera/camera.py` currently has no locking at all (verified: no
`threading` import), so this is a net-new, narrowly scoped addition,
not a refactor of existing synchronization.

**Files to create/modify:**
- `src/aprilcam/camera/camera.py` (add the lock)

**Testing plan:** see Testing section below — this ticket is provable
headlessly with `FakeBackend` and two real Python threads; no camera
hardware needed.

**Documentation updates:** a short docstring note on `Camera.read()`
explaining why the lock exists (a persistent background stream reader
now coexists with one-shot RPC reads) — point at this ticket/sprint
for context, matching this codebase's convention of citing the
originating ticket in load-bearing comments.

## Testing

- **Existing tests to run**: `tests/aprilcam/camera/test_camera.py`
  and the full suite, `uv run pytest`.
- **New tests to write**: two threads calling `read()` concurrently
  against a `FakeBackend` (extend `tests/aprilcam/camera/
  test_camera.py`'s existing `FakeBackend` with a small artificial
  delay inside `read()` to make a race observable) — assert every
  `read()` call sees the backend's `is_open`/queue state consistently
  and call counts match exactly (no lost or duplicated reads); prove
  the lock is load-bearing by showing the same test fails without it
  (a regression check the ticket's completion notes should mention
  having run once, not a permanent test).
- **Verification command**: `uv run pytest && uv run mypy`.

## Completion Notes

- **Locking scope: the entire body of `Camera.read()`, not just the
  `self._backend.read()` call.** The lock (`self._read_lock =
  threading.Lock()`, set in `__init__`) is acquired via `with
  self._read_lock:` wrapping `read()`'s whole method body, including
  the `_attempt_self_heal()` call it may trigger on the 5th consecutive
  failure. This is a deliberate reading of the acceptance criterion
  "never interleave calls to the underlying `CaptureBackend`" —
  `_attempt_self_heal()` calls `self._backend.close()`/`self._backend.
  open()` on the *same* backend object, so locking only the plain-read
  branch would leave those two calls racing against a concurrent
  thread's `backend.read()`. No deadlock risk: nothing reached from
  within `read()` (`_safe_read`, `_attempt_self_heal`, the injected
  `on_reopen` callback — verified against `DaemonService._mark_stale`,
  its only real wiring) calls back into `read()` itself, so a plain
  non-reentrant `Lock` never sees same-thread re-acquisition.
- **Lock scope excludes `open()`/`close()`/`apply_config()`/
  `effective_config()`**, exactly as the acceptance criteria specify.
  This is a known, accepted narrowing: nothing in this sprint calls
  `open()`/`close()` from a background thread concurrently with
  `read()` (producers in tickets 003/004 only call `read()` on their
  timer; `open()`/`close()` happen at producer start/stop or from
  `DaemonService`), so this ticket does not close that broader race —
  see the Honesty/Finding note below.
- **Baseline was 1122 (already advanced by ticket 001); suite now
  1125** (`uv run pytest -m "not camera"` → `1125 passed, 40
  deselected`, ~95s). 3 new tests in `tests/aprilcam/camera/
  test_camera.py`: `test_concurrent_reads_never_interleave_backend_calls`,
  `test_concurrent_reads_self_heal_reopen_stays_correct_under_pressure`,
  `test_concurrent_reads_do_not_deadlock`. `FakeBackend` gained
  `read_delay` (artificial per-read delay, default 0, to widen the race
  window) and a `_busy`/`reentrant_hits` reentrancy detector (records,
  rather than raises, since `Camera._safe_read()`/`_attempt_self_heal()`
  both swallow backend exceptions — asserted on directly post-run
  instead). `uv run mypy` → "Success: no issues found in 73 source
  files". Zero `typing.Protocol` in new/changed code.
- **Load-bearing regression check, run once (not committed):** with the
  `with self._read_lock:` wrapper temporarily stripped back out to the
  pre-ticket unlocked body, all 3 new concurrency tests failed
  (`backend.reentrant_hits == 3` instead of `0` in two of them, on top
  of the deadlock-guard test also failing the same way) — confirming
  the tests actually exercise the race and the lock is what closes it,
  not a tautology. The file was restored from a full backup immediately
  after, `git diff --stat` confirmed only the intended lock-related
  hunk remains, and the full suite + mypy were re-run clean afterward.
- **Honesty / finding (not fixed by this ticket, in scope by design):**
  `_attempt_self_heal()`'s own budget check (`if self._reopens >=
  MAX_REOPENS`) is check-then-act, but that is *not* a race under this
  ticket's lock — the entire `read()` call, including this check, now
  runs as one atomic unit per thread, so two threads can never both
  pass the check and both increment `_reopens` past the cap. The real,
  still-open race is different and pre-existing: `open()`/`close()`
  are NOT covered by `_read_lock` (by design, per the acceptance
  criteria), so an external caller invoking `Camera.close()` or
  `Camera.open()` concurrently with an in-flight `read()` (including
  one that is mid-self-heal) still races on `self._backend` and on
  `self._present`/`self._consecutive_failures`/etc — e.g. `close()`
  resetting `_reopens = 0` while a concurrent `read()` is mid-
  `_attempt_self_heal()` reading/writing that same counter is a real
  torn-state possibility. Nothing in this sprint exercises that
  combination (producers call only `read()`; `open()`/`close()` are
  driven by `DaemonService` at producer/camera lifecycle boundaries,
  not concurrently with an active reader per the current architecture),
  so it is not fixed here — flagging it explicitly rather than
  implying `Camera` is now fully thread-safe end-to-end. A future
  ticket that needs concurrent `open()`/`close()`/`read()` should widen
  the lock's scope (or add a second one) rather than assume this
  ticket already covers it.
- **What tickets 003/004 need to know**: `Camera.read()` is now safe to
  call from a background producer thread at the same time a one-shot
  `get_frame`/`get_tags` RPC calls it on another thread — both go
  through the same `_read_lock`, so calls fully serialize (one
  complete `read()`, including any self-heal it triggers, before the
  next begins). Producers should call only `read()` in their steady-
  state loop; `open()`/`close()` remain the caller's own responsibility
  to sequence outside of concurrent `read()` pressure (see the finding
  above) — in practice this means `open()` before starting a producer
  thread and `close()` only after it has been joined/stopped, matching
  how `DaemonService` already manages camera lifecycle today.
