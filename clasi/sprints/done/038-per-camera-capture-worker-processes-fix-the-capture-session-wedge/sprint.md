---
id: 038
title: Per-camera capture worker processes (fix the capture-session wedge)
status: done
branch: sprint/038-per-camera-capture-worker-processes-fix-the-capture-session-wedge
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
issues:
- camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 038: Per-camera capture worker processes (fix the capture-session wedge)

## Goals

Fix the process-local AVFoundation capture-session wedge
(`clasi/issues/camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md`)
by giving every opened camera its own capture worker process, so that a
wedged capture session can be cleared by killing and respawning a worker
instead of restarting the whole daemon. Root cause was established
2026-09-04 (see the issue's final section): the wedge is process-local —
a fresh process reads the same camera index fine at the exact moment the
daemon cannot, and `release()` does not fully tear down AVFoundation's
capture-session state within a process. No in-process reopen strategy
can clear it; only a process boundary does. Also close the two
gaps identified in the original write-up that are cheap and
independently valuable regardless of the worker-process fix: `usable`
should mean "delivers a frame," and `aprilcam camera view` must fail
with a diagnostic after a first-frame timeout instead of hanging
forever.

## Problem

`Camera` in `src/aprilcam/camera/camera.py` drives one backend
(`CaptureBackend` from `src/aprilcam/camera/backends/__init__.py`)
in-process under a read lock, with `_attempt_self_heal` doing
close/open cycles bounded by `MAX_REOPENS`/`FAILURE_THRESHOLD`. On
camera 2 (`hd-usb-camera`, 1920x1080) that self-heal loop exhausted ten
reopens in a minute — every reopen reported `isOpened() == True` but
never delivered a frame — and the daemon marked the camera `unusable`
while a brand-new `cv2.VideoCapture(2, cv2.CAP_AVFOUNDATION)` in a
fresh process read frames immediately. Because the wedge lives in
per-process AVFoundation session state, no fix that stays inside the
daemon process (delays, backoff, fresh backend objects, re-resolved
index) can work. Compounding this, `usable` is set by
`camera/discovery.py::_probe_openable()` from open()+close() alone, so
a wedged camera still reports healthy, and `aprilcam camera view` has
no bound on waiting for a first frame, so an operator sees a silent
hang rather than a diagnosable error.

## Solution

Move each camera's `cv2.VideoCapture` (or platform-equivalent) out of
the daemon process and into a small dedicated worker process
(`python -m aprilcam.camera.capture_worker --index N ...`) that the
daemon spawns per opened camera. The worker owns the capture session
and publishes frames to the daemon over shared memory (a short ring of
full frames — roughly 6 MB each at 1080p, so a pipe is the wrong
transport) plus a control channel for `get_property`/`set_property`/
`settable_names`/`frame_size` and settle reads. A new `WorkerBackend`
implements the existing `CaptureBackend` base class
(`open/read/close/is_open/frame_size/get_property/set_property/
settable_names/describe_set_failure`) so `Camera` and
`_camera_for` in `src/aprilcam/daemon/service.py` see no interface
change; self-heal becomes kill-and-respawn of the worker process,
reusing the existing failure-counting and reopen-budget logic rather
than replacing it. TCC camera access is inherited by children of the
daemon process, so a worker gets the same grant. Alongside the worker
architecture: make `usable` require a successful probe frame read
(with the added probe latency accepted as the cost), and give
`aprilcam camera view` a first-frame timeout that fails with a
diagnostic naming the camera and the likely cause instead of hanging.

## Success Criteria

- A camera whose capture session wedges (reads return `None`
  indefinitely while `isOpened()` still reports true) recovers via
  worker respawn without a full daemon restart and without disrupting
  other cameras' streams.
- A worker crash does not take down the daemon or other cameras.
- `camera probe` / `usable` requires a real frame read, not just
  open()+close().
- `aprilcam camera view` fails within a bounded timeout with a clear
  diagnostic (names the camera, suggests the likely cause) instead of
  hanging forever when no frame arrives.
- The macOS `control_backend` (uvc-util) path and non-macOS backends
  keep working unchanged through the same `CaptureBackend` interface.

## Scope

### In Scope

- A capture worker process module (`aprilcam.camera.capture_worker` or
  similar) that owns one `cv2.VideoCapture`/backend instance and can be
  invoked standalone (`python -m ...`).
- A shared-memory (or equivalent low-latency, multi-MB-per-frame)
  frame-publishing transport from worker to daemon, plus a control
  channel for property get/set, `settable_names`, `frame_size`, and
  settle reads.
- A `WorkerBackend` implementing the existing `CaptureBackend` base
  class in `src/aprilcam/camera/backends/__init__.py`, wired in via
  `resolve_capture_backend(...)` / `_camera_for` in
  `src/aprilcam/daemon/service.py`.
- Reworking `Camera._attempt_self_heal` in
  `src/aprilcam/camera/camera.py` to kill-and-respawn the worker
  process on repeated read failure, preserving `MAX_REOPENS` /
  `FAILURE_THRESHOLD` semantics.
- `camera/discovery.py::_probe_openable()` requiring a successful frame
  read (not just open()+close()) before reporting `usable`.
- A first-frame timeout in `aprilcam camera view` that fails loudly
  with a diagnostic instead of hanging.
- Tests covering worker lifecycle (spawn, respawn on failure, clean
  shutdown) without requiring real camera hardware — any
  hardware-dependent test must be hardware-marked and skip cleanly
  when no camera is present, since agents may not open real cameras.

### Out of Scope

- Changing the detection pipeline (AprilTag/ArUco processing) itself.
- Changing the RPC/proto surface between daemon and MCP
  server/clients — the worker boundary is internal to the daemon.
- Linux/Raspberry Pi capture backends, beyond keeping them working
  through the same `CaptureBackend` interface (no worker-process
  rework is required for backends that don't exhibit this wedge).
- The separate, related issue
  `clasi/issues/later/daemon-start-should-fail-loudly-without-capture-backend.md`
  (daemon starting with no capture backend at all) — distinct failure
  mode, not addressed here.
- Root-causing whether the wedge correlates with UVC control writes
  during an active stream (noted as an open question in the issue);
  this sprint fixes the wedge structurally via process isolation
  rather than chasing that correlation.
- Starting, stopping, or restarting the real daemon, or opening real
  cameras, during planning or implementation — per project rules,
  agents never do this; hardware-dependent verification is the
  stakeholder's to run manually.

## Test Strategy

Unit/integration tests exercise `WorkerBackend` and the worker
lifecycle (spawn, respond to control-channel calls, simulate a stalled
read and confirm kill-and-respawn, clean shutdown) using a fake/stub
capture source in the worker rather than a real camera. Any test that
needs a real `cv2.VideoCapture` or physical device is hardware-marked
and skips cleanly in the offline gate (`-m "not camera and not
integration"`). `usable`/probe changes get a test asserting a
frame-read failure flips `usable` to false. The `camera view`
timeout gets a test with a stub subscriber that never delivers a frame,
asserting the CLI exits with a diagnostic within the bound rather than
hanging. Full suite runs once at `close_sprint` per project convention.

## Architecture

**Substantial** — introduces a new cross-process IPC subsystem (a
per-camera capture worker process plus a shared-memory frame ring and
control channel), a new `CaptureBackend` implementation
(`WorkerBackend`), and touches four subsystems (`camera/`, `daemon/`,
`view/`, `cli/`) with a new daemon→subprocess cross-module dependency
that does not exist today. This project has opted into the persistent
per-subsystem design-doc set (`design_docs_opt_in: true`), so this
sprint's architecture content lives in the sprint's `design/` overlay
(`clasi/sprints/038-.../design/`) as full edited copies of the affected
canonical docs, not in this section — see each `<name>.diff.md` for the
reviewable diff. Affected canonical docs: `docs/design/design.md`
(system process/deployment view gains a new runtime piece),
`src/aprilcam/camera/DESIGN.md` (the worker backend, IPC protocol,
self-heal-as-respawn, probe-reads-a-frame), `src/aprilcam/daemon/DESIGN.md`
(backend selection wiring, stall-detection watchdog extension),
`src/aprilcam/view/DESIGN.md` (`StreamLoop` first-frame timeout), and
`src/aprilcam/cli/DESIGN.md` (`camera view`'s diagnostic on that
timeout). `client/` was considered but is **not** touched — see the
camera-DESIGN.md overlay's Design Rationale for why the first-frame
bound lives entirely in `view/`'s existing polling loop rather than as
a new socket-level timeout in `client/streams.py`.

### Architecture Overview

See the `design/` overlay — `design.md`, `camera-DESIGN.md`,
`daemon-DESIGN.md`, `view-DESIGN.md`, `cli-DESIGN.md` — for the full
write-up (component diagram, module responsibilities, IPC protocol,
migration concerns).

### Design Rationale

See each overlay doc's own Design Rationale / "Rejected alternatives"
subsections — notably `camera-DESIGN.md`'s rationale for why
`Camera._attempt_self_heal` needs **no code change at all** (self-heal
already only calls the backend's uniform `close()`/`open()`, and
`WorkerBackend` implements kill-and-respawn entirely inside those two
methods), and why the first-frame timeout lives in `view/viewer.py`'s
`StreamLoop` rather than as a new `client/streams.py` socket timeout.

### Migration Concerns

None — no data model change, no on-disk schema change, no wire/RPC
change. `WorkerBackend` is selected by config/env (default stays
`OpenCVBackend` until explicitly enabled per the ticket sequence, so a
mid-sprint partial merge cannot change runtime camera behavior). See
`camera-DESIGN.md`'s Migration Concerns for the full statement.

## Use Cases

### SUC-001: A wedged capture session recovers without a daemon restart
Parent: UC-25 (camera health/probing)

- **Actor**: The daemon's self-heal mechanism (no human actor — this is
  an automatic recovery use case).
- **Preconditions**: A camera is held open by the daemon through a
  `WorkerBackend`; its capture worker process has stopped delivering
  frames (reads return `None`, or its heartbeat/sequence counter has
  stopped advancing) while `isOpened()`-equivalent state still reports
  true.
- **Main Flow**:
  1. `Camera._attempt_self_heal` (unchanged) or the daemon's stall
     watchdog detects the failure/stall.
  2. `WorkerBackend.close()` sends a terminate signal to the worker
     process (SIGTERM, then SIGKILL after a bounded grace period) and
     releases the shared-memory ring.
  3. `WorkerBackend.open()` spawns a fresh worker process (a new
     process, clearing whatever process-local AVFoundation state was
     wedged), re-attaches a shared-memory ring, and waits for the
     worker's first heartbeat.
  4. The camera resumes delivering frames; `MAX_REOPENS`/
     `FAILURE_THRESHOLD` budget semantics apply exactly as before.
- **Postconditions**: The camera is usable again without the daemon
  process restarting; other cameras' workers and streams are
  undisturbed throughout.
- **Acceptance Criteria**:
  - [ ] A simulated stalled worker (fake capture source that stops
        advancing its heartbeat) is killed and respawned by self-heal
        without a test needing real camera hardware.
  - [ ] Other cameras' `Camera`/`WorkerBackend` instances are
        unaffected by one camera's respawn (verified via a fake
        multi-worker test, not real hardware).
  - [ ] `MAX_REOPENS`/`FAILURE_THRESHOLD` counting is unchanged in
        `Camera` — no new counters duplicated inside `WorkerBackend`.

### SUC-002: A crashed worker process does not take the daemon down
Parent: UC-25

- **Actor**: The daemon process (resilience against a worker crash).
- **Preconditions**: A capture worker process has crashed or been
  killed externally (not by the daemon's own self-heal).
- **Main Flow**:
  1. `WorkerBackend.read()` detects the worker is no longer alive
     (process exit, or a dead heartbeat) and returns `None` rather than
     raising.
  2. `Camera`'s existing failure counting treats this as ordinary read
     failures, eventually triggering self-heal (SUC-001) exactly as if
     the worker had merely stalled.
  3. The daemon process itself, and every other held-open camera's
     worker, are completely unaffected.
- **Postconditions**: The daemon keeps serving every other camera
  normally; the crashed camera recovers via the same self-heal path as
  a stall.
- **Acceptance Criteria**:
  - [ ] A test that kills a fake worker process out-of-band (not via
        `close()`) observes `read()` returning `None`, not an
        exception escaping into `Camera`/`DaemonService`.
  - [ ] The same test confirms an unrelated second fake worker keeps
        delivering frames throughout.

### SUC-003: `usable` means "delivers a frame"
Parent: UC-25

- **Actor**: An operator or agent running `camera probe`/`camera list`.
- **Preconditions**: A camera's capture session is open
  (`isOpened()`-equivalent true) but wedged — reads never return a
  frame.
- **Main Flow**:
  1. `CameraDiscovery._probe_openable` opens the camera, attempts one
     bounded frame read (not just open-then-close), and closes it.
  2. A wedged camera's bounded read returns `None`/times out;
     `_probe_openable` reports `usable=False`.
  3. A healthy camera's read succeeds within the bound; `usable=True`,
     unchanged from before.
- **Postconditions**: `usable` in `camera probe`/`camera list` output
  is never asserted for a camera that cannot actually deliver a frame.
- **Acceptance Criteria**:
  - [ ] A fake backend whose `read()` always returns `None` after
        `open()` succeeding flips `usable` to `False` via
        `_probe_openable`, where it previously reported `True`.
  - [ ] A healthy fake backend still reports `usable=True`.
  - [ ] The added read's bounded latency is documented in
        `camera-DESIGN.md` as an accepted, explicit cost tradeoff.

### SUC-004: `camera view` fails fast with a diagnostic instead of hanging
Parent: UC-16 (raw camera view)

- **Actor**: An operator running `aprilcam camera view <camera>`
  against a camera whose stream never delivers a first frame (wedged
  session, or any other cause).
- **Preconditions**: The daemon connection succeeds and the stream
  subscription is accepted, but no frame arrives within a bounded
  first-frame timeout.
- **Main Flow**:
  1. `view/viewer.py`'s `StreamLoop.run` tracks elapsed time since
     `run()` started with no frame yet received.
  2. Once the bound elapses with `_latest` still unset, `StreamLoop`
     raises a typed, local (non-`AprilCamError`) timeout exception
     naming the camera — mirroring `NoDisplayAvailable`'s existing
     "client-local condition, not a daemon error taxonomy member"
     pattern.
  3. `run_raw_view` propagates it; `cli/camera.py`'s `cmd_view` catches
     it and prints a diagnostic naming the camera and suggesting the
     likely cause (a wedged capture session) and next step, instead of
     the window hanging open indefinitely.
- **Postconditions**: An operator gets a bounded, diagnosable failure
  (default on the order of 10s) instead of an open-ended silent hang.
- **Acceptance Criteria**:
  - [ ] A test using a stub `ImageStream` that never yields a frame
        confirms `StreamLoop.run` raises the timeout exception within
        the configured bound (a short bound injected for the test, not
        the real default).
  - [ ] `cmd_view`'s diagnostic names the camera and suggests the
        likely cause, and the process exits `EXIT_ERROR` rather than
        hanging.
  - [ ] A stream that *does* deliver a first frame within the bound is
        completely unaffected — no regression to the existing
        streaming display loop.

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | usable means delivers a frame (probe reads before reporting healthy) | — |
| 002 | camera view fails with a diagnostic on first-frame timeout instead of hanging | — |
| 003 | Shared-memory frame ring and control-channel IPC protocol | — |
| 004 | capture_worker process and WorkerBackend (CaptureBackend implementation) | 003 |
| 005 | Camera.check_stall and config/env-gated WorkerBackend selection | 004 |
| 006 | Daemon stall-detection watchdog (check_stalled_cameras) | 005 |

Tickets execute serially in the order listed. 001 and 002 are
independent quick wins (either could run before or after the others
with no dependency conflict — listed first because they are the
simplest, most isolated, most immediately valuable pieces per the
source issue's own "three gaps worth fixing, independently landable"
framing). 003-006 build the worker-process subsystem in strict
dependency order: the IPC primitives first, then the backend/worker
process that uses them, then the wiring that makes them reachable at
all (config-gated, off by default), then the proactive daemon-level
watchdog that depends on that wiring existing.
