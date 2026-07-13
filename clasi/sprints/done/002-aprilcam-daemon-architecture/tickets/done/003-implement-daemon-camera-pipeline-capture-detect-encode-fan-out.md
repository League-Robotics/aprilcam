---
id: '003'
title: Implement daemon camera pipeline (capture, detect, encode, fan-out)
status: done
use-cases:
- SUC-002
- SUC-003
depends-on:
- '002'
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement daemon camera pipeline (capture, detect, encode, fan-out)

## Description

Create `src/aprilcam/daemon/camera_pipeline.py`. This module owns one physical
camera per instance — it calls `cv.VideoCapture`, runs `AprilCam.process_frame`
on each frame, JPEG-encodes the result, and fans out the encoded `FrameMessage`
to all connected subscriber queues. It is the only code path in the entire
codebase that opens a camera after this sprint.

Reuse without modification: `AprilCam.process_frame`, `PlayfieldBoundary`,
`RingBuffer`, and `load_calibration_for_camera`.

## Acceptance Criteria

- [x] `src/aprilcam/daemon/camera_pipeline.py` created with `CameraPipeline` class.
- [x] `CameraPipeline.__init__(cam_name, index, config)` sets up state; does
  not open the camera yet.
- [x] `CameraPipeline.start()` opens `cv.VideoCapture(index)`, starts the
  capture thread, writes `info.json` to `<data_dir>/<cam_name>/`.
- [x] `CameraPipeline.stop()` signals the capture thread to exit, releases
  the `VideoCapture`, removes the data socket.
- [x] `CameraPipeline.add_subscriber(queue)` registers a
  `queue.Queue(maxsize=2)`; frame delivery uses `put_nowait` with silent drop
  on `queue.Full`.
- [x] `CameraPipeline.remove_subscriber(queue)` deregisters safely (no error
  if queue not found).
- [x] `info.json` written to `<data_dir>/<cam_name>/info.json` at pipeline
  start, containing: `data_socket` (str path), `paths_file` (str path),
  `device_name` (str), `homography` (list or null), `calibrated` (bool),
  `frame_size` (list [w, h]).
- [x] `paths_file` field in `info.json` points to
  `<data_dir>/<cam_name>/paths.json` (file need not exist yet).
- [x] The capture thread runs `AprilCam.process_frame` on each raw frame and
  uses `encode_frame` from `daemon.protocol` before enqueuing.
- [x] Slow-subscriber drop: if a subscriber queue is full, the frame is
  dropped for that subscriber only; the capture loop continues.
- [x] `CameraPipeline.capture_frame() -> bytes` returns the raw (not JPEG)
  frame bytes for the most recently captured frame (used by calibration CLI
  via daemon RPC).

## Implementation Plan

### Approach

One `threading.Thread` per camera running a `while not self._stop_event.is_set()`
loop. Per iteration: `ret, frame = cap.read()` → process → JPEG encode →
fan out to all subscriber queues via `put_nowait`. Use `threading.Lock` to
protect the subscriber list.

### Files to Create

- `src/aprilcam/daemon/camera_pipeline.py`

### Files to Modify

None (other than what is already created by T001-T002).

### Notes

- `AprilCam` is constructed per pipeline with `family`, `proc_width`,
  `use_clahe`, `use_sharpen` from `Config` or with defaults.
- JPEG encode: `cv.imencode(".jpg", frame, [cv.IMWRITE_JPEG_QUALITY, 85])`.
- `info.json` is written atomically (write temp file, rename).
- `RingBuffer` lives inside `CameraPipeline` (default 300 frames).
- The data socket (for subscribers to connect) is created and managed by
  `daemon.server`, not by `CameraPipeline` itself. `CameraPipeline` receives
  an `add_subscriber` / `remove_subscriber` interface.

### Testing Plan

Covered by T009. Key test: construct a `CameraPipeline` with a mock
`VideoCapture` (or a loopback video file), add a subscriber queue, call
`start()`, drain a few frames, call `stop()`, verify the subscriber received
valid `FrameMessage` objects.

### Documentation Updates

Docstrings on `CameraPipeline` and its public methods.
