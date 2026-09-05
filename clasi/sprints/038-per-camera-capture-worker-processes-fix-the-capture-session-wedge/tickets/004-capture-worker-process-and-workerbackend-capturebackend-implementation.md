---
id: '004'
title: capture_worker process and WorkerBackend (CaptureBackend implementation)
status: open
use-cases:
- SUC-001
- SUC-002
depends-on:
- '003'
github-issue: ''
issue: camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# capture_worker process and WorkerBackend (CaptureBackend implementation)

## Description

Build the two pieces that use ticket 003's IPC primitives to actually
fix the wedge:

1. `capture_worker.py` (`python -m aprilcam.camera.capture_worker`) — a
   standalone child-process entry point that owns one real
   `OpenCVBackend`/`cv2.VideoCapture`, publishes frames into the
   shared-memory ring on every successful capture, and answers control-
   channel requests (`get_property`/`set_property`/`settable_names`/
   `frame_size`) against its own capture object. It also issues the
   settle-after-open reads `Camera.apply_config` already relies on one
   layer up (`camera/DESIGN.md` §3's "Settle-after-open" constraint) —
   the worker does this itself since the real `cv2.VideoCapture` only
   exists inside it.
2. `WorkerBackend` (a `CaptureBackend` implementing `is_open`/`open`/
   `read`/`close`/`frame_size`, plus the `ControlBackend`-shaped
   `get_property`/`set_property`/`settable_names` surface, duck-typed
   the same way `OpenCVBackend` already provides both) — `open()`
   spawns a fresh `capture_worker` child process and waits for its
   first heartbeat; `close()` terminates it (SIGTERM, then SIGKILL
   after a bounded grace period) and detaches the ring; `read()` copies
   out the latest frame from the ring or returns `None` if none is
   fresh or the child is no longer alive (never raises — a worker
   crash is an ordinary read failure, not an exception escaping into
   `Camera`).

See the sprint's `design/camera-DESIGN.md` overlay §3/§4 for the full
constraints (process-local wedge root cause, why `Camera` needs zero
change to its self-heal path, the component diagram) and Design
Rationale for rejected alternatives (wrapping every backend, a pipe
instead of shared memory).

## Acceptance Criteria

- [ ] `capture_worker.main()` runs as a real subprocess in at least one
      test, using a fake/synthetic frame source injected in place of a
      real `cv2.VideoCapture` (per sprint.md's Test Strategy: no real
      camera hardware in this ticket's tests).
- [ ] `WorkerBackend.open()` spawns the child and blocks until its
      first heartbeat (or a bounded startup timeout is exceeded, in
      which case `open()` raises — matching `CaptureBackend.open()`'s
      "raises on failure, never a sentinel" contract).
- [ ] `WorkerBackend.read()` returns real frames once the child is
      running and publishing, and returns `None` (never raises) once
      the child process has been killed out-of-band (simulating a
      crash) or its heartbeat has gone stale.
- [ ] `WorkerBackend.close()` terminates the child (verify via process
      exit, not just "we sent a signal") and leaves no leaked shared-
      memory segment or zombie process.
- [ ] `WorkerBackend.get_property`/`set_property`/`settable_names`/
      `frame_size` round-trip correctly through the control channel
      against the worker's fake capture source.
- [ ] A test with two independent fake workers confirms one worker's
      crash/kill does not affect the other's frames or control-channel
      responses (SUC-002's multi-camera independence criterion).
- [ ] `WorkerBackend` is a real subclass of `CaptureBackend` (no
      `typing.Protocol`, per this project's interfaces rule) and passes
      the same generic backend-contract tests (if any exist) that
      `OpenCVBackend`/`V4l2LoopbackBackend` already pass.
- [ ] Exactly one test in this ticket may be `@pytest.mark.camera`-marked
      against a real capture device, and it must skip cleanly with no
      hardware present (sprint.md Test Strategy).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/ -m "not camera and not integration"`
- **New tests to write**: worker-process spawn/publish/read round-trip
  with a fake frame source; kill-out-of-band-then-read-returns-None;
  control-channel property get/set round-trip; two-worker independence;
  clean shutdown/no-leak-on-close.
- **Verification command**: `uv run pytest tests/aprilcam/camera/backends/ -m "not camera and not integration"`

## Implementation Plan

**Approach**: `capture_worker.py`'s `main()` parses its invocation
arguments (camera index, ring/control-channel handles or names to
attach to), constructs a real `OpenCVBackend` (or an injectable fake
for tests, via an internal seam — do not require monkeypatching
`cv2.VideoCapture` itself), and loops: read a frame, publish it into
the ring (advancing the sequence number last), check for a pending
control-channel request and answer it, repeat. `WorkerBackend` wraps
`multiprocessing.Process`/`subprocess.Popen` (whichever fits the
IPC primitives ticket 003 built — `multiprocessing.shared_memory` is
usable from either) to spawn `python -m aprilcam.camera.capture_worker`
with appropriate arguments, and implements the `CaptureBackend`/
`ControlBackend`-shaped surface as a thin client over ticket 003's ring
and control channel.

**Files to create**:
- `src/aprilcam/camera/capture_worker.py`
- `src/aprilcam/camera/backends/worker_backend.py`

**Testing plan**: New tests under `tests/aprilcam/camera/backends/`
spawning real subprocesses running `capture_worker.main()` against an
injected fake frame source (in-process thread or subprocess running a
synthetic source, per sprint.md's Test Strategy) — never a real camera,
except the one explicitly hardware-marked, cleanly-skipping test this
ticket's acceptance criteria allow.

**Documentation updates**: None per-ticket — covered by the sprint's
already-approved `design/camera-DESIGN.md` overlay content, applied to
the canonical doc at sprint close.
