---
id: '003'
title: Shared-memory frame ring and control-channel IPC protocol
status: open
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Shared-memory frame ring and control-channel IPC protocol

## Description

Foundational ticket for the per-camera capture worker process fix
(root cause: `clasi/issues/camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md`'s
"Root cause established 2026-09-04" section — the AVFoundation
capture-session wedge is process-local; only a process boundary
clears it). This ticket builds the two IPC primitives the worker
architecture needs, **without** the worker process or `WorkerBackend`
itself (that's ticket 004) — a self-contained, hardware-free piece
that can be fully unit-tested against two threads/processes in the
same test, no camera, no subprocess module dependency on the rest of
`camera/` yet:

1. A shared-memory frame ring (`multiprocessing.shared_memory`): a
   short ring of raw-frame slots sized for a camera's resolution
   (~6 MB per 1080p BGR frame), with a fixed-layout header carrying the
   current write-slot index, a monotonically increasing sequence
   number, a capture timestamp, and the frame's shape/dtype. One
   writer, one reader. The reader locates and copies out (once, no
   double-copy) the newest complete frame named by the header; a
   sequence number that hasn't advanced since the last read means "no
   fresh frame yet."
2. A control channel (pipe or local socket) carrying small, synchronous
   request/response messages for `get_property`/`set_property`/
   `settable_names`/`frame_size`.

See the sprint's `design/camera-DESIGN.md` overlay §4 ("Per-camera
capture worker process (sprint 038)") for the full protocol
description and the component diagram, and its Design Rationale for
why shared memory was chosen over a pipe for frame transport.

## Acceptance Criteria

- [ ] A frame-ring writer can publish a sequence of frames (varying
      sizes/dtypes across test cases, at least one matching a real
      1080p BGR shape) into the shared-memory segment; a reader in a
      different process (or thread, if that's sufficient to exercise
      the real code path — prefer a real second process for at least
      one test given the eventual real usage is cross-process) sees
      each new frame exactly once per sequence advance and gets a
      "no new frame" signal (not a stale copy, not a crash) when
      nothing new has been published.
- [ ] The ring never returns a torn/partially-written frame to a
      reader — the header's sequence-number-advances-last discipline is
      verified by a test that reads concurrently with writes.
- [ ] The control channel supports `get_property`/`set_property`/
      `settable_names`/`frame_size` request/response round-trips with a
      fake handler on the other end (no real camera).
- [ ] Shared-memory segments are named/sized correctly and are cleanly
      unlinked/closed on both ends — a test confirms no leaked shared-
      memory segment survives a normal open/close cycle (this matters
      because leaked `/dev/shm`-style segments are a real, observable
      OS resource, unlike an in-process object).
- [ ] No dependency on a real `cv2.VideoCapture` or real camera
      hardware anywhere in this ticket's tests.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/ -m "not camera and not integration"` (confirm no regression to existing backends)
- **New tests to write**: ring publish/read-latest/no-new-frame
  round-trip (cross-process where practical); concurrent
  writer-while-reading test for the no-torn-frame guarantee; control-
  channel request/response round-trip against a fake handler; shared-
  memory cleanup/no-leak test.
- **Verification command**: `uv run pytest tests/aprilcam/camera/backends/ -m "not camera and not integration"`

## Implementation Plan

**Approach**: Implement the ring and control-channel protocol as
small, independently testable modules under `camera/backends/` (e.g. a
`_worker_ipc.py` or similar internal helper, named at implementation
time) that ticket 004's `WorkerBackend`/`capture_worker.py` will import
on both the parent and child side. Keep the header format and slot
count as named constants with a documented rationale (ring depth,
frame size assumptions). Do not implement process spawning or
`CaptureBackend` conformance here — that is ticket 004's scope
entirely; this ticket's deliverable is the wire format and the
publish/read/no-new-frame semantics, tested with both ends driven
directly by test code (a fake writer, a fake reader) rather than a
real worker process.

**Files to create**:
- `src/aprilcam/camera/backends/` — new internal module(s) for the
  shared-memory ring and control-channel protocol (naming decided at
  implementation time, consistent with this package's existing
  `backends/*.py` one-small-module-per-concern convention).

**Testing plan**: New unit tests under `tests/aprilcam/camera/backends/`,
using real `multiprocessing.shared_memory`/pipes but fake/synthetic
data — no real camera, no real `capture_worker.py` process (that
arrives in ticket 004).

**Documentation updates**: None per-ticket — covered by the sprint's
already-approved `design/camera-DESIGN.md` overlay content, applied to
the canonical doc at sprint close.
