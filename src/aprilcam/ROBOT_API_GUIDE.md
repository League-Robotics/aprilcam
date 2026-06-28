# AprilCam Robot Direct API

**This guide is for robot programs that need high-frequency access to tag
positions and live overlay drawing.**  If you are an AI agent working
interactively, use the MCP tools instead.  If you are writing a control
loop that runs at 5-50 Hz, use the Python API described here — it skips
the MCP layer entirely and talks directly to the daemon over gRPC.

---

## Quick Start

```python
from aprilcam.config import Config
from aprilcam.client.control import DaemonControl

config = Config.load()
dc = DaemonControl.connect_default(config)   # auto-spawns daemon if needed

# One-shot tag read
cam = dc.list_cameras()[0]
tags = dc.get_tags(cam)
for tag in tags.tags:
    print(tag.id, tag.world_xy)   # world_xy is (x, y) in cm, or None

dc.close()
```

---

## ⚠️ Is your tag mounted on a robot? Register its mount offset

`tag.world_xy` is the **tag's** position. If the tag is mounted on a robot —
offset from the robot's centre of rotation — register that offset **once** and
the daemon reports your robot's **centre** (and heading) for that tag instead of
the raw tag. A tag is "mobile" simply because you registered it here.

```python
# Pose of the tag relative to the robot's centre of rotation:
#   x_mm    forward of centre (robot frame, +x forward)
#   y_mm    left of centre    (robot frame, +y left)
#   z_cm    tag height above the playfield (also corrects camera parallax)
#   yaw_deg tag heading relative to the robot's forward
# Persisted by the daemon — call once at start-up, not every loop.
dc.register_mobile_tag(100, x_mm=43, y_mm=0, z_cm=11.8, yaw_deg=0, owner="my-robot")

dc.list_mobile_tags()      # -> [{"tag_id","x_mm","y_mm","z_cm","yaw_deg","owner"}, ...]
dc.clear_mobile_tag(100)   # remove one
dc.clear_mobile_tags()     # remove all
```

Without this, `world_xy` for a robot-mounted tag is the tag itself, not the point
the robot turns about. (Operators can do the same from the CLI:
`aprilcam mobile register 100 --x 43 --z 11.8`; agents via the
`register_mobile_tag` MCP tool. All three share one persisted registry.)

---

## DaemonControl — Full API

`DaemonControl` wraps the gRPC channel.  Create it once; reuse it across
your control loop.  It is thread-safe.

### Connection

```python
from aprilcam.config import Config
from aprilcam.client.control import DaemonControl

# Auto-connect (recommended): spawns daemon if not running, probes gRPC,
# returns a ready-to-use instance.
dc = DaemonControl.connect_default(Config.load())

# Manual connect (explicit Unix socket or TCP port):
dc = DaemonControl(unix_path="/tmp/aprilcam/control.sock")
dc.connect()

# As a context manager:
with DaemonControl.connect_default(Config.load()) as dc:
    ...
```

### Camera Management

```python
cameras = dc.list_cameras()          # -> list[str]  e.g. ["arducam-ov9782-usb-camera"]
cam = dc.open_camera(index=4)        # -> str cam_name   (index = OS camera number)
info = dc.get_camera_info(cam)       # -> CameraInfo(cam_name, calibrated, frame_size, fps)
frame = dc.capture_frame(cam)        # -> np.ndarray BGR (JPEG decoded)
dc.reload_calibration(cam)           # reload calibration.json from disk
dc.close_camera(cam)
dc.shutdown()                        # stop the daemon process
```

### One-Shot Tag Query

Best for low-frequency agents.  Returns the latest frame's tags without
setting up a stream.

```python
tag_frame = dc.get_tags(cam)
# tag_frame.tags          -> list[TagRecord]
# tag_frame.homography    -> list[list[float]] | None  (3x3 matrix)
# tag_frame.fps           -> float

for tag in tag_frame.tags:
    print(tag.id)                 # int
    print(tag.center_px)          # (float, float) pixel center
    print(tag.world_xy)           # (float, float) cm — the robot's CENTRE if this
                                  #   tag is registered (see "mounted on a robot?"
                                  #   above), otherwise the tag itself
    print(tag.yaw)                # float, radians
    print(tag.speed_world)        # float, cm/s
    print(tag.in_playfield)       # bool
```

### Tag Stream (high-frequency)

Use the tag stream for control loops.  Each `read()` blocks until the
next published frame (rate-limited to `max_hz`, default 20).

```python
tag_stream = dc.get_tag_stream(cam, max_hz=30)

for msg in tag_stream:
    if isinstance(msg, TagFrame):       # aprilcam.client.models.TagFrame
        for tag in msg.tags:
            x, y = tag.world_xy or (0, 0)
            # ... control logic ...
    # msg may also be an OverlayFrame (if another process published one)
    # — just ignore those in a consumer-only loop.

tag_stream.close()
```

### Image Stream

```python
image_stream = dc.get_image_stream(cam, max_hz=30)

for frame in image_stream:             # frame is np.ndarray BGR
    cv2.imshow("live", frame)
    if cv2.waitKey(1) == ord("q"):
        break

image_stream.close()
```

---

## Live Overlay — Push Annotations at 5-50 Hz

`publish_overlay` broadcasts graphical elements to every `aprilcam view`
window that is subscribed to that camera's tag stream.  The view drops
any overlay whose TTL has expired — set TTL shorter than your update
period to ensure stale data disappears automatically.

### Element types

All coordinates are **world cm** (same space as `tag.world_xy`).
Colors are `[R, G, B]` each 0-255.  Thickness is pixels; `-1` = filled.

| type | params | description |
|------|--------|-------------|
| `"arc"` | `[cx, cy, radius, start_deg, end_deg]` | Ellipse arc (handles non-square homography) |
| `"arrow"` | `[x1, y1, x2, y2]` | Arrow from tail to head |
| `"point"` | `[x, y, radius_cm]` | Circle at world position |
| `"polyline"` | `[x0, y0, x1, y1, …]` | Open polyline |
| `"text"` | `[x, y]` or `[x, y, font_scale]` | Text label at world position; set `"text"` key in element dict |
| `"rect"` | `[x1, y1, x2, y2]` | Rectangle; `thickness=-1` fills |
| `"polygon"` | `[x0, y0, x1, y1, …]` | Closed polygon; `thickness=-1` fills |

### Robot control-loop pattern

```python
import time
from aprilcam.config import Config
from aprilcam.client.control import DaemonControl

config = Config.load()
dc = DaemonControl.connect_default(config)
cam = dc.list_cameras()[0]

UPDATE_HZ = 10
TTL = 0.3   # slightly longer than one period so the view never sees a gap

try:
    while True:
        tag_frame = dc.get_tags(cam)

        # --- your control logic here ---
        robot_tag = next((t for t in tag_frame.tags if t.id == 7), None)
        if robot_tag and robot_tag.world_xy:
            rx, ry = robot_tag.world_xy
            heading = robot_tag.yaw

            import math
            arrow_len = 12.0
            ax = rx + arrow_len * math.cos(heading)
            ay = ry + arrow_len * math.sin(heading)
            lookahead_r = 25.0

            dc.publish_overlay(cam, [
                # robot body
                {"type": "point",
                 "params": [rx, ry, 5.0],
                 "color": [60, 120, 255], "thickness": -1},
                # heading arrow
                {"type": "arrow",
                 "params": [rx, ry, ax, ay],
                 "color": [240, 240, 240], "thickness": 3},
                {"type": "text", "params": [rx, ry], "text": f"tag {robot_tag.id}", "color": [255, 230, 0]},
                # pure-pursuit lookahead arc
                {"type": "arc",
                 "params": [rx, ry, lookahead_r,
                            math.degrees(heading) - 90,
                            math.degrees(heading) + 90],
                 "color": [0, 220, 60], "thickness": 2},
            ], ttl=TTL)

        time.sleep(1.0 / UPDATE_HZ)

finally:
    dc.publish_overlay(cam, [], ttl=0)   # clear immediately on exit
    dc.close()
```

### Clear the overlay immediately

```python
dc.publish_overlay(cam, [], ttl=0)
```

---

## Persistent Paths — Write to paths.json

Persistent paths (waypoint sequences shown in the live view) are stored
in a `paths.json` file inside the camera's data directory.  Unlike
overlays, they persist across daemon restarts and do not expire.

**Via the MCP tools** (recommended for AI agents): `create_path`,
`delete_path`, `clear_paths`, `list_paths`.

**Via direct file write** (recommended for robot programs or scripts):

```python
import json, os
from pathlib import Path

PATHS_FILE = Path("data/aprilcam/cameras/arducam-ov9782-usb-camera/paths.json")

def write_paths(paths: list[dict]) -> None:
    tmp = PATHS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(paths))
    os.replace(tmp, PATHS_FILE)    # atomic swap — live view picks it up within ~33ms

# Example: one path with three waypoints
write_paths([{
    "path_id": "path_000",
    "playfield_id": "arducam-ov9782-usb-camera",
    "waypoints": [
        {"x": 30, "y": 20, "size_cm": 4, "symbol": "filled_circle",
         "symbol_color": [0, 200, 80], "line_color": [0, 200, 80]},
        {"x": 90, "y": 20, "size_cm": 4, "symbol": "filled_circle",
         "symbol_color": [0, 200, 80], "line_color": [0, 200, 80]},
        {"x": 60, "y": 65, "size_cm": 4, "symbol": "filled_circle",
         "symbol_color": [0, 200, 80], "line_color": [0, 200, 80]},
    ],
}])

# Remove all paths
write_paths([])
```

Valid `symbol` values: `square`, `filled_square`, `circle`, `filled_circle`,
`triangle`, `filled_triangle`, `x`, `none`.

---

## TagStreamConsumer — Overlay-aware iteration

`TagStreamConsumer.read()` returns either a `TagFrame` (Pydantic model)
or an `OverlayFrame` (proto message) from the multiplexed socket.  In a
consumer-only loop you can ignore `OverlayFrame` messages:

```python
from aprilcam.client.models import TagFrame
from aprilcam.client.stream import TagStreamConsumer

stream = dc.get_tag_stream(cam)
for msg in stream:
    if isinstance(msg, TagFrame):
        # handle tag data
        pass
```

---

## Configuration

`Config.load()` searches for settings in this order (highest priority first):

| Source | Example |
|--------|---------|
| Environment variable | `APRILCAM_DATA_DIR=/mnt/robot/data` |
| `.env` file (walk up from CWD) | `APRILCAM_SOCKET_DIR=/run/aprilcam` |
| `.aprilcam` dotfile (walk up from CWD) | `APRILCAM_LOG_LEVEL=DEBUG` |
| `~/.aprilcam` user dotfile | — |

Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `APRILCAM_SOCKET_DIR` | `/tmp/aprilcam/` | gRPC control socket and stream sockets |
| `APRILCAM_DATA_DIR` | `./data/aprilcam/` | Camera data, calibration, paths.json |
| `APRILCAM_LOG_LEVEL` | `INFO` | Daemon log verbosity |

---

## Demo Script

`tests/demo_overlay.py` in the repository walks through all features:
triangle path → cross path → static overlay scene → animated robot orbit
→ cleanup.  Run it with a live camera open in `aprilcam view`.

---

## Coordinate System

All world coordinates use a **playfield-centred ENU convention**:

- **Origin**: the center of the playfield (AprilTag A1 = position 0, 0).
- **X axis**: East (right when standing at the south edge looking north).
- **Y axis**: North (toward the far edge of the field).
- **Units**: centimetres.
- **Yaw / heading**: measured in radians, 0 = East, π/2 = North
  (standard mathematical / ENU convention).

For the standard 134.3 × 89.3 cm main playfield the field corners are
approximately ±67 cm (east–west) and ±44.65 cm (north–south) from the
origin.

---

## Camera Configuration and Calibration (MCP Workflow)

Calibration maps camera pixels to real-world cm.  Anything that uses
`tag.world_xy` requires a calibrated playfield.  All of this is done through
MCP tools — `camera_id` and `playfield_id` are opaque handles, never file
paths, and you never read or write files yourself.

### Get world coordinates from an already-calibrated camera

```json
// 1. open the camera
{ "tool": "open_camera", "arguments": { "pattern": "<name>" } }
// -> { "camera_id": "...", "playfield_id": "...", "playfield_name": "..." }
//    playfield_id / playfield_name are present only if already configured.

// 2. if you did NOT get a playfield_id, build one from the stored calibration
{ "tool": "create_playfield", "arguments": { "camera_id": "<camera_id>" } }
// -> { "playfield_id": "...", "calibrated": true }
```

Then use the `playfield_id` as the `source_id` for `stream_tags` / `get_tags`
/ `get_objects` / `where`; `world_xy` (cm) is populated.  A `create_playfield`
result of `"calibrated": false` means the camera has not been calibrated yet.

### Link a camera to a playfield, then calibrate (setup)

If a camera is not yet linked to a playfield, link it then calibrate — both
via MCP tools, no files:

```json
{ "tool": "set_camera_playfield",
  "arguments": { "camera_id": "<camera_id>", "playfield_name": "main-playfield" } }
{ "tool": "calibrate_playfield",
  "arguments": { "playfield_id": "<playfield_id>" } }
```

`calibrate_playfield` takes **no width/height** — dimensions come from the
named playfield definition.  Discover the available names with
`list_playfields`.

### Stale calibration warning

`open_camera` may return `calibration_stale: true` when the stored
calibration was made against a different playfield definition than the one
currently configured (for example, after the playfield definition's corner
positions were revised).  In that case:

```json
{
  "camera_id": "cam_0",
  "playfield_id": "pf_0",
  "calibration_stale": true
}
```

Fix: call `calibrate_playfield(playfield_id=...)` to re-calibrate with the
current definition.  Until recalibrated, world coordinates will be computed
from the old homography and may be inaccurate.

### Getting world coordinates (read this)

World positions (cm, A1-centred: origin at AprilTag 1, +x east, +y north) come
only from a **calibrated playfield**:

1. Call `open_camera(...)` in your session first — the server holds no state
   across restarts and auto-opens nothing. It returns `playfield_id` and
   `playfield_name` when the camera is configured + calibrated.
2. Pass the **`playfield_id`** (e.g. `pf_<camera>`) as the `source_id` to
   `stream_tags` / `get_tags` / `get_objects` / `where`. Tag and object
   `world_xy` then populate. Passing the bare `camera_id` also works — the
   server auto-resolves the camera's playfield — but `playfield_id` is
   canonical. With no calibrated playfield, `world_xy` is `null` and only
   pixel coordinates are returned.

### MCP camera and paths tools

| Tool | Purpose |
|------|---------|
| `list_cameras()` | Enumerate available cameras |
| `open_camera(index?, pattern?)` | Open a camera; returns `camera_id`, `playfield_id`, `playfield_name` if configured |
| `close_camera(camera_id)` | Release a camera |
| `set_camera_playfield(camera_id, playfield_name)` | Link a camera to a named playfield definition |
| `calibrate_playfield(playfield_id)` | Calibrate using the linked playfield def (no width/height needed) |
| `list_playfields()` | List all named playfield definitions (`name`, `display_name`, dimensions) |
| `get_playfield(name?)` | Return a playfield's entire structure — all AprilTags, ArUco tags, rectangles, dots, dimensions, origin (whole map, not a search) |
| `where(query, source_id?)` | Search the playfield map for one feature by natural language |
| `create_path(playfield_id, waypoints)` | Add a persistent waypoint path |
| `delete_path(path_id)` | Remove a path |
| `clear_paths(playfield_id)` | Remove all paths for a playfield |
| `list_paths(playfield_id)` | List paths for a playfield |
