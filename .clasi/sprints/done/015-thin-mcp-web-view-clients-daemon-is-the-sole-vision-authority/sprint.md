---
id: '015'
title: "Thin MCP/web/view Clients \u2014 Daemon Is the Sole Vision Authority"
status: done
branch: sprint/015-thin-mcp-web-view-clients-daemon-is-the-sole-vision-authority
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
- SUC-007
issues: []
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 015: Thin MCP/web/view Clients — Daemon Is the Sole Vision Authority

## Goals

Make the daemon the **sole vision authority**. After this sprint, `cv2`/`opencv`
must not be imported by `mcp_server.py`, `web_server.py`, or `view_cli.py`. The
MCP server, web hub, and `aprilcam view` become pure gRPC daemon clients that
consume perception *results* (tag records, object records, raw JPEG frames) —
they perform no pixel work.

## Problem

The MCP server runs computer vision in-process: `stream_tags`/`start_detection`
instantiate a full `DetectionLoop` + `AprilCam` ArUco detector backed by
`DaemonCapture`. `get_objects` runs `ColorClassifier` + `cv2.pointPolygonTest`
on live frames. Seven image-processing MCP tools (`detect_lines`, `detect_circles`,
`detect_contours`, `detect_motion`, `detect_qr_codes`, `crop_region`,
`apply_transform`) import OpenCV at call time. This forces `opencv-contrib` onto
any machine that runs `mcp`, `web`, or `view` — wrong for a remote client that
only needs to talk to a Pi daemon.

## Solution

1. Delete the seven image-processing MCP tools outright (confirmed stakeholder decision).
2. Add a `GetObjects` RPC to the daemon; move `ColorClassifier` / object detection
   into the daemon pipeline; MCP `get_objects` calls the RPC.
3. Rewire `get_tags`, `stream_tags`/`start_detection`, `get_tag_history`, `where`
   to consume the daemon's `GetTags` / `GetTagStream` / `WhereIs` RPCs. Client-side
   tag-history buffer holds plain tag records (no pixels). Gripper world-xy and
   A1 coord transforms (pure data math) stay in the MCP.
4. Rewrite `web_server.py` as a direct daemon client via `DaemonControl` — no
   `mcp_server` import — exposing a thin HTTP/WS bridge over daemon RPCs.
5. Slim `view_cli.py` to use Pillow for JPEG decoding instead of `cv2.imdecode`.
6. Move `opencv-contrib-python` to daemon-only in `pyproject.toml`; add Pillow to
   base deps; update `DAEMON_COMMANDS` in `cli/__init__.py`.
7. Update docs and add a verification test for the opencv-free client import.

## Success Criteria

- `grep -r "import cv2\|import cv " src/aprilcam/server/ src/aprilcam/cli/view_cli.py`
  returns zero matches.
- The verification test (`test_015_opencv_free_clients.py`) passes with cv2
  monkeypatched as unavailable.
- `uv run pytest` green.
- `aprilcam mcp`, `aprilcam web`, and `aprilcam view` start without importing
  `opencv-contrib`.

## Scope

### In Scope

- Delete `detect_lines`, `detect_circles`, `detect_contours`, `detect_motion`,
  `detect_qr_codes`, `crop_region`, `apply_transform` MCP tools (handlers + tests
  + docs + helpers).
- Remove `DetectionLoop` / `AprilCam` / `DaemonCapture` / `resolve_source`
  in-process detection machinery from `mcp_server.py`; rewire all tag/detection
  tools to daemon RPCs.
- Add `GetObjects` RPC to `proto/aprilcam.proto`; regen stubs; add handler in
  `daemon/grpc_server.py`; rewire MCP `get_objects`.
- Rewrite `web_server.py` as a direct `DaemonControl` client.
- Slim `view_cli.py` to use Pillow instead of `cv2.imdecode`.
- `pyproject.toml` packaging changes (opencv to daemon extra, Pillow to base).
- `DAEMON_COMMANDS` cleanup in `cli/__init__.py`.
- Doc updates: `project-overview.md`, `AGENT_GUIDE.md`, `ROBOT_API_GUIDE.md`,
  `docs/wiki/`.
- Verification test: `test_015_opencv_free_clients.py`.
- Single version bump at sprint close.

### Out of Scope

- Raspberry Pi CSI camera / libcamera support.
- Streamable HTTP MCP transport.
- TLS on gRPC.
- Any new MCP tools beyond what is listed.
- Changes to the daemon's detection pipeline internals beyond adding `GetObjects`.

## Test Strategy

- Unit tests for the `GetObjects` daemon handler (mocked pipeline).
- Unit test that `get_tags`, `get_tag_history`, `stream_tags` work against a mock
  daemon gRPC stub (no real camera).
- Verification test (`test_015_opencv_free_clients.py`) that monkeypatches
  `__import__` to raise on `cv2` and asserts `mcp_server`, `web_server`, and
  `view_cli` still import cleanly.
- Update/remove existing image-processing tool tests.
- All existing tests must remain green.

## Architecture Notes

- Builds on Sprint 014 (remote daemon, file-proxy RPCs, `DaemonControl`,
  `discovery.py`, `cli/_daemon.py`).
- The daemon already exposes: `GetTags`, `GetTagStream`, `WhereIs`,
  `CaptureFrame`, `GetImageStream`, `PublishOverlay`, all file-proxy RPCs.
- `GetObjects` is the only new RPC in this sprint.
- `gripper_world_xy` computation (pure dict math, no pixels) stays in MCP.
- The tag-history ring buffer migrates from holding `RingBuffer` frames (with
  numpy arrays) to holding plain tag-record dicts streamed from `GetTagStream`.
- `vision/objects.py` (ColorClassifier, SquareDetector) becomes daemon-only code;
  it must not be imported from any client path after this sprint.

## GitHub Issues

(none yet)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Add GetObjects RPC to proto and daemon | — |
| 002 | Delete MCP image-processing tools | — |
| 003 | Remove in-process detection; rewire MCP tag tools to daemon | 001 |
| 004 | Rewrite web_server.py as direct daemon client | 003 |
| 005 | Slim view_cli.py to Pillow JPEG decode | — |
| 006 | Packaging: opencv to daemon-only, Pillow to base | 003, 005 |
| 007 | Docs update and opencv-free verification test | 002, 003, 004, 005, 006 |

Tickets execute serially in the order listed.
