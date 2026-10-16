---
id: '003'
title: Propagate stream subscribe errors to the client
status: open
use-cases: [SUC-004]
depends-on: []
github-issue: ''
issue: stream-subscribe-errors-are-lost-so-playfield-view-reports-no-frame-instead-of-not-calibrated.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Propagate stream subscribe errors to the client

## Description

`daemon/stream_transport.py`'s `_Listener._sender_loop` catches a failed
subscribe, logs it, and drops the connection without telling the client,
so `aprilcam playfield view` on an uncalibrated camera times out and
prints the FirstFrameTimeout "capture session may be wedged" text instead
of the UC-18.S-2 "not calibrated" pointer (issue
`stream-subscribe-errors-are-lost-so-playfield-view-reports-no-frame-instead-of-not-calibrated.md`).

Fix: on subscribe failure, send one typed error frame (kind + message)
before `_drop(conn)`, still logging the exception; send failures are
ignored (best effort). Add the wire message (e.g. `StreamError`) to
`proto/aprilcam_v2.proto`, regenerate `proto/aprilcam_v2_pb2*`, and decide
and document in the transport and `client/streams.py` docstrings how an
error frame is told apart from the `GetFrameResponse`/tag data frames on
the same channel while leaving successful streams byte-identical.
Client side: `SocketImageStream.__iter__` (and the tag stream) decode the
error frame and raise the matching `aprilcam.errors` class
(`NotCalibrated`, `UnknownCamera`, `CameraNotPresent`; a generic
`AprilCamError` for anything else). `view/viewer.py`'s `StreamLoop`
already relays reader-thread exceptions to `run()`; verify the typed error
reaches `cli/playfield.py`'s existing `except NotCalibrated` before the
first-frame timeout path, and that `cli/camera.py` behaves likewise.
`client/streams.py` stays cv2-free.

## Acceptance Criteria

- [ ] A failing subscribe makes the transport send a typed error frame,
      then close; the exception is still logged.
- [ ] Iterating a client stream that receives an error frame raises the
      matching typed `AprilCamError`, not a clean end of stream.
- [ ] Successful streams are byte-identical to today (existing stream
      tests pass unchanged).
- [ ] `aprilcam playfield view` against a camera whose stream raises
      `NotCalibrated` prints the "not calibrated" message and not the
      "no frame received" text; non-zero exit.
- [ ] Transport and streams docstrings and the relevant DESIGN notes
      record the error-frame encoding.

## Implementation Plan

- Files to modify: `proto/aprilcam_v2.proto` and generated
  `src/aprilcam/proto/aprilcam_v2_pb2*.py(i)`,
  `src/aprilcam/daemon/stream_transport.py`,
  `src/aprilcam/client/streams.py`; check `view/viewer.py` and
  `cli/playfield.py` only if the typed error does not already reach the
  handler.
- Depends on no other ticket (ticket 001's viewer work is independent).

## Testing

- **Existing tests to run**: `tests/aprilcam/client/test_streams.py`, the
  stream-transport daemon tests, `tests/aprilcam/cli/test_playfield_cli.py`,
  `tests/aprilcam/cli/test_camera_cli.py`.
- **New tests to write** (hardware-free): in `tests/aprilcam/client/`,
  a loopback socket pair where a fake sender writes an error frame and
  the stream raises `NotCalibrated` / `UnknownCamera` /
  `CameraNotPresent`; a daemon-side test that a `_subscribe` raising
  `NotCalibrated` sends the error frame before closing; in
  `tests/aprilcam/cli/test_playfield_cli.py`, a fake daemon whose
  `stream_images` (or iteration) raises `NotCalibrated` produces
  "not calibrated" output and not the FirstFrameTimeout text.
- **Verification command**: `uv run pytest tests/aprilcam/client tests/aprilcam/cli/test_playfield_cli.py`
