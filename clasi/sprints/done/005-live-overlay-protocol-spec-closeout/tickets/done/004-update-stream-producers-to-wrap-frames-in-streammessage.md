---
id: '004'
title: Update stream producers to wrap frames in StreamMessage
status: done
use-cases:
  - SUC-002
  - SUC-004
depends-on:
  - '003'
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update stream producers to wrap frames in StreamMessage

## Description

Change `TagStreamProducer` in `src/aprilcam/daemon/stream.py` to:
1. Wrap every published `TagFrame` in a `StreamMessage` oneof before serializing.
2. Add a `publish_overlay(overlay_frame: OverlayFrame)` method that wraps an
   `OverlayFrame` in `StreamMessage` and broadcasts it immediately, bypassing
   rate limiting and change detection.

This is the daemon-side half of the wire format change. After this ticket, the
tag stream socket carries `StreamMessage` bytes. Ticket 005 updates the consumer
side to match.

## Acceptance Criteria

- [x] `TagStreamProducer` serializes `StreamMessage(tag_frame=tag_frame)` instead
      of bare `TagFrame` bytes.
- [x] `TagStreamProducer.publish_overlay(overlay_frame)` exists and serializes
      `StreamMessage(overlay=overlay_frame)` to all subscribers immediately.
- [x] `publish_overlay()` does not go through change detection or rate limiting.
- [x] Existing `publish_if_changed()` and `force_publish()` behavior is unchanged
      (same logic, just wraps in StreamMessage).
- [x] Import smoke test passes: `uv run python -c "from aprilcam.daemon.stream import TagStreamProducer; print('ok')"`.
- [x] `uv run pytest tests/` passes.

## Implementation Plan

### Approach

1. Read `src/aprilcam/daemon/stream.py` to understand the current serialization
   path (find the `_frame_bytes` or equivalent helper, or inline serialization).
2. Change the serialization to wrap in `StreamMessage`:
   ```python
   from aprilcam.proto import aprilcam_pb2
   def _to_stream_bytes(proto_msg):
       if isinstance(proto_msg, aprilcam_pb2.TagFrame):
           wrapper = aprilcam_pb2.StreamMessage(tag_frame=proto_msg)
       elif isinstance(proto_msg, aprilcam_pb2.OverlayFrame):
           wrapper = aprilcam_pb2.StreamMessage(overlay=proto_msg)
       else:
           raise TypeError(f"Unsupported message type: {type(proto_msg)}")
       return wrapper.SerializeToString()
   ```
3. Add `publish_overlay(overlay_frame: OverlayFrame)` method that calls the
   existing broadcast path with `_to_stream_bytes(overlay_frame)`.

### Files to Modify

- `src/aprilcam/daemon/stream.py`

### Testing Plan

- Import smoke: `uv run python -c "from aprilcam.daemon.stream import TagStreamProducer; print('ok')"`
- `uv run pytest tests/`
- Note: end-to-end socket test requires a running daemon; this is out of scope
  for automated tests. Manual verification in ticket 008.

### Documentation Updates

None.
