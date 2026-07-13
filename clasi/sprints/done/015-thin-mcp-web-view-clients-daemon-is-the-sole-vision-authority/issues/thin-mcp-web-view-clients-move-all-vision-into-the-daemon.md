---
status: in-progress
sprint: '015'
tickets:
- 015-001
---

# Thin MCP / web / view clients — the daemon is the sole vision authority

## Problem

The MCP server still performs **computer vision in-process** on frames it
fetches from the daemon, which forces `mcp`, `web`, and `view` to depend on
**opencv-contrib** even though the camera and detection already live in the
daemon. The MCP server is not — and should not be — a generalized vision
pipeline. The daemon is the vision authority; everything else should be a thin
client that gets perception *results* or raw *frames* and does no pixel work.

(Surfaced while bringing the daemon up remotely on a Pi: a base/client install
can't run `aprilcam cameras`/`view`/`mcp` without opencv, which is wrong for a
remote-client machine.)

## Target architecture

**The daemon is the only component that does any computer vision** (camera
capture + AprilTag/ArUco detection + object detection). `mcp`, `web`, and `view`
become pure gRPC daemon clients with **no OpenCV dependency and no in-process
vision**. This extends [[project_daemon_sole_camera_owner]] from "sole camera
owner" to "sole vision authority".

### MCP server (`src/aprilcam/server/mcp_server.py`)
Keep (all via daemon RPCs / passthrough — no cv2):
- Camera lifecycle: `open_camera`, `close_camera`, `list_cameras` (EnumerateCameras).
- Playfield / calibration / config / paths (the file-proxy RPCs).
- **Perception results**: `get_tags`, `get_objects`, `where`, `stream_tags`,
  `get_tag_history` — sourced from the daemon's `GetTags` / `GetTagStream` /
  `WhereIs` (and an objects/history RPC as needed). `get_tag_history` buffers
  **tag records** (plain data) from the tag stream — no pixels, no cv2.
- **Raw frames**: `get_frame` / `capture_frame` — pass the daemon's JPEG
  (`CaptureFrame`) straight through as base64/file. No decode, no cv2.

**Remove entirely** (do NOT relocate to the daemon — consumers fetch a frame and
run their own CV outside the MCP):
- Image-processing tools: `detect_lines`, `detect_circles`, `detect_contours`,
  `detect_motion`, `detect_qr_codes`, `crop_region`, `apply_transform`.
- The in-process `DetectionLoop` / `AprilCam` ArUco detector and
  `resolve_source` local-CV path (`mcp_server.py` ~2280–2530, the
  `DaemonCapture`+`DetectionLoop` machinery). Tags come from the daemon.

### web "hub" (`src/aprilcam/server/web_server.py`)
- **Stop wrapping `mcp_server`.** Connect directly to the daemon socket via
  `DaemonControl`. Expose an HTTP/WebSocket bridge over the daemon's data:
  tags / tag stream / frames / overlay (`GetTags`, `GetTagStream`,
  `CaptureFrame`/`GetImageStream`, `PublishOverlay`). Define the REST/WS surface
  as a thin translation of those RPCs — no MCP-tool indirection, no cv2.

### view (`src/aprilcam/cli/view_cli.py`)
- Decode the JPEG stream with **Pillow** (`Image.open(BytesIO(...))`) instead of
  `cv2.imdecode`; display stays tkinter + `ImageTk`. Drop the cv2 import. `view`
  then needs only Pillow + tkinter.

### Packaging (`pyproject.toml`)
- **opencv-contrib becomes daemon-only.** Base/client install (which now covers
  `mcp`, `web`, `view`) is opencv-free; move `opencv-contrib-python` (and
  anything else only the daemon needs) out of the path the clients import.
  Add Pillow to base deps (for `view`). Remove `cameras`/`tags`/`view`/`mcp`/`web`
  from the `DAEMON_COMMANDS` "needs opencv" hint set in `cli/__init__.py` as they
  become opencv-free (keep only genuine daemon-side commands there).

## Confirmed scope decisions (stakeholder-approved)

1. **Image-processing tools are DELETED from the product** — not moved to the
   daemon. `detect_lines`, `detect_circles`, `detect_contours`, `detect_motion`,
   `detect_qr_codes`, `crop_region`, `apply_transform` are removed outright. A
   consumer that needs pixel processing calls `get_frame`/`capture_frame` and
   runs its own CV outside the MCP. Remove their tests, docs, and any helper
   code that exists only to serve them.
2. **Object detection moves to the daemon.** Add a daemon **`GetObjects`** RPC
   (and, if needed for `get_tag_history`/`stream_tags` to be thin, a tag-history
   query or rely on buffering tag *records* from `GetTagStream` client-side —
   plain data, no cv2). The MCP `get_objects`/`get_tags`/`get_tag_history`/
   `stream_tags`/`where` then all source from daemon RPCs. No in-process
   detection remains in the MCP server.
3. **web ("hub") is a direct daemon client with a thin bridge surface.** It
   connects via `DaemonControl` (no `mcp_server` import) and exposes an HTTP/WS
   translation of the daemon RPCs: REST for one-shot tags/objects/where/frame,
   a WebSocket for the live tag stream, image/frame fetch, and overlay publish.
   The endpoints are a 1:1 thin mapping of daemon RPCs — no MCP-tool indirection,
   no pixel work in the web layer.

## Requirements / docs to update

- `.clarules`/`project-overview.md`: remove the **Image Processing Tools** MCP
  section and the Sprint-5 scope; document the new boundary — *MCP/web = perception
  results + raw frames only; all vision lives in the daemon*.
- Update `AGENT_GUIDE.md` / `ROBOT_API_GUIDE.md` / `docs/wiki` accordingly.

## Related / out of scope

- Raspberry Pi CSI camera support via libcamera (separate issue) — orthogonal:
  this issue is about the client/daemon vision boundary, not how the daemon
  opens a Pi camera.
- Builds on [[project_daemon_sole_camera_owner]], the v0.20260619.8 remote-daemon
  + discovery work, and the file-proxy RPCs.
