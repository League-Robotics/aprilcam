---
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 005 Use Cases

## SUC-001: Verify protocol spec implementation
Parent: Issue — aprilcam-daemon-protocol-specification

- **Actor**: Developer / team-lead
- **Preconditions**: Sprint 004 is complete; gRPC daemon implementation exists.
- **Main Flow**:
  1. Developer reads the daemon protocol specification issue.
  2. Developer checks each specified component (proto, grpc_server, stream,
     client/control, client/stream, client/models, mdns) against the actual
     source files.
  3. Any discrepancies are noted; minor ones are acceptable if the implementation
     achieves the same goals through equivalent means.
  4. Developer marks the issue done via CLASI MCP.
- **Postconditions**: Issue `aprilcam-daemon-protocol-specification` is marked done;
  backlog is clean.
- **Acceptance Criteria**:
  - [ ] All major components specified in the issue are present in the codebase.
  - [ ] Issue is marked done in CLASI.

---

## SUC-002: Robot process publishes live overlay to view
Parent: Issue — plan-live-overlay-via-tag-stream-socket

- **Actor**: Robot control process (AI agent or Python script)
- **Preconditions**: Daemon is running with at least one camera open and a tag
  stream active. `view_cli` is displaying the live view.
- **Main Flow**:
  1. Robot process creates a `DaemonControl` and calls `publish_overlay(cam_name,
     elements, ttl=0.5)` at ~10 Hz.
  2. Daemon receives the `PublishOverlay` gRPC call, looks up the
     `TagStreamProducer` for that camera, and calls `publish_overlay(overlay)`.
  3. `TagStreamProducer` wraps the `OverlayFrame` in a `StreamMessage` and
     broadcasts it to all tag stream subscribers immediately (no rate limiting).
  4. `view_cli` tag reader thread receives the `StreamMessage`, detects the
     `overlay` field, and stores it in `_latest_overlay`.
  5. The render loop reads `_latest_overlay`, checks TTL, and calls
     `display.draw_live_overlay(frame, overlay, homography)`.
  6. Overlay graphics appear on the live view within one frame.
- **Postconditions**: Arc, arrow, point, and polyline elements are drawn on the
  live view in real time; they disappear when TTL expires.
- **Acceptance Criteria**:
  - [ ] Arc elements are drawn with correct ellipse shape under non-square homography.
  - [ ] Arrow elements are drawn with arrowhead.
  - [ ] Point elements are drawn as filled circles.
  - [ ] Polyline elements are drawn as open polylines.
  - [ ] Expired overlays (time.time() - timestamp > ttl) are not drawn.
  - [ ] Unknown element types are silently skipped.
  - [ ] No overlay drawn when homography is None.

---

## SUC-003: MCP agent publishes live overlay
Parent: Issue — plan-live-overlay-via-tag-stream-socket

- **Actor**: AI agent using the MCP server
- **Preconditions**: MCP server is running; daemon has a camera open.
- **Main Flow**:
  1. Agent calls `open_camera` to get a `camera_id`.
  2. Agent calls `set_live_overlay(camera_id, elements_json, ttl=1.0)`.
  3. MCP server parses `elements_json`, builds `OverlayFrame`, calls
     `DaemonControl.publish_overlay()`.
  4. Overlay appears in the live view.
  5. Agent calls `clear_live_overlay(camera_id)` to remove the overlay immediately.
- **Postconditions**: Overlay visible during active publishing; removed after
  `clear_live_overlay` or TTL expiry.
- **Acceptance Criteria**:
  - [ ] `set_live_overlay` tool exists and is callable from MCP client.
  - [ ] `clear_live_overlay` tool exists and immediately removes the overlay.
  - [ ] Malformed element JSON returns a descriptive error, not a crash.

---

## SUC-004: Tag stream carries both tag frames and overlay frames
Parent: Issue — plan-live-overlay-via-tag-stream-socket

- **Actor**: Any tag stream subscriber (view_cli, robot process, test harness)
- **Preconditions**: Tag stream is active.
- **Main Flow**:
  1. Subscriber reads from the tag stream socket via `TagStreamConsumer.read()`.
  2. The method decodes a `StreamMessage` and returns either a `TagFrame` (Pydantic
     model) or an `OverlayFrame` (proto message).
  3. Caller checks the return type and handles each appropriately.
- **Postconditions**: Both message types are correctly decoded; existing tag-frame
  consumers handle the new union return type without breakage.
- **Acceptance Criteria**:
  - [ ] `TagStreamConsumer.read()` returns `TagFrame | OverlayFrame`.
  - [ ] Existing tag detection behavior is unchanged — tag frames still arrive and
        are processed correctly.
  - [ ] Wire format is `StreamMessage` oneof wrapping both types.
