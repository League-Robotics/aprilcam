---
id: '002'
title: 'Extend proto: StreamMessage wrapper and PublishOverlay RPC'
status: done
use-cases:
  - SUC-002
  - SUC-003
  - SUC-004
depends-on: []
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Extend proto: StreamMessage wrapper and PublishOverlay RPC

## Description

Add the overlay message hierarchy and `PublishOverlay` RPC to `proto/aprilcam.proto`.
This is a pure proto change — no Python code changes in this ticket. The generated
bindings are regenerated in ticket 003.

The new messages enable multiplexing `TagFrame` and `OverlayFrame` on the same tag
stream socket, and allow any gRPC caller to push overlays to all stream subscribers.

## Acceptance Criteria

- [x] `OverlayElement` message added with fields: `type` (string), `params` (repeated
      float), `color` (repeated int32), `thickness` (int32).
- [x] `OverlayFrame` message added with fields: `timestamp` (double), `ttl` (float),
      `elements` (repeated OverlayElement), `camera_id` (string).
- [x] `StreamMessage` message added as oneof wrapper with fields: `tag_frame`
      (TagFrame, field 1) and `overlay` (OverlayFrame, field 2).
- [x] `PublishOverlayRequest` message added with fields: `cam_name` (string),
      `overlay` (OverlayFrame).
- [x] `PublishOverlay` RPC added to the `AprilCam` service:
      `rpc PublishOverlay (PublishOverlayRequest) returns (StatusReply);`
- [x] `StatusReply` message exists (add if not already present) with at least
      `ok` (bool) field.
- [x] Field numbers do not conflict with existing message fields.
- [ ] Proto compiles without errors (verified in ticket 003).

## Implementation Plan

### Approach

1. Read the current `proto/aprilcam.proto` to understand existing field numbers
   and message layout.
2. Add `OverlayElement`, `OverlayFrame`, `StreamMessage`, `PublishOverlayRequest`
   messages after the existing message definitions.
3. Add `PublishOverlay` RPC to the `AprilCam` service block.
4. Add `StatusReply` message if not already present.
5. Verify no field number conflicts within each message.

### Files to Modify

- `proto/aprilcam.proto` — add messages and RPC

### Testing Plan

- Compilation verified in ticket 003 (regenerate bindings).
- No runtime test needed for this ticket alone — proto is not executable without
  generated bindings.

### Documentation Updates

None — proto file is self-documenting via gRPC Server Reflection.
