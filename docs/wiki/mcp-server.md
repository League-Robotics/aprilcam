---
title: Using the MCP Server
blurb: How an AI agent drives AprilCam over the Model Context Protocol — the golden path, the coordinate system, and the full tool reference.
order: 20
updated: 2026-06-20
tags: [mcp, agent, tools]
---

# Using the AprilCam MCP Server

The MCP server (`aprilcam mcp`) exposes AprilCam to AI agents over the
**Model Context Protocol**. You open cameras, build playfields, read tags and
objects, capture frames, draw paths and overlays, and look up playfield
features — all through tools, using opaque handles. **You never need, and do
not have, filesystem access**: `camera_id` and `playfield_id` are handles, not
paths.

The server is a **thin client**. All vision (AprilTag/ArUco detection,
homography, deskew, object detection) runs in the [daemon](daemon.md); the MCP
server forwards perception results and raw frames. The live-camera flow needs
**no OpenCV** in the MCP server's environment.

## Running it

The MCP server speaks stdio. Register it with your MCP client (e.g. an
`.mcp.json`):

```json
{
  "mcpServers": {
    "aprilcam": { "command": "aprilcam", "args": ["mcp"] }
  }
}
```

`aprilcam mcp` auto-connects to the daemon (spawning it if needed), so the only
prerequisite is that the camera host's daemon is reachable. Base install is
enough — `pipx install aprilcam` ships the MCP SDK; OpenCV is not required (see
[install tiers](overview.md#install-tiers)).

> The server keeps **no state across restarts** and auto-opens nothing. Every
> session must call `open_camera` first — it is the gate that registers the
> handle every downstream tool needs.

## The golden path

```text
1. open_camera(pattern="<name>")  ->  camera_id  (+ playfield_id if configured)
2. (only if no playfield_id came back)  create_playfield(camera_id)
3. stream_tags(playfield_id)      ->  start the detection loop
4. get_tags / get_objects / where / create_path  using the playfield_id
5. stop_stream(playfield_id)      ->  when done
```

- **`open_camera`** returns a `playfield_id` and `playfield_name` immediately
  when the camera is already configured and calibrated — skip step 2. A
  `"calibration_stale": true` flag means recalibration is needed.
- Pass the **`playfield_id`** as the `source_id` to `stream_tags` / `get_tags`
  / `get_objects` / `where` so that `world_xy` populates. Passing the
  `camera_id` also works — the server resolves the camera's playfield.
- Discover the field with `get_playfield()` / `list_playfields()`; search it
  with `where()`. Call `get_robot_api_guide()` for the high-rate Python API.

### Camera numbers are the persistent enumeration handle

`list_cameras` returns each camera's **persistent enumeration number** — a
stable handle that does not change when cameras are plugged/unplugged — as
`index`. `open_camera(index=N)` accepts that same number (it is resolved to the
live OS device index via the daemon). Prefer `pattern=` (a name substring) when
you can; it is unambiguous.

### Coordinate system

When the playfield is calibrated, every tag/object carries `world_xy` in
**centimetres**, **A1-centred**: the origin is AprilTag 1 (playfield centre),
`+x` is east, `+y` is north. Angles (`orientation_yaw`, `heading_rad`) are
radians, `0` = +x, counter-clockwise positive — so a tag's forward direction is
`(cos yaw, sin yaw)`. Parallax correction and the A1 origin shift are applied by
the daemon. `get_objects` reports object `world_xy` in the **same** A1-centred
frame as `get_tags`.

### Image return format

Every tool that returns an image takes a `format`: `"base64"` (inline in the
response, default) or `"file"` (a JPEG is written to a temp file and its path
returned). Choose per request.

## Tool reference

### Version & connection

| Tool | Purpose |
|------|---------|
| `get_version()` | Package version and the active daemon target. |
| `connect_daemon(host?, port?, local?)` | Switch the daemon connection (mDNS by default, or a host/port, or `local=True` for the Unix socket). Tears down all session state and reconnects. |

### Cameras

| Tool | Purpose |
|------|---------|
| `list_cameras()` | Available cameras, each with its persistent `index` (enum), `name`, `slug`. |
| `open_camera(index?, pattern?, source?, backend?)` | Open a camera (or `source="screen"`); returns `camera_id`, and `playfield_id`/`playfield_name` if configured. **The required first call.** |
| `close_camera(camera_id)` | Release a camera. |
| `set_camera_playfield(camera_id, playfield)` | Link a camera to a named playfield definition (then calibrate). |

### Playfields

| Tool | Purpose |
|------|---------|
| `list_playfields()` | Named playfield definitions known to the server. |
| `get_playfield(name?)` | The whole playfield map: dimensions, origin, every AprilTag/ArUco/rectangle/dot with world positions. |
| `get_playfield_info(playfield_id)` | Live state of a registered playfield: corners, `calibrated`, homography. |
| `create_playfield(camera_id, max_frames?)` | Build a playfield by detecting ArUco corners from the live feed.¹ |
| `calibrate_playfield(playfield_id, camera_height_cm?, …)` | Compute and store the homography from the linked playfield definition.¹ |

### Detection, tags & objects

| Tool | Purpose |
|------|---------|
| `stream_tags(source_id, …)` | **Preferred** — start a detection loop (records an ops pipeline). |
| `start_detection(source_id, …)` | Legacy equivalent of `stream_tags`. |
| `stop_stream(source_id)` / `stop_detection(source_id)` | Stop the loop (match the one you started with). |
| `get_tags(source_id)` | Latest detections: `id`, `center_px`, `world_xy`, `orientation_yaw`, velocity, `in_playfield`. |
| `get_tag_history(source_id, num_frames?)` | Recent frames from the ring buffer (up to 300). |
| `get_objects(source_id)` | Detected non-tag colored objects with `world_xy`, `color`, `bbox`. |
| `where(query, source_id?)` | Natural-language playfield lookup (e.g. "where is the red dot"); merges live detection when `source_id` is given. |
| `pixel_to_world(source_id, pixels)` | Convert arbitrary pixel coordinates to A1-centred cm. |

### Frame capture

| Tool | Purpose |
|------|---------|
| `capture_frame(camera_id, format?, quality?)` | One frame; a playfield source is deskewed. |
| `get_frame(source_id, format?, quality?)` | Raw frame, no processing. |

### Live view, paths & overlays

| Tool | Purpose |
|------|---------|
| `start_live_view(camera_id, …)` / `stop_live_view(view_id)` | Open/close the live visualization window (tag overlays, paths). |
| `create_path(playfield_id, waypoints_json, name?)` | Add a persistent waypoint path (world cm); rendered in the live view. |
| `list_paths(playfield_id)` / `delete_path(path_id)` / `clear_paths(playfield_id)` | Manage paths. |
| `set_live_overlay(camera_id, elements_json, ttl?)` | Push transient overlay drawables (arc/arrow/point/polyline/text/rect/polygon, world cm). |
| `clear_live_overlay(camera_id)` | Remove the overlay immediately. |

### Frame registry & static images ¹

| Tool | Purpose |
|------|---------|
| `create_frame` · `create_frame_from_image` · `process_frame` · `get_frame_image` · `save_frame` · `release_frame` · `list_frames` | Capture/load a frame into a registry and run `deskew`/`detect_tags`/`detect_aruco` on it. |
| `deskew_image(playfield_id, image_path, …)` | Warp a static image to top-down via a playfield's homography. |

> **¹ OpenCV note.** Most tools are cv2-free, but a few do pixel work *inside*
> the MCP server and therefore need OpenCV in the MCP server's environment:
> `create_playfield` (live corner detection), `calibrate_playfield`, and the
> frame-registry / static-image tools. For an already-calibrated camera the
> golden path never hits these — `open_camera` returns the `playfield_id`
> directly. Calibration is normally done once on the daemon host with
> `aprilcam calibrate`. Moving this residual pixel work into the daemon is
> tracked work; until then these tools raise a clear install-hint error on a
> base (no-OpenCV) install.

## Robot hand-off

For a robot control loop that needs tags at 5–50 Hz or live overlay drawing,
don't drive it through MCP at runtime — call `get_robot_api_guide()` and hand
the program the [`DaemonControl` Python API](robot-direct-api.md), which talks
to the daemon directly over gRPC.
