# AprilCam Robot API Guide

This guide is for **robot control programs** — code that runs a loop at
5-50 Hz and needs direct, low-latency access to tag positions. If you
are an AI agent working interactively through tool calls, read the
**`agent`** guide instead (`aprilcam guide`, or `Guides().get("agent")`
in Python) — it covers the same conventions with a workflow-first
framing and the full command table.

This guide assumes you already know the world frame (A1-centred, `+x`
east, `+y` north, cm, heading 0 = east, counter-clockwise positive) and
tag addressing (family + number, never a bare id) — see the `agent`
guide's sections 1-2 if you have not read them yet.

## Connecting

```python
from aprilcam.client.discovery import Discovery

daemon = Discovery().connect()                # local daemon, unix socket
# daemon = Discovery().connect("192.168.1.20")  # a named remote daemon
```

`Discovery().connect(host)` is a **direct connect** — a unix socket
when `host` is `None`, plain TCP otherwise — it does not run a
daemon-list probe first. Precedence for an unnamed target: explicit
`host` argument beats the `APRILCAM_DAEMON_HOST` environment variable
beats the local default.

## Reading Tags

```python
from aprilcam.types import TagFamily, TagId

record = daemon.get_tag("arducam-ov9782-usb-camera", TagId(TagFamily.APRILTAG, 7))
if record is None:
    ...      # not currently detected -- distinct from "at (0, 0)"
elif record.world is None:
    ...      # camera not calibrated -- record.pixel is all you get
else:
    x, y = record.world.x, record.world.y   # cm, A1-centred
    heading = record.heading_rad             # atan2 of velocity; 0 = east, CCW+

# Every currently-detected tag on a camera, unbounded:
frame = daemon.get_tags("arducam-ov9782-usb-camera")
for record in frame.tags:
    ...
```

`get_tag` returns `None` for "not currently detected" — never a record
at `(0, 0)`, which would be indistinguishable from a real position
there. `get_tags` returns every currently-detected tag with no count or
limit; an empty field returns an empty list, not an error.

## Is Your Tag Mounted on a Robot? Register Its Mount

`record.world` is the **tag's** position by default. If the tag is
mounted on a robot — elevated above the field plane, offset from the
robot's own reference point — register its mount parameters once per
session and every subsequent report for that tag becomes the
**robot's** position and heading instead of the raw tag's:

```python
from aprilcam.types import MountParameters

daemon.register_tag(
    TagId(TagFamily.APRILTAG, 7),
    MountParameters(
        mount_x=12.0,       # cm, robot's own frame: +x FORWARD of the reference point
        mount_y=0.0,        # cm, robot's own frame: +y LEFT of the reference point
        mount_z=18.0,       # cm, tag height above the field plane -- drives parallax correction
        mount_yaw_rad=0.0,  # radians, tag facing relative to the robot's forward
        size_cm=8.0,        # optional -- tag edge length; no daemon-wide default exists
    ),
)
```

The reference point (where `mount_x = mount_y = mount_z = 0`) is
**your choice, not something the daemon mandates** — the daemon has no
way to inspect your robot's physical geometry. Centre of rotation is a
sensible default if you have no other reason to pick something else,
but whichever point you choose, every offset above is measured from
it, in the robot's own frame, rotating with `mount_yaw_rad` as the
robot turns in place.

Equivalent from a shell, for one-off registration without writing
Python:

```
aprilcam tags mount register apriltag 7 --x 12 --z 18 --yaw-deg 0 --size 8
aprilcam tags mount list
aprilcam tags mount clear apriltag 7
```

`mount_z` nonzero requires a calibrated camera — registering it against
an uncalibrated one is rejected rather than silently accepted and
ignored.

### This registration does not survive a daemon restart

Mount parameters are **runtime-only**: held in memory, gone the moment
the daemon restarts — same as annotations, below. **Register at the
start of every session**, not once ever. A robot program that connects,
skips this call because "I already registered it last week," and then
reads `record.world` after a daemon restart silently gets the *raw
tag's* position back instead of its own — no error, just a
plausible-looking wrong number that will not announce itself. If your
control loop depends on `register_tag` having been called, call it
every time you connect, before you trust a reported position.

## Polling

There is no dedicated tag-streaming RPC yet (`stream_tags` /
`stream_images` are a later sprint) — poll `get_tag`/`get_tags` from
your own loop. Each call is a single round trip against the daemon's
latest detection; calling faster than your camera's own frame rate
gains nothing.

## Drawing on the Playfield

Annotations (`daemon.put_annotation`) are world-coordinate drawings —
markers, paths, polygons, circles, text — visible to every other
client watching the same playfield: a viewer, another robot, an agent.
Like mount registrations, **annotations are runtime-only and do not
survive a daemon restart** — re-draw them at the start of every session
that wants them visible, not just once. (`aprilcam playfield
annotations <name>` lists or clears the current set from a shell;
authoring one is currently a Python-client call, not yet a CLI verb.)

## Configuration

Camera and playfield configuration documents are pushed over RPC
(`daemon.set_camera_config`/`daemon.set_playfield`, or the equivalent
`aprilcam camera config <file>`/`aprilcam playfield config <file>` from
a shell) and take effect immediately — but a **pushed** config is also
in-memory only. It is discarded on daemon restart; the daemon reloads
from its on-disk configuration directory instead. To make an
experiment permanent, put the file where the daemon reads it (locally,
or `scp` to a remote daemon host) — pushing over RPC is for trying
something out, not for installing it.

## Errors

Every method above raises a typed exception from `aprilcam.errors` on
failure — `UnknownCamera`, `UnknownPlayfield`, `NotCalibrated`,
`UnsupportedSetting`, and so on — never a bare string to parse. Catch
`aprilcam.errors.AprilCamError` for any of them if your control loop
just needs to know "did that call succeed."

## Everything Else

See `aprilcam guide` (the `agent` guide) section 7 for the full command
table — every capability above is also reachable one-shot from a shell
script without writing Python, using the same `daemon`/`camera`/
`playfield`/`tags` command groups.

---
*Same source as the `agent` guide's own footnote: this text is
packaged with the software (`aprilcam.guides`) and read identically
with no daemon running, or over the wire from `Daemon.get_guide`.*
