---
title: Using the MCP Server
blurb: How an AI agent drives AprilCam over the Model Context Protocol — running the server, the golden path, the full tool catalog, and error behavior.
order: 3
updated: 2026-08-23
tags: [mcp, agent, tools]
---

# Using the AprilCam MCP Server

The MCP server (`aprilcam mcp`) exposes AprilCam to AI agents over the
**Model Context Protocol**: enumerate cameras, read tag positions in world
coordinates, fetch raw or deskewed frames, push camera and playfield
configuration, trigger calibration, register robot tag mounts, and draw
annotations on the shared field view — all through tools, addressing
cameras and playfields by **name**, never by device or file path.

The server is a **thin client over gRPC**. All vision (AprilTag/ArUco
detection, homography, deskew, calibration) runs inside the
[daemon](daemon.md); the MCP server forwards queries and results and does
no pixel processing — **OpenCV is never imported** in its environment, so
the base `pipx install aprilcam` is enough (see
[install tiers](overview.md#install-tiers)). The wire contract underneath
is [daemon-interface.md](daemon-interface.md).

## Running it

The server speaks stdio. Register it with your MCP client (e.g. a
`.mcp.json`); add `-d`/`--daemon HOST` to pin it to a daemon on another
host:

```json
{
  "mcpServers": {
    "aprilcam": { "command": "aprilcam", "args": ["mcp", "-d", "vidar.local"] }
  }
}
```

### How it finds the daemon

The server **starts unbound** — no daemon is contacted at startup, so
`aprilcam mcp` always starts cleanly whether or not a daemon is running
anywhere. Each tool call resolves a connection on demand, by precedence:
the call's own optional `daemon` parameter (a hostname, or `host:port` for
a non-default port; the default TCP port is 5280), then the `-d HOST` the
server was started with or the `APRILCAM_DAEMON_HOST` environment variable,
then the local daemon's Unix socket.

Connections are cached per target and dropped only when a call actually
fails — so a daemon restart mid-session costs exactly one failed tool call:
that call returns a connection error, the next one reconnects and succeeds,
and the server itself stays up throughout. If no daemon is reachable at
all, start one on the camera host (`aprilcam daemon start`) — see
[Operating the Daemon](daemon.md#run).

> The daemon owns **all** state — calibrations, playfield definitions,
> annotations, mount registrations. The MCP server keeps nothing beyond
> its connection cache; two agents pointed at the same daemon see the
> same field.

## The golden path

```text
1. get_guide()                  -> conventions and tool map (no daemon needed)
2. list_cameras()               -> find a camera; calibrated: true means ready
3. get_tags(camera) / get_tag(camera, family, number)   -> world positions
4. register_tag(...) if your tag rides on a robot        -> robot pose, not tag pose
5. draw_* / where / get_frame   -> annotate, look things up, see the field
```

`get_guide` is the one tool that never touches a daemon — it returns the
packaged agent guide
([`AGENT_GUIDE.md`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/AGENT_GUIDE.md))
and is always safe to call first, even before any daemon exists.

### Conventions to know

- **Coordinates** — world positions are **centimetres, A1-centred**:
  AprilTag 1 (the surveyed field centre) is the origin, `+x` east, `+y`
  north. Angles are radians, `0` = east, counter-clockwise positive.
  `yaw_rad` is the direction a tag's front **faces**; `heading_rad` is
  motion direction from velocity — a robot driving backwards has them
  180° apart, which is signal, not error.
- **Tag addressing** — always **family plus number**: `"apriltag"` or
  `"aruco"` and a non-negative integer. AprilTag 7 and ArUco 7 are
  different tags; family is never inferred from the number.
- **Uncalibrated cameras** still detect tags but cannot report world
  coordinates: when a record's `calibrated` flag is `false`, `world` is
  **always `null`** (never a fabricated `(0, 0)`) and `pixel` is the only
  position.
- **Per-session state** — mount registrations and annotations live only in
  the daemon's memory and **do not survive a daemon restart**. Re-register
  mounts and re-draw annotations at the start of every session that needs
  them; `list_tag_parameters` and `list_annotations` show what is held.

## Tool catalog

Every daemon-backed tool below also accepts an optional `daemon` parameter
(per-call target host, described above), omitted from the signatures for
brevity.

### Guide

| Tool | Purpose |
|------|---------|
| `get_guide(name?)` | The packaged usage guide — `"agent"` (default) or `"robot"`: world-frame conventions, tag addressing, bring-up order. Works with **no daemon running anywhere**; read it first. |

### Perception (read-only)

| Tool | Purpose |
|------|---------|
| `list_cameras()` | Every camera the daemon knows: `name`, `present`, `usable`, `playfield` link, `calibrated`, `calibration_stale`, stable persistent `number`. Absent-but-configured cameras are listed and flagged, not omitted. |
| `probe_cameras()` | Force a deep re-interrogation of every camera backend; the report names any backend it had to skip, with a reason. |
| `get_frame(camera, deskewed?, encoding?, format?)` | One frame — raw, or the deskewed top-down view when `deskewed` is true (requires calibration; no silent raw fallback). `encoding` is `"jpeg"` (default) or `"png"`; `format` below. |
| `get_tags(camera)` | Every tag currently detected: `world` position (when calibrated), `pixel`, `yaw_rad`, `heading_rad`, `world_velocity`, `speed`, corner quads. An empty field is an empty frame, not an error. |
| `get_tag(camera, family, number)` | One tag by identity; `null` when not currently detected — never a fabricated position. |
| `get_tag_history(camera, family?, number?, frames?)` | Recent per-frame history from the daemon's ring buffer (roughly the last 10 s), for computing or verifying trajectories. Give `family` and `number` together to filter to one tag, or neither for all; absences stay explicit as gaps. |
| `where(playfield, query)` | Where a **surveyed** feature is, by fuzzy name/colour/kind/cardinal match against the playfield definition (e.g. `"the orange dot"`). Answered from the definition, not vision — no calibration or camera needed, but never a live observation. Ambiguity returns every match; no match, an empty list. |

### Configuration & calibration

| Tool | Purpose |
|------|---------|
| `get_camera_config(camera)` | The live camera config document — what was last pushed or installed, never a hardware readback. |
| `set_camera_config(document)` | Push a whole camera config as a JSON object (never a file path), targeted by its own `"name"` field. Validated client-side before any RPC; a rejected push leaves the previous config untouched. |
| `reset_camera_config(camera)` | Discard any live override and re-apply the stored on-disk config — how an experiment ends without a daemon restart. |
| `get_playfield(name)` | The whole playfield definition, in the same round-trippable shape `set_playfield` accepts. |
| `set_playfield(document)` | Push a whole playfield definition as a JSON object, targeted by its own `"name"`. Rejected whole if malformed, with the problem named. |
| `list_playfields()` | Every playfield definition the daemon holds. |
| `calibrate(camera, estimate_intrinsics?)` | Calibrate against the camera's linked playfield. No partial success: either every required ArUco id matched and a homography solved, or the result names `missing_ids` (or a degenerate-geometry `reason`) and any prior-good calibration is untouched. `estimate_intrinsics` also fits the lens model. |
| `get_calibration(camera)` | The current calibration (`stale`, `calibrated_at`, `playfield`), or `null` if never calibrated. A stale calibration is returned, not hidden. |

### Mobile tags (robot mounts)

| Tool | Purpose |
|------|---------|
| `register_tag(family, number, mount_x?, mount_y?, mount_z?, mount_yaw_rad?, size_cm?)` | Describe how a tag is mounted on its robot so queries report **the robot's** pose, not the tag's. Offsets are in the robot's own frame (`+x` forward, `+y` left), in cm. Upserts by tag; in-memory only. Returns a warning string when a nonzero `mount_z` cannot currently apply (below). |
| `unregister_tag(family, number)` | Forget a mount; the tag reports as itself again. Unregistering an unknown tag is a no-op, not an error. |
| `list_tag_parameters()` | Every registered mount, each with a `mount_z_applied` flag telling you whether the height correction is actually in effect. |

A nonzero `mount_z` (tag height, driving parallax correction) needs the
observing camera to have been **located**, not merely calibrated — and
locating (`aprilcam camera locate`) is a hands-on operator task with no
MCP tool, by design. On a calibrated-but-unlocated camera the registration
succeeds but the height correction is silently inert: check the returned
warning or `mount_z_applied`, and ask your operator to locate the camera.

### Annotations (drawing)

Shared drawings on a playfield, visible to every client (viewers, other
agents, robots via the [direct API](robot-direct-api.md)). All coordinates
are world-cm; the daemon stores shapes, renderers draw them. Every `draw_*`
tool upserts by `(layer, id)` — last write wins — and takes optional
`color`, `line_width`, and `ttl_seconds` (omitted means no expiry). An
unknown playfield is rejected with an error naming the playfields the
daemon does hold.

| Tool | Purpose |
|------|---------|
| `draw_marker(playfield, layer, id, x, y, label?)` | A labelled point of interest — a goal, an obstacle. |
| `draw_path(playfield, layer, id, points)` | An open polyline through `[x, y]` pairs — a route or planned motion. |
| `draw_polygon(playfield, layer, id, points)` | A closed region — a zone, a keep-out area. |
| `draw_circle(playfield, layer, id, center_x, center_y, radius_cm)` | A circular region. |
| `draw_text(playfield, layer, id, x, y, text)` | A free-standing label. |
| `remove_annotation(playfield, layer, id)` | Delete one annotation; removing an unknown id is a no-op. |
| `clear_layer(playfield, layer?)` | Clear one layer, or everything on the playfield when `layer` is omitted; clearing an empty layer is a no-op. |
| `list_annotations(playfield, layer?)` | The current annotation set (with a `revision` counter) — how one agent reads another's plan without pixels. Empty at revision 0 after a daemon restart. |

## Image return format

`get_frame` — the one image-returning tool — takes a `format` parameter,
chosen per request: **`"base64"`** (default) returns the encoded image
inline as MCP image content, touching no disk; **`"file"`** writes the
bytes under a per-process temp directory (`aprilcam-mcp-*`) and returns
**only the path** — never both, and never a path you did not ask for.
Files accumulate for the life of the server process (no automatic
cleanup), and the bytes pass through from the daemon unmodified — the MCP
server never decodes or re-encodes the image.

## Error behavior

A failed call comes back as a normal MCP tool-level error (`isError: true`)
with a plain-text message — never a crashed server, never a leaked
traceback. AprilCam's typed errors pass through with their own agent-legible
message naming what was requested and what is available: unknown
camera/playfield errors list the names the daemon does know, `NotCalibrated`
and `NoPlayfieldLinked` name the missing precondition, `MalformedDocument`
names exactly what was wrong with a pushed config (checked client-side,
before any RPC), and a connection failure says no daemon was reachable.
Anything else becomes a generic `internal error: <type>`, with the traceback
logged server-side only.

Absence is generally **not** an error: an undetected tag is `null`, an
empty field or an unmatched `where` query is an empty list, and removing or
unregistering something that is not there is a no-op.

## A short agent workflow

```text
get_guide()                    # conventions and tool map; no daemon needed
list_cameras()                 # -> "field-cam": present, usable, calibrated

register_tag(family="apriltag", number=7, mount_x=3.5)
                               # our robot's tag; re-register every session
get_tag(camera="field-cam", family="apriltag", number=7)
   # -> { world: { x: -42.1, y: 17.3 }, yaw_rad: 1.58, speed: 0.0, ... }

where(playfield="table-1", query="the blue dot")
   # -> [{ name: "blue-dot", ... surveyed at (30.0, -25.0) }]

draw_path(playfield="table-1", layer="plan", id="route-7",
          points=[[-42.1, 17.3], [-10, 0], [30, -25]], color="cyan")
   # plan now visible to every viewer and robot on this field

get_frame(camera="field-cam", deskewed=true)
   # top-down JPEG, inline — visually confirm the field matches the plan
```

## Robot hand-off

MCP is for reasoning and orchestration, not for a control loop. A robot
program that needs tag poses at 5–50 Hz should use the
[Robot Direct API](robot-direct-api.md) — the Python client library that
talks to the daemon directly over gRPC (`get_guide(name="robot")` covers
it). Both paths hit the same daemon and see the same field, world frame,
and annotations.

## Source

The server lives in
[`src/aprilcam/mcp/server.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/mcp/server.py),
with the tool groups under
[`src/aprilcam/mcp/tools/`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/mcp/tools/)
(`perception.py`, `configuration.py`, `mobile_tags.py`, `annotations.py`)
and the CLI entry point in
[`src/aprilcam/cli/mcp.py`](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/cli/mcp.py).
Every tool description above comes from the registered tool's docstring.
