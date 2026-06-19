# AprilCam Agent Guide

You are using **AprilCam**, a Python library and MCP server for camera
management, AprilTag/ArUco fiducial marker detection, playfield
homography, and image processing on robotics playfields.

## Quick Start (Library)

```python
from aprilcam import detect_tags

for tags in detect_tags(camera=3):
    for t in tags:
        # age=0.0 means seen this frame; age>0 means stale (last seen age seconds ago)
        status = "live" if t.age == 0 else f"stale {t.age:.2f}s"
        print(f"Tag {t.id} [{status}] at ({t.center_px[0]:.0f}, {t.center_px[1]:.0f})")
```

### Tags + Objects (dual camera)

```python
from aprilcam import detect_tags

for tags in detect_tags(camera=3, detect_objects=True, color_camera=2):
    for t in tags:
        print(f"Tag {t.id} world=({t.world_xy[0]:.1f}, {t.world_xy[1]:.1f})")
    for obj in tags.objects:  # .objects attribute on the tag list
        print(f"  {obj.color} cube at ({obj.world_xy[0]:.1f}, {obj.world_xy[1]:.1f})")
```

### One-shot Object Detection

```python
from aprilcam import detect_objects

objects = detect_objects(camera=3, color_camera=2)
for obj in objects:
    print(f"{obj.color} at ({obj.world_xy[0]:.1f}, {obj.world_xy[1]:.1f})")
```

## Quick Start (MCP)

```json
{ "mcpServers": { "aprilcam": { "command": "aprilcam", "args": ["mcp"] } } }
```

Then call `list_cameras` → `open_camera` → `start_detection` → `get_tags`.

Run `aprilcam --agent` (or `aprilcam --agent robot` for the robot API guide)
to print this guide to stdout from any shell context.

## Directory Layout

AprilCam uses FHS directories when running as root and XDG directories
for non-root use. See `aprilcam config` for the current resolved paths.

| Concern | System (root) | Developer (non-root) | Override |
|---------|--------------|----------------------|---------|
| Data (persistent) | `/var/lib/aprilcam` | `~/.local/share/aprilcam` | `APRILCAM_DATA_DIR` |
| Runtime (sockets) | `/run/aprilcam` | `$XDG_RUNTIME_DIR/aprilcam` | `APRILCAM_SOCKET_DIR` |
| Logs | `/var/log/aprilcam` | `~/.local/state/aprilcam` | `APRILCAM_LOG_DIR` |

## Core Concepts

### Cameras
- **Index**: Integer (0, 1, 2...) identifying a camera device.
- **camera_id**: Handle string returned by `open_camera` (e.g., `cam_0`).
- Cameras are opened by index or by name pattern (substring match).
- Screen capture is also available as a camera source (`source="screen"`).

### Playfields
- A **playfield** is a camera view with ArUco corner markers (IDs 0-3)
  defining a rectangular region.
- **Deskew**: Perspective-corrects the playfield to a top-down rectangle
  using the corner markers. No calibration needed.
- **Calibration**: Maps pixel coordinates to real-world units (cm) by
  providing physical measurements between corner markers.
- A `playfield_id` can be used anywhere a `camera_id` is accepted.

### Tag Detection
- **Detection loop**: A persistent background loop that detects
  AprilTag 36h11 and ArUco markers on every frame, storing results
  in a 300-frame ring buffer (~10 seconds at 30fps).
- **TagRecord** fields: `id`, `center_px`, `corners_px`,
  `orientation_yaw`, `world_xy` (if calibrated), `vel_px`,
  `speed_px`, `vel_world`, `speed_world`, `heading_rad`,
  `in_playfield`, `timestamp`, `frame_index`, `age`.
- **`age`**: 0.0 if the tag was detected this frame. If the tag was
  seen recently but not this frame, `age` is the seconds since last
  detection. Tags are returned for up to ~1 second after they
  disappear, then pruned. This gives callers a stable tag set
  without flickering.
- Velocities are EMA-smoothed with dead-band suppression.

### Homography Files
- Calibration data is saved to `data/` as JSON files.
- **Per-camera naming**: `data/homography-<device-slug>-<WxH>.json`
  (e.g., `data/homography-brio-501-1920x1080.json`).
- **Global fallback**: `data/homography.json` is used if no per-camera
  file exists.
- The `detect_tags()` generator auto-discovers the right homography
  file when `homography="auto"` (default).
- Homography only needs to be computed once per camera position.

### Joint Calibration (Multi-Camera)
- For dual-camera setups (B&W for speed + color for classification),
  use **joint calibration**: `data/joint-calibration.json`.
- Joint calibration uses ALL visible tags (ArUco corners + AprilTags)
  as shared reference points — typically 10-12 correspondence points.
- The B&W camera gets a standard 3x3 homography.
- The color camera gets a 3x3 homography PLUS barrel distortion
  correction (camera_matrix + dist_coeffs from `cv.calibrateCamera`).
- File has `"type": "joint"` to distinguish from single-camera files.

**Running calibration:**
```python
from aprilcam import calibrate

calibrate(bw_camera=3, color_camera=2)
# Saves to data/calibration.json (default)

# Custom output path:
calibrate(bw_camera=3, color_camera=2, output="my_calibration.json")
```

**Using the calibration:**
```python
from aprilcam.homography import load_joint_calibration
from pathlib import Path

bw_cal, color_cal = load_joint_calibration(Path("data/joint-calibration.json"))

# Undistort a color frame (removes barrel distortion)
corrected = color_cal.undistort(color_frame)

# Map pixel to world coordinates (both cameras in same space)
wx, wy = bw_cal.pixel_to_world(px, py)      # B&W pixel → cm
wx, wy = color_cal.pixel_to_world(px, py)    # Color pixel → cm
```

## Library API

### Primary Interface

```python
from aprilcam import detect_tags

# Simple: open camera 0, auto-load homography, yield tags per frame
for tags in detect_tags(camera=0):
    ...

# With options
for tags in detect_tags(
    camera=0,              # index or device name pattern
    homography="auto",     # "auto", path, or None
    family="36h11",        # AprilTag family
    data_dir="data",       # where homography files live
    proc_width=0,          # processing width (0 = native)
):
    ...
```

### Available Imports

```python
from aprilcam import (
    detect_tags,              # Generator: yields tags per frame (with age)
    detect_objects,           # One-shot: returns colored cubes
    AprilCam,                 # Core detection engine
    TagRecord,                # Per-tag detection result (has .age field)
    ObjectRecord,             # Per-object detection result (has .color field)
    AprilTag,                 # Tag model with tracking state
    Playfield,                # Playfield polygon and deskew
    CameraError,              # Base camera exception
    CameraInUseError,         # Camera busy (includes PID)
    CameraNotFoundError,      # Camera index doesn't exist
    CameraPermissionError,    # Permission denied
)
```

### Lower-Level Usage

```python
from aprilcam import AprilCam
from aprilcam.config import AppConfig
import cv2, time

cfg = AppConfig.load()
H = cfg.load_homography(device_name="Brio 501", resolution=(1920, 1080))

cam = AprilCam(index=0, homography=H, headless=True)
cap = cam._init_capture()
cam.reset_state()

while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break
    tags = cam.process_frame(frame, time.monotonic())
    for tr in tags:
        print(tr.id, tr.center_px, tr.world_xy)

cap.release()
```

## MCP Tools Reference

### Camera Management
- `list_cameras()` → list of `{index, name, backend}`
- `open_camera(index?, pattern?, source?, backend?)` → `{camera_id}`
- `close_camera(camera_id)` → confirmation
- `capture_frame(source_id, format?, quality?)` → image

### Playfield & Homography
- `create_playfield(camera_id, max_frames?)` → `{playfield_id}`
- `create_playfield_from_image(path)` → `{playfield_id}`
- `calibrate_playfield(playfield_id, width, height, units?)` → calibration result
- `deskew_image(source_id, format?, quality?)` → deskewed image
- `get_playfield_info(playfield_id)` → polygon, calibration, dimensions

### Tag Detection & Tracking
- `start_detection(source_id, family?, proc_width?, detect_interval?)` → `{status: "started"}`
- `stop_detection(source_id)` → confirmation
- `get_tags(source_id)` → `{tags: [{id, center_px, world_xy, vel_px, ...}]}`
- `get_tag_history(source_id, num_frames?)` → ring buffer frames
- `stream_tags(source_id)` → SSE stream of tag updates

### Image Processing
- `get_frame(source_id, format?, quality?, annotate?)` → raw frame
- `crop_region(source_id, x, y, w, h)` → cropped image
- `detect_lines(source_id)` → line segments
- `detect_circles(source_id)` → circles with centers/radii
- `detect_contours(source_id)` → contour polygons
- `detect_motion(source_id)` → motion regions
- `detect_qr_codes(source_id)` → decoded QR data
- `apply_transform(source_id, operation, ...)` → transformed image

### Frame Model (batch processing)
- `create_frame(source_id)` / `create_frame_from_image(path)` → `{frame_id}`
- `process_frame(frame_id, operations)` → processed frame
- `get_frame_image(frame_id)` → image data
- `save_frame(frame_id, path)` → saved file path
- `release_frame(frame_id)` → confirmation
- `list_frames()` → active frames

### Multi-Camera Compositing
- `create_composite(name, sources)` → `{composite_id}`
- `get_composite_frame(composite_id)` → merged frame
- `get_composite_tags(composite_id)` → merged tag detections

### Live View (web UI)
- `start_live_view(source_id, annotate?)` → `{view_id, url}`
- `stop_live_view(view_id)` → confirmation

### Server / Daemon Management
- `get_version()` → `{version, active_daemon_host, active_daemon_port}` — package
  version plus the host/port (or `unix:<path>`) of the currently connected daemon.
  `active_daemon_host` and `active_daemon_port` are `null` when no daemon is
  connected yet.
- `connect_daemon(host?, port?, local?)` → `{target, cameras}` — switch the MCP
  server's live daemon connection at runtime without restarting the MCP process.
  Tears down all open cameras, detection loops, and session state, then reconnects
  to the specified target.  Arguments:
  - `host` (str, optional) — hostname or IP; omit for mDNS auto-discovery.
  - `port` (int, default 5280) — TCP port.
  - `local` (bool, default false) — force Unix-socket connection to the local daemon.
  On success returns the resolved target string and a list of available cameras.

## Common Workflows

### Workflow 1: Detect tags on a playfield with world coordinates

```
1. list_cameras          → find camera index
2. open_camera(index=0)  → cam_0
3. create_playfield(camera_id="cam_0")  → pf_0
4. calibrate_playfield(playfield_id="pf_0")   # dimensions come from the linked playfield definition
5. start_detection(source_id="pf_0")
6. get_tags(source_id="pf_0")  → tags with world_xy in cm
```

### Workflow 2: Quick visual inspection

```
1. open_camera(index=0)  → cam_0
2. get_frame(source_id="cam_0", format="base64", annotate=true)
```

### Workflow 3: Track tag movement over time

```
1. open_camera + create_playfield + calibrate + start_detection
2. get_tag_history(source_id="pf_0", num_frames=60)
   → last 60 frames of tag positions, velocities, headings
```

### Workflow 4: Detect colored objects (dual-camera)

```
1. Run joint calibration once (saves to data/joint-calibration.json):
   calibrate_joint(bw_cap, color_cap, field_width_cm=134.3, field_height_cm=89.3)
2. aprilcam live -c 3 --color-camera 2
3. Press 'd' to detect objects — uses joint calibration for
   barrel-corrected color fusion in world coordinates
4. Press 'c' to clear overlays
```

## CLI Commands

```
aprilcam mcp          # Start MCP server (stdio)
aprilcam web          # Start HTTP server with REST API, MCP SSE, WebSocket, and web UI
aprilcam cameras      # List available cameras
aprilcam taggen       # Generate AprilTag/ArUco marker images
aprilcam live         # Open live camera view with tag overlays
aprilcam init         # Configure MCP entries for Claude Code / VS Code
aprilcam tool         # List, inspect, and run MCP tools from CLI
```

## Web Server

```
aprilcam web [--port 17439] [--host 0.0.0.0]
```

Provides:
- **REST API**: `POST /api/<tool_name>` — same tools as MCP
- **MCP SSE**: `/mcp/sse` — MCP protocol over Server-Sent Events
- **WebSocket**: `/ws/tags/<source_id>` — real-time tag streaming
- **Web UI**: `/` — live camera view with tag table

## Error Handling

- Camera not found → `CameraNotFoundError`
- Camera in use by another process → `CameraInUseError`
  - Includes `pid` and `process_name` attributes
  - Error message includes `kill <PID>` suggestion
  - On macOS: uses `lsof` to identify blocking process
  - On Linux: uses `fuser` on `/dev/video*`
- Permission denied → `CameraPermissionError`
- All inherit from `CameraError` — catch that for any camera issue
- MCP tools return `{"error": "message"}` on failure

```python
from aprilcam import detect_tags, CameraInUseError

try:
    for tags in detect_tags(camera=0):
        ...
except CameraInUseError as e:
    print(f"Camera busy: {e}")
    if e.pid:
        print(f"Kill blocking process: kill {e.pid}")
```

## Tips for Agents

1. **Always call `list_cameras` first** to discover available cameras
   and their indices.
2. **Playfield source IDs work everywhere** camera IDs work — prefer
   playfields when you need deskewed views or world coordinates.
3. **Start detection once**, then poll `get_tags` repeatedly — don't
   start/stop the detection loop per query.
4. **Homography persists** — calibrate once, and future sessions
   auto-load the per-camera file from `data/`.
5. **Use `annotate=true`** on `get_frame` to see tag overlays for
   visual debugging.
6. **Tag velocities** are in pixels/second (`vel_px`) and world
   units/second (`vel_world`, if calibrated). They are EMA-smoothed
   with a dead-band to suppress jitter on stationary tags.
