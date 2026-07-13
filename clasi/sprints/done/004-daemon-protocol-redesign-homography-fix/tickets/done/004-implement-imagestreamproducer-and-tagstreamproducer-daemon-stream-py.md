---
id: '004'
title: Implement ImageStreamProducer and TagStreamProducer (daemon/stream.py)
status: done
use-cases:
  - SUC-003
  - SUC-004
depends-on:
  - '002'
  - '003'
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement ImageStreamProducer and TagStreamProducer (daemon/stream.py)

## Description

Create `src/aprilcam/daemon/stream.py` with two producer classes that own all
daemon-side stream socket I/O. These classes replace the subscriber-queue fan-out
pattern in `CameraPipeline`.

`ImageStreamProducer` creates and owns a server socket (Unix, TCP, or both depending
on daemon config). On each call to `publish()`, it serializes an `ImageFrame` protobuf
message with a 4-byte big-endian length prefix and writes it to all connected clients.

`TagStreamProducer` creates and owns a server socket for the tag stream. It implements
adaptive publish: on each call to `publish_if_changed(tag_frame)`, it compares the new
tag positions against the last published set. It publishes immediately if any tag moved
more than `change_threshold_px` (default 8) or any tag entered/left the scene, subject
to a `max_hz` rate cap. It also fires `force_publish()` on a 1-second heartbeat timer
even when nothing has moved.

Both producers run an accept-loop thread that accepts new subscriber connections and
spawns a sender thread per connection.

Also update `CameraPipeline` to construct and call producers instead of building
`FrameMessage` objects and pushing to subscriber queues.

## Acceptance Criteria

- [ ] `ImageStreamProducer(cam_name, config).start()` creates the stream socket(s)
      and returns a `StreamEndpoint`.
- [ ] `ImageStreamProducer.publish(frame_id, ts_mono_ns, ts_wall_ms, jpeg, w, h)`
      sends a length-prefixed `ImageFrame` protobuf to all connected clients.
- [ ] `ImageStreamProducer.stop()` closes the socket and cleans up.
- [ ] `TagStreamProducer(cam_name, config, max_hz, change_threshold_px).start()`
      returns a `StreamEndpoint`.
- [ ] `TagStreamProducer.publish_if_changed(tag_frame)` publishes when change threshold
      is exceeded, rate-limited by `max_hz`.
- [ ] `TagStreamProducer.force_publish(tag_frame)` always publishes (used for heartbeat).
- [ ] Heartbeat fires every 1 second when no change has been published.
- [ ] `CameraPipeline` calls producers instead of using subscriber queues.
- [ ] `CameraPipeline.add_subscriber()` and `remove_subscriber()` methods are removed.

## Implementation Plan

### Approach

1. Create `src/aprilcam/daemon/stream.py` with `ImageStreamProducer` and
   `TagStreamProducer`.
2. Each producer:
   - `start()`: create listening socket(s) (Unix at `<socket_dir>/<cam_name>/images-<uuid>.sock`
     and/or TCP at a dynamic OS-assigned port). Start accept-loop thread. Return
     `StreamEndpoint`.
   - Accept-loop: for each new connection, spawn a sender thread that reads from a
     per-connection queue.
   - `publish()` / `publish_if_changed()`: serialize protobuf, write to all active
     connection queues (drop silently if queue full, maxsize=2).
   - `stop()`: set stop event, close listening socket, join threads.
3. `TagStreamProducer` tracks last published tag positions (`dict[tag_id → (cx, cy)]`).
   Change detection: any tag id added/removed, or any tag moved > `change_threshold_px`.
   Rate limiter: `time.monotonic() - last_publish_ts >= 1.0 / max_hz`.
   Heartbeat: a background timer thread calls `force_publish()` every 1 second.
4. Update `CameraPipeline`:
   - Accept `image_producer: ImageStreamProducer` and `tag_producer: TagStreamProducer`
     as constructor arguments (or set via `set_producers()`).
   - In the capture loop, after JPEG encoding, call `image_producer.publish(...)`.
   - After tag detection, call `tag_producer.publish_if_changed(tag_frame)`.
   - Remove `_subscribers`, `add_subscriber()`, `remove_subscriber()`, subscriber
     queue fan-out code.

### Files to Create / Modify

- `src/aprilcam/daemon/stream.py` — new
- `src/aprilcam/daemon/camera_pipeline.py` — remove subscriber queues, add producer calls
- `src/aprilcam/daemon/protocol.py` — may remove `FrameMessage` dataclass; keep framing helpers

### Testing Plan

- Write `tests/test_daemon_stream.py`:
  - Start an `ImageStreamProducer` with Unix-socket-only config; connect a raw
    client socket; call `publish()`; read the length-prefixed response; parse as
    `ImageFrame` protobuf; verify fields.
  - `TagStreamProducer`: publish a tag frame where all tags are stationary; verify
    no publish in < 1s; verify heartbeat fires after 1s.
  - `TagStreamProducer`: move a tag > 8 px; verify immediate publish.
- Run `uv run pytest tests/test_daemon_stream.py`.
