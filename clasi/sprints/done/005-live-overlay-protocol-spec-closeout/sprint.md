---
id: '005'
title: Live Overlay & Protocol Spec Closeout
status: done
branch: sprint/005-live-overlay-protocol-spec-closeout
use-cases: []
issues: []
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 005: Live Overlay & Protocol Spec Closeout

## Goals

1. Verify and close out the Daemon Protocol Specification issue — confirm the
   gRPC implementation fully satisfies the spec, then mark the issue done.
2. Implement the Live Overlay feature: multiplex `OverlayFrame` messages on the
   existing tag stream socket so that an AI agent or robot process can push
   visual annotations (arcs, arrows, points, polylines) to the live view at
   up to 10 Hz.

## Problem

**Issue 1 — Protocol Spec closeout**: The `aprilcam-daemon-protocol-specification`
issue documents the intended gRPC architecture. Sprint 004 implemented it, but
the issue was never formally verified and closed. It remains open, creating noise
in the backlog.

**Issue 2 — Live Overlay**: A robot running pure-pursuit navigation needs to
display its decision state (lookahead arc, target point, heading arrow, intended
path) updated live in the `aprilcam view` window. There is currently no mechanism
for an external process to push graphical overlays into the view. File-based
approaches introduce 1–38 ms latency and per-frame stat() calls; the tag stream
socket (already open, already read by view_cli) is the right channel.

## Solution

**Closeout**: Read the spec issue, check each implementation claim against the
actual source files, and if everything matches, mark the issue done.

**Live Overlay**: Extend the tag stream wire format from bare `TagFrame` protobuf
to a `StreamMessage` oneof wrapper that can carry either a `TagFrame` or a new
`OverlayFrame`. Add a `PublishOverlay` gRPC RPC that lets any caller (MCP tool or
direct `DaemonControl` caller) push an overlay to all tag stream subscribers.
`view_cli` handles the new message type by storing the latest overlay and drawing
it on every rendered frame until its TTL expires.

## Success Criteria

- Protocol spec issue is verified and marked done.
- `set_live_overlay` MCP tool is callable; overlays appear in `aprilcam view`
  within one frame of calling.
- Overlays disappear automatically after their TTL (default 1.0 s).
- `clear_live_overlay` immediately removes the overlay.
- A 10 Hz loop calling `DaemonControl.publish_overlay()` produces visually smooth
  real-time tracking in the live view.
- `uv run pytest tests/` passes with no regressions.

## Scope

### In Scope

- Protocol spec issue verification and closeout (single ticket).
- `proto/aprilcam.proto` — add `OverlayElement`, `OverlayFrame`, `StreamMessage`,
  `PublishOverlay` RPC, and `PublishOverlayRequest`.
- Protobuf Python bindings regeneration.
- `daemon/stream.py` — wrap frames in `StreamMessage`; add
  `TagStreamProducer.publish_overlay()`.
- `client/stream.py` — decode `StreamMessage`; return `TagFrame | OverlayFrame`.
- `daemon/grpc_server.py` — implement `PublishOverlay` RPC.
- `client/control.py` — add `DaemonControl.publish_overlay()`.
- `ui/display.py` — add `draw_live_overlay()` with arc, arrow, point, polyline support.
- `cli/view_cli.py` — handle overlay messages in tag reader thread; apply overlay
  in render loop with TTL expiry.
- `server/mcp_server.py` — add `set_live_overlay` and `clear_live_overlay` tools.

### Out of Scope

- Persistent overlays stored to disk (paths.json stays file-based and unchanged).
- Image stream changes (overlay travels only on the tag stream).
- Multi-overlay layering / named overlay channels.
- Streamable HTTP transport (deferred to a later sprint).

## Test Strategy

- Import smoke tests: confirm MCP server, daemon modules, and client modules
  import cleanly after changes.
- Unit tests for `draw_live_overlay()` covering each element type and TTL expiry.
- Regression: `uv run pytest tests/` must pass.
- Manual integration: run `aprilcam view` with a live camera, call
  `set_live_overlay` via MCP, verify overlays appear and expire.

## Architecture Notes

- All coordinates in overlay elements are world cm; `draw_live_overlay()` maps
  them through the inverse homography to display pixels.
- `draw_live_overlay()` is a no-op when homography is None (uncalibrated camera).
- The `StreamMessage` wrapper is a breaking wire format change — all senders and
  receivers are updated atomically in this sprint.
- Element types unknown to the renderer are silently skipped (forward compatibility).

## GitHub Issues

(None linked yet.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Verify and close daemon protocol spec issue | — |
| 002 | Extend proto: StreamMessage wrapper + PublishOverlay RPC | — |
| 003 | Regenerate protobuf Python bindings | 002 |
| 004 | Update stream producers to use StreamMessage | 003 |
| 005 | Update tag stream consumer to decode StreamMessage | 004 |
| 006 | Implement PublishOverlay gRPC servicer + DaemonControl method | 005 |
| 007 | Add draw_live_overlay to display module | 003 |
| 008 | Handle overlay messages in view_cli | 006, 007 |
| 009 | Add set_live_overlay and clear_live_overlay MCP tools | 006 |

Tickets execute serially in the order listed.
