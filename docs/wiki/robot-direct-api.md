---
title: Robot Direct API
blurb: The Python client library for robot control loops — discover and connect to the daemon, read tag positions at loop rate, subscribe to streams, and draw shared annotations.
order: 4
updated: 2026-08-23
tags: [api, grpc, robot, python, client]
---

# AprilCam Robot Direct API

**This guide is for robot programs that need high-frequency access to
tag positions.** If you are an AI agent working interactively, use the
MCP tools instead ([mcp-server.md](mcp-server.md)). A control loop
running at 5–50 Hz uses the Python client library described here — it
talks directly to the daemon over gRPC and stream sockets, bypassing
the MCP layer entirely.

The client library
([`src/aprilcam/client/`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/))
is **the only sanctioned way to talk to a daemon** — the CLI, the MCP
server, and robot programs all sit on it — and it contains **no
OpenCV**: the daemon is the sole vision authority. The wire-level
contract underneath is [daemon-interface.md](daemon-interface.md);
running the daemon itself is [daemon.md](daemon.md).

Two conventions are load-bearing everywhere below (from
[`src/aprilcam/types.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/types.py)):
the **world frame** is A1-centred, `+x` east, `+y` north, units **cm**;
**angles** are radians, 0 = east, counter-clockwise positive. `yaw_rad`
is where a tag *faces*; `heading_rad` is where it is *moving* (`atan2`
of velocity) — a robot driving backwards has them 180° apart, which is
signal, not error.

---

## Quick Start

```python
from aprilcam.client.discovery import Discovery

daemon = Discovery().connect()          # local daemon, over its unix socket

frame = daemon.get_tags("arducam-ov9782-usb-camera")
for record in frame.tags:
    print(record.tag.family.value, record.tag.number, record.world)
```

`connect()` returns a `Daemon` — the control-plane interface defined in
[`src/aprilcam/daemon/interfaces.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/daemon/interfaces.py),
implemented client-side by `GrpcDaemon`
([`src/aprilcam/client/daemon_client.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/daemon_client.py)).
Cameras are addressed by **name** (from `daemon.list_cameras()` or
`aprilcam camera list`); there is no default camera.

---

## Connecting and Discovery

`Discovery`
([`src/aprilcam/client/discovery.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/discovery.py))
has two jobs: finding daemons and connecting to one.

```python
from aprilcam.client.discovery import Discovery

discovery = Discovery()

daemon = discovery.connect()                    # local: unix socket
daemon = discovery.connect("pi-field.local")    # remote: TCP, default port 5280
daemon = discovery.connect("192.168.1.20:5280") # remote: explicit port
```

`connect(host)` is a **direct connect** — no discovery probe first. An
explicit `host` argument beats the `APRILCAM_DAEMON_HOST` environment
variable, which beats the local default (the daemon's unix socket). It
never spawns a daemon; if nothing answers within the connect timeout it
raises `aprilcam.errors.ConnectionFailed`.

`find_all()` is the network view — the local daemon (probed via its
lock file and socket, no network needed) plus every daemon advertising
`_aprilcam._tcp.local.` over mDNS:

```python
for info in discovery.find_all():        # tuple[DaemonInfo, ...]
    print(info.host,
          "local" if info.is_local else "remote",
          "ok" if info.reachable else "advertising but not answering")
```

Finding nothing is an **empty tuple, not an error**; discovery
*failing* (multicast blocked) raises `DiscoveryFailed` instead. A
remote `DaemonInfo.host` embeds its port as `"ip:port"` and can be
passed straight to `connect()`. If something answers but speaks the old
v1 protocol, calls raise `DaemonVersionMismatch` — "something answered,
but not as a compatible daemon."

---

## Reading Tags

Three camera-addressed queries, defined on `Daemon`:

```python
from aprilcam.types import TagFamily, TagId

CAM = "arducam-ov9782-usb-camera"

# One tag. Family + number, never a bare id — AprilTag 1 and ArUco 1 differ.
record = daemon.get_tag(CAM, TagId(TagFamily.APRILTAG, 7))
if record is None:
    ...   # not currently detected — never a fake record at (0, 0)
elif record.world is None:
    ...   # camera not calibrated — record.pixel is all you get
else:
    x, y = record.world.x, record.world.y   # cm, A1-centred
    facing = record.yaw_rad                 # orientation (0 = east, CCW+)

# Every currently detected tag, no count or limit:
frame = daemon.get_tags(CAM)                 # -> TagFrame

# Recent full-rate history from the daemon's ring buffer (~10 s),
# optionally filtered to one tag:
history = daemon.get_tag_history(CAM, frames=60)   # -> tuple[TagFrame, ...]
```

### The models

All returned values are frozen dataclasses from
[`src/aprilcam/types.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/types.py)
(built from the wire messages by
[`src/aprilcam/client/models.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/models.py)):

| `TagRecord` field | type | meaning |
|---|---|---|
| `tag` | `TagId` | family + number |
| `world` | `WorldPoint \| None` | position in cm; `None` on an uncalibrated camera |
| `yaw_rad` | `float \| None` | facing direction (mount-corrected) |
| `heading_rad` | `float \| None` | motion direction from velocity |
| `world_velocity`, `speed` | optional | velocity components and magnitude, cm/s |
| `pixel` | `PixelPoint \| None` | diagnostic pixel position |
| `world_corners`, `pixel_corners` | 4-tuples | observed tag outline, for drawing |
| `timestamp` | `float` | capture time |

A `TagFrame` bundles `tags`, `timestamp`, `frame_index`, and
`calibration_stale` — the in-band flag that positions have become
suspect. **Mount corrections are already applied**: with a registered
mount (next section), `world` and `yaw_rad` report the *robot*, not the
tag. `world_corners`/`pixel_corners` stay uncorrected — they outline
the tag as physically seen, for drawing.

---

## Tag Mounted on a Robot? Register Its Mount — Every Session

```python
from aprilcam.types import MountParameters, TagFamily, TagId

warning = daemon.register_tag(
    TagId(TagFamily.APRILTAG, 7),
    MountParameters(
        mount_x=12.0,       # cm, robot frame: +x forward, +y left of your reference point
        mount_z=18.0,       # cm above the field plane — drives parallax correction
        mount_yaw_rad=0.0,  # tag facing relative to the robot's forward
        size_cm=8.0,        # optional tag edge length
    ),
)
if warning:
    print(warning)   # e.g. mount_z accepted but inert — no camera located yet

daemon.list_tag_parameters()   # dict[TagId, MountRegistration]
daemon.unregister_tag(TagId(TagFamily.APRILTAG, 7))
```

Registrations are **runtime-only** — in memory, gone on daemon restart.
Register at the start of *every* session, or after a restart you
silently read the raw tag's position instead of your robot's. The
parallax correction (`mount_z`) additionally needs the observing
camera's own solved position (`aprilcam camera locate`); until then
`register_tag` returns a warning and `list_tag_parameters` reports
`mount_z_applied=False`. Shell equivalent:
`aprilcam tags mount register apriltag 7 --x 12 --z 18 --size 8`.

---

## Streams

For control loops, subscribe rather than poll. `stream_tags` /
`stream_images` return `TagStream` / `ImageStream` subscriptions
([`src/aprilcam/client/streams.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/streams.py))
— a length-prefixed protobuf socket: unix locally, TCP to a remote
daemon, chosen automatically.

```python
stream = daemon.stream_tags(CAM)
try:
    for frame in stream:          # frame is aprilcam.types.TagFrame
        if frame.calibration_stale:
            ...                   # told in-band, not silently wrong
        for record in frame.tags:
            ...                   # control logic at stream rate
finally:
    stream.close()
```

`daemon.stream_images(CAM, deskewed=True)` is the same shape, yielding
`ImageFrame`s. Semantics worth knowing:

* **Change-driven, rate-capped, with a heartbeat** — a quiet field stays
  distinguishable from a dead stream. **Backpressure is silent drop**:
  a slow consumer loses frames, never stalls the pipeline.
* **`close()` unsubscribes.** When the last subscriber leaves, the
  daemon idles that product — holding a stream open costs it real work.
  A clean daemon-side close simply ends iteration; genuine socket
  errors propagate.
* Iterate with `for frame in stream:`. A throwaway
  `next(iter(stream))` closes the socket after one frame — keep the
  iterator (`it = iter(stream)`) if you call `next()` manually.

One-shot `get_tags`/`get_tag` polling remains fine for low-frequency
callers; polling faster than the camera's frame rate gains nothing.

---

## Drawing on the Playfield — Annotations

Annotations are **world-coordinate drawings held by the daemon**,
visible to every client watching the same playfield (viewers, agents,
other robots): playfield-addressed, keyed `(layer, id)`,
last-write-wins, and — like mount registrations — **runtime-only**,
gone on daemon restart and re-drawn by clients on reconnect.

```python
from aprilcam.types import Annotation, Marker, Style, WorldPoint

PF = "main-playfield"

daemon.put_annotation(PF, Annotation(
    id="goal", layer="strategy",
    shape=Marker(at=WorldPoint(x=60.0, y=30.0), label="goal"),
    style=Style(color="#00C850", line_width=3),
    ttl_seconds=None,                    # optional expiry for crash-proof overlays
))

daemon.replace_layer(PF, "robot-7", (...,))  # atomically swap a whole layer
daemon.get_annotations(PF)                   # -> AnnotationSet
daemon.remove_annotation(PF, "strategy", "goal")
daemon.clear_annotations(PF, "robot-7")      # omit layer to clear every layer
```

Shapes are `Marker`, `PathShape` (open polyline), `Polygon` (closed),
`Circle`, and `Text` — all geometry in world cm. `Style` is
presentation only: `color` is any Pillow colour string (default
`"yellow"`), `line_width` is pixels on the rendered image.

### A control-loop pattern

```python
from aprilcam.client.discovery import Discovery
from aprilcam.types import Annotation, Circle, MountParameters, Style, TagFamily, TagId

CAM, PF = "arducam-ov9782-usb-camera", "main-playfield"
ROBOT = TagId(TagFamily.APRILTAG, 7)

daemon = Discovery().connect()
daemon.register_tag(ROBOT, MountParameters(mount_z=18.0, size_cm=8.0))  # every session

stream = daemon.stream_tags(CAM)
try:
    for frame in stream:
        record = next((r for r in frame.tags if r.tag == ROBOT), None)
        if record is None or record.world is None:
            continue
        # ... control logic on record.world / record.yaw_rad ...
        daemon.replace_layer(PF, "robot-7", (
            Annotation(id="body", layer="robot-7", ttl_seconds=1.0,
                       shape=Circle(center=record.world, radius_cm=10.0),
                       style=Style(color="#3C78FF")),
        ))
finally:
    daemon.clear_annotations(PF, "robot-7")
    stream.close()
```

---

## Frames and Client-Side Rendering

`get_frame` returns one encoded frame plus everything needed to
interpret it:

```python
frame = daemon.get_frame(CAM, deskewed=True)   # encoding="png" for lossless
frame.data                                     # encoded image bytes
frame.px_per_cm, frame.world_extent            # deskewed frames only
frame.tags                                     # embedded TagFrame (detection running)
frame.annotations                              # embedded AnnotationSet (deskewed only)
```

A deskewed frame spans exactly the field rectangle — `px_per_cm` plus
that anchor is the whole world→pixel conversion. A deskewed request on
an uncalibrated camera fails explicitly, never silently substituting a
raw frame.

`Renderer`
([`src/aprilcam/client/renderer.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/renderer.py))
rasterizes a frame's embedded overlays cv2-free (Pillow): tag boundary
quads with the green front "hat", id labels, a crosshair at each
corrected world position, and the embedded annotations.

```python
from aprilcam.client.renderer import Renderer

png = Renderer().render(frame, layers=("strategy",))   # layers=None draws all
```

`extra=` accepts local `Annotation`s to draw without sending them to
the daemon — ad-hoc debug overlays.

---

## Daemon Lifecycle

`DaemonLifecycle`
([`src/aprilcam/client/lifecycle.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/client/lifecycle.py))
starts, stops, and checks on the **local** daemon (a remote daemon is
started over SSH, not by this API):

```python
from aprilcam.client.lifecycle import DaemonLifecycle

lifecycle = DaemonLifecycle()
status = lifecycle.status()            # -> DaemonStatus
if not status.running:
    lifecycle.start()                  # spawns python -m aprilcam.daemon, waits until ready
```

`start()` pre-probes the daemon's singleton lock and raises
`AlreadyRunning` naming the holder; when it returns, the daemon is
genuinely ready (cameras enumerated, control plane answering).
`DaemonStatus` reports `running`, the camera list, and the resolved
`config_dir`/`state_dir`; `daemon.status()` returns the same shape over
RPC.

---

## Errors

Every call raises a typed exception from
[`src/aprilcam/errors.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/errors.py)
— reconstructed from the wire, never a bare string to parse:
`UnknownCamera`, `UnknownPlayfield`, `CameraNotPresent`,
`NotCalibrated`, `UnsupportedSetting`, `AlreadyRunning`,
`ConnectionFailed`, `DiscoveryFailed`, and friends. Catch the base
`aprilcam.errors.AprilCamError` when your loop only needs "did that
call succeed."

---

## Packaged Guides

The same conventions ship inside the package, readable with **no
daemon running**: `Guides().get("robot")` (from
`aprilcam.client.guides`) returns the condensed in-package version of
this page, and `Guides().available()` lists `("agent", "robot")` — the
`agent` guide covers the MCP workflow
([mcp-server.md](mcp-server.md)). `daemon.get_guide("robot")` serves
byte-identical text over the wire; `aprilcam guide` prints it from a
shell.
