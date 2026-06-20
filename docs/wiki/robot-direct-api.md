---
title: Robot Direct API
blurb: High-frequency Python API for robot control loops — read tag positions and push live overlays directly over gRPC, bypassing the MCP layer.
order: 30
updated: 2026-06-20
tags: [api, grpc, robot, python]
---

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
cameras = dc.list_cameras()          # -> list[str]  open camera names
devices = dc.enumerate_cameras()     # -> list[CameraDevice(index, name, slug, enum)]
cam = dc.open_camera(index=1)        # -> str cam_name   (index = OS device index)
info = dc.get_camera_info(cam)       # -> CameraInfo(cam_name, calibrated, frame_size, fps)
frame = dc.capture_frame(cam)        # -> np.ndarray BGR (JPEG decoded)
dc.reload_calibration(cam)           # reload calibration.json from disk
dc.close_camera(cam)
dc.shutdown()                        # stop the daemon process
```

`open_camera(index=...)` is the **low-level OS device index** at this layer
(unlike the CLI and MCP `open_camera`, which take the persistent enumeration
number). To open by the stable enum, resolve it through `enumerate_cameras`:

```python
dev = next(d for d in dc.enumerate_cameras() if d.enum == 3)
cam = dc.open_camera(index=dev.index)
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
    print(tag.world_xy)           # (float, float) in cm, or None if uncalibrated
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

`Config.load()` reads `APRILCAM_*` environment variables, `.env`/`.aprilcam`
dotfiles (walking up from the CWD), and `/etc/aprilcam.env`, with an
auto-selected FHS or XDG directory layout. The full variable table and the
resolution order are in
**[Operating the Daemon → Configuration](daemon.md#configuration)**.

The one value a client must get right is `APRILCAM_SOCKET_DIR`: your program
and the daemon must resolve the **same** socket directory, or `connect_default`
won't find the daemon. When they share an environment this is automatic; set it
explicitly only when they don't.

---

## Demo Script

`tests/demo_overlay.py` in the repository walks through all features:
triangle path → cross path → static overlay scene → animated robot orbit
→ cleanup.  Run it with a live camera open in `aprilcam view`.
