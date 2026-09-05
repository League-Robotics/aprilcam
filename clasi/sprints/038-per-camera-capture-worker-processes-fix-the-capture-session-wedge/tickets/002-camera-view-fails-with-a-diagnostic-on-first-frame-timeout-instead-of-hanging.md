---
id: '002'
title: camera view fails with a diagnostic on first-frame timeout instead of hanging
status: open
use-cases:
- SUC-004
depends-on: []
github-issue: ''
issue: camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# camera view fails with a diagnostic on first-frame timeout instead of hanging

## Description

`aprilcam camera view <camera>` hangs forever with no error when the
subscribed stream never delivers a first frame — `view/viewer.py`'s
`StreamLoop.run` pumps the window and samples its single-slot `_latest`
cell on a fixed cadence indefinitely, with no bound on "how long since
we started with nothing yet received." This is gap 2 from
`clasi/issues/camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md`
and is fully independent of the worker-process tickets (003-006) — it
fixes the *symptom* (a silent, open-ended hang) regardless of *cause*
(a wedged session, a daemon that never subscribed, anything else).

Add a bounded first-frame timeout to `StreamLoop.run`: if
`first_frame_timeout_s` elapses with `_latest` still unset, close the
stream, join the reader thread (reusing the existing bounded-join
discipline), and raise a new, client-local `FirstFrameTimeout(camera)`
exception — deliberately **not** an `aprilcam.errors.AprilCamError`,
mirroring `NoDisplayAvailable`'s existing "this is local to the client
process, not the daemon error taxonomy" pattern. Propagate it through
`run_raw_view` unchanged. In `cli/camera.py`'s `cmd_view`, catch it
alongside the existing `NoDisplayAvailable`/`AprilCamError` catches and
print a diagnostic naming the camera and the likely cause (a wedged
capture session) before returning `EXIT_ERROR`.

See the sprint's `design/view-DESIGN.md` and `design/cli-DESIGN.md`
overlay copies for the full design and rationale (including why this
lives in `view/viewer.py`'s existing polling loop rather than as a new
`client/streams.py` socket timeout).

## Acceptance Criteria

- [ ] `StreamLoop` accepts a `first_frame_timeout_s` parameter (a real
      default for production use, injectable short for tests).
- [ ] A stub `ImageStream` that never yields a frame causes
      `StreamLoop.run` to raise `FirstFrameTimeout` within the
      configured bound, with the stream closed and the reader thread
      joined before the exception propagates.
- [ ] `FirstFrameTimeout` is a plain exception (not
      `aprilcam.errors.AprilCamError`), carrying the camera name.
- [ ] `run_raw_view` propagates `FirstFrameTimeout` unchanged (window
      closed in its existing `finally`, same as any other stream
      failure).
- [ ] `cli/camera.py`'s `cmd_view` catches `FirstFrameTimeout` and
      prints a diagnostic naming the camera and suggesting the likely
      cause (a wedged capture session) and a next step, then returns
      `EXIT_ERROR` — never an unhandled exception, never a hang.
- [ ] A stream that *does* deliver a first frame within the bound is
      completely unaffected — no timing regression to the existing
      `_PUMP_INTERVAL_S` cadence or ordinary shutdown path.
- [ ] Once any frame has been received, the timeout logic never fires
      again for the rest of that stream's life (this bounds only the
      *first* frame).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/view/ tests/aprilcam/cli/ -m "not camera and not integration"`
- **New tests to write**: a stub `ImageStream` yielding zero frames,
  with a short injected `first_frame_timeout_s`, confirming
  `FirstFrameTimeout` is raised and the reader thread does not survive;
  a `cmd_view`-level test confirming the diagnostic message and
  `EXIT_ERROR` exit code on that exception; a regression test
  confirming a stream that yields a frame promptly is unaffected.
- **Verification command**: `uv run pytest tests/aprilcam/view/ tests/aprilcam/cli/ -m "not camera and not integration"`

## Implementation Plan

**Approach**: Track a start timestamp (`time.monotonic()`) in
`StreamLoop.run` when the loop begins; on each pump-cadence tick,
before or alongside sampling `_latest`, check whether
`first_frame_timeout_s` has elapsed with `_latest` still `None`. On
timeout, perform the same stream-close-and-reader-join teardown the
method's `finally` block already does, then raise `FirstFrameTimeout`.
Define `FirstFrameTimeout` in `view/viewer.py` alongside
`NoDisplayAvailable`. Wire `cmd_view` to catch it with a message naming
the camera.

**Files to modify**:
- `src/aprilcam/view/viewer.py` (`StreamLoop`, `run_raw_view`, new
  `FirstFrameTimeout`)
- `src/aprilcam/cli/camera.py` (`cmd_view`)

**Testing plan**: New unit tests for `StreamLoop`'s timeout behavior
using a stub `ImageStream` (no real socket, no real daemon); a
`cmd_view`-level test with a stubbed `run_view` callable raising
`FirstFrameTimeout` directly (matching the existing test pattern for
`NoDisplayAvailable`).

**Documentation updates**: None per-ticket — covered by the sprint's
already-approved `design/view-DESIGN.md`/`design/cli-DESIGN.md`
overlay content, applied to the canonical docs at sprint close.
