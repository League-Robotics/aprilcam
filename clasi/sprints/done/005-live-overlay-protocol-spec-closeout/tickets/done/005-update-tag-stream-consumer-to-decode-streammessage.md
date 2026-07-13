---
id: '005'
title: Update tag stream consumer to decode StreamMessage
status: done
use-cases:
  - SUC-002
  - SUC-004
depends-on:
  - '004'
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update tag stream consumer to decode StreamMessage

## Description

Change `TagStreamConsumer.read()` in `src/aprilcam/client/stream.py` to decode
a `StreamMessage` instead of a bare `TagFrame`. The return type widens to
`TagFrame | OverlayFrame` (where `TagFrame` is the Pydantic model and
`OverlayFrame` is the proto message).

This is the client-side half of the wire format change. After this ticket, any
subscriber using `TagStreamConsumer` can receive both tag frames and overlay frames.

## Acceptance Criteria

- [x] `TagStreamConsumer.read()` parses the incoming bytes as `StreamMessage`.
- [x] When `StreamMessage.HasField("tag_frame")`, converts to Pydantic `TagFrame`
      model (existing conversion path, unchanged).
- [x] When `StreamMessage.HasField("overlay")`, returns the `OverlayFrame` proto
      message directly.
- [x] Return type annotation is `TagFrame | OverlayFrame` (or equivalent union).
- [x] `__iter__` is updated consistently if it delegates to `read()`.
- [x] Import smoke: `uv run python -c "from aprilcam.client.stream import TagStreamConsumer; print('ok')"`.
- [x] `uv run pytest tests/` passes.

## Implementation Plan

### Approach

1. Read `src/aprilcam/client/stream.py` to find `TagStreamConsumer.read()`.
2. Change the deserialization:
   ```python
   from aprilcam.proto import aprilcam_pb2
   from aprilcam.client.models import TagFrame as TagFrameModel

   def read(self) -> TagFrameModel | aprilcam_pb2.OverlayFrame:
       data = self._read_frame_bytes()
       msg = aprilcam_pb2.StreamMessage()
       msg.ParseFromString(data)
       if msg.HasField("tag_frame"):
           return TagFrameModel.from_proto(msg.tag_frame)
       elif msg.HasField("overlay"):
           return msg.overlay
       else:
           raise ValueError("StreamMessage has no known payload field")
   ```
3. Update `__iter__` if it does not already delegate to `read()`.
4. Update type annotations.

### Files to Modify

- `src/aprilcam/client/stream.py`

### Testing Plan

- Import smoke: `uv run python -c "from aprilcam.client.stream import TagStreamConsumer; print('ok')"`
- `uv run pytest tests/`

### Documentation Updates

None.
