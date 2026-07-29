# AprilCam System Specification (v2)

**Status:** DRAFT — for stakeholder review
**Date:** 2026-07-29
**Supersedes:** `.claude/rules/project-overview.md` (v1 requirements), `docs/design/overview.md`

This document re-specifies AprilCam based on (a) what the code actually does
today and (b) the stakeholder's direction for the second version. It is the
high-level system spec: what the pieces are, where they live, and what each
one does. Detailed per-subsystem design lives in co-located `DESIGN.md` files
(CLASI design-doc set); this document is the map.

---

## 1. What the system is

AprilCam gives robots, AI agents, and humans a shared, calibrated view of a
physical playfield. A camera looks down at a field marked with ArUco corner
fiducials and AprilTags; AprilCam turns that into:

- a **deskewed, metrically calibrated top-down image** of the playfield,
- a **live stream of tag records** (id, world position in cm, heading,
  velocity), corrected for lens distortion, parallax, and robot tag mounting,
- a **queryable playfield configuration** — the surveyed definition (tags,
  colored squares, dots) and camera configs, held by the daemon and served
  whole to every client, local or remote.

## 2. System principles

These are the rules that shape every boundary below.

1. **The daemon is the single camera owner and the single vision authority.**
   All OpenCV work — capture, detection, tracking, homography, deskew,
   calibration — happens exactly once, inside the daemon. No other component
   may open a camera or import cv2.
2. **Expensive shared processing happens once; clients are thin.** If two
   clients would each need the same derived product (the deskewed image, the
   tag stream), the daemon computes and publishes it. Clients subscribe,
   they do not recompute. *(New in v2: the daemon publishes the deskewed
   playfield stream; today every client warps the image itself.)*
3. **The daemon is the rendezvous point.** A daemon sits with its cameras
   (e.g., a Raspberry Pi over the field); local and remote clients all
   collaborate through it. Therefore configuration the clients must agree
   on — playfield definitions, camera configs — lives *in the daemon*:
   pushed to it as whole documents, held by it, served back by it. Files
   on any one client's disk are never shared state.
   **Whole-document semantics:** configuration moves as complete files.
   You push an entire playfield or camera config; you get an entire one
   back. There are no field-level edit RPCs.
4. **Calibration is robot-agnostic.** The calibration pipeline knows about
   the field (corners, surveyed marker positions, dimensions), never about
   robots. Robot-specific facts (tag height above the field, mount offset,
   mount yaw) are per-tag parameters registered at runtime by clients and
   applied by the daemon as a post-processing step.
5. **World coordinates first; pixels only at the edges.** The pipeline
   converts to world coordinates (cm, A1-centred) as early as possible,
   and everything downstream — tracking, storage, inter-process
   contracts, queries — is world coordinates. Positions are **never
   stored in pixels**. Pixel coordinates legitimately exist in exactly
   two places: (a) inside the daemon's detection internals, before the
   calibrated conversion; (b) at the final rendering step, where the
   *client* converts world → pixels for the specific image it is drawing
   on, using the published stream metadata (the deskewed image is
   uniform, so that conversion is exact). If a client scales the image,
   the conversion is its responsibility — which is why the daemon
   publishes world coordinates plus metadata, not pre-baked pixel
   positions. Tag records report world coordinates as the contract;
   raw-pixel fields are diagnostic-only (and the only representation
   available from an uncalibrated camera, explicitly flagged as such).
6. **Geometry conventions are sacrosanct.** The orientation of the world
   frame — which way is up, which way is east, where the positives are —
   was hard-won and must never be re-derived casually: A1-centred
   origin, +x east, +y north; on the deskewed image, north is up and
   east is right; the world origin shift is a pure subtraction on both
   axes, never a y-flip. Any change touching geometry must demonstrate
   these still hold (see §7) rather than assume them.
7. **Clients see handles and services, never file paths.** MCP clients in
   particular are never told to touch files; everything is a tool call.
8. **Config vs. state split is preserved.** `config_dir` holds hand-edited,
   developer-owned files; `data_dir` holds regenerable state. (Sprint 019.)

## 3. Process / deployment view

| Runtime piece | Kind | Required? | Notes |
|---|---|---|---|
| `aprilcamd` (daemon) | long-running process | yes | sole camera owner; gRPC control + binary streams; mDNS-advertised |
| `aprilcam mcp` | process per agent session (stdio) | for agents | pure daemon client: perception + configuration tools |
| `aprilcam view` | interactive GUI process | optional | thin subscriber: deskewed stream + tags; draws via the client drawing library |
| CLI commands | short-lived processes | optional | thin daemon clients (calibrate, cameras, tags, mobile, probe…) |
| ROS bridge | future process | no | subscribes to daemon streams, republishes as ROS topics |
| Web server | — | **removed in v2** | HTTP/WS interface dropped; ROS + MCP + view cover the consumers |

There is **one daemon** serving both perception and configuration. The
playfield is not a separate service and not a shared directory: playfield
and camera configuration documents are pushed to the daemon, held by it,
and served back to any client, local or remote (§4.5). The daemon is the
only rendezvous point, which is what makes a Pi-over-the-field with
remote users work.

There is **one MCP server**, not two. It fronts perception and
configuration through the same daemon connection. Internally the tool
domains are separate modules with no shared state.

Drawing on the playfield — paths, markers, overlays — is **not a service
at all** in v2: it is a client-side Python library (§4.6). There is no
multi-client collaboration on paths, no path storage in the daemon, and
no path tools in the MCP server.

**Cardinality:** a camera observes exactly one playfield; a playfield has
zero or more cameras. A camera's config names its playfield; a playfield
config reports the cameras currently linked to it.

## 4. Subsystem map

Each subsystem is a top-level directory under `src/aprilcam/` and carries a
co-located `DESIGN.md`. Current-code locations are noted where they differ.

### 4.1 `daemon/` — the perception daemon

**What it does:** owns cameras end-to-end. Per camera: a capture thread runs
the vision pipeline (detect/track AprilTags + ArUco, polygon filtering,
velocity, world-coordinate transform with A1-centred origin, parallax and
mobile-tag mount correction), then publishes:

**The internal image pipeline is invisible.** Where detection runs —
which resolution (full-resolution raw for subpixel corner accuracy),
which preprocessing stage, before or after warping — is a daemon-internal
choice that clients can never observe and the daemon is free to change.
The only things that cross the boundary after detection and deskew are
**world coordinates** and the deskewed image with its exact world↔pixel
metadata (principle 5).

- **gRPC control plane** (unix socket + TCP 5280, mDNS-advertised):
  camera lifecycle, one-shot queries (`CaptureFrame`, `GetTags`,
  `GetObjects`, `WhereIs`), stream endpoint allocation, calibration
  trigger *(new in v2 — see 4.4)*, mobile-tag registry, and the
  configuration service *(new in v2)*:
  - `SetPlayfield` / `GetPlayfield` — push/fetch a complete playfield
    definition document. The daemon persists it, uses it internally
    (calibration ground truth, `where` lookups, field dimensions), and
    serves it back verbatim plus derived info (which cameras are linked).
  - `SetCameraConfig` / `GetCameraConfig` / `ResetCameraConfig` —
    push/fetch a complete camera config document (camera kind, gains,
    shutter, brightness, UVC controls, playfield link, mount position);
    reset re-applies the stored config to the hardware. The daemon
    introspects what it can (dimensions, device name) and reports it;
    the pushed document owns everything adjustable.
  - Whole documents only, in both directions — no field-level edits.
- **Raw image stream** — JPEG frames, length-prefixed protobuf sockets.
- **Deskewed playfield stream** *(new in v2)* — the calibrated top-down
  image, computed once per frame in the daemon and published like the raw
  stream. **Sizing rule: fixed bounding box.** The field rectangle is
  scaled, aspect-preserved, to fit a maximum frame size — **default
  800×800**, overridable per playfield; the output is uniform-scale by
  construction, and
  the resulting px-per-cm is a *derived, reported* property, never a
  chosen constant. Frame dimensions and field cm dimensions ride in the
  stream metadata so clients convert deskewed-pixel ↔ world-cm exactly.
  Frame size is stable for the life of a stream. Clients that render the
  playfield subscribe to this instead of warping raw frames themselves.
- **Tag stream** — `TagFrame` records (change-driven, rate-capped,
  heartbeat). **World-first contract:** records carry world position
  (cm), world velocity, speed, and heading as the primary fields;
  raw-pixel fields are diagnostic-only and are the only representation
  for an uncalibrated camera (flagged as such). Clients plotting tags
  on an image convert world → pixels themselves via the stream
  metadata (principle 5).

Singleton discipline (flock), idle gating (no subscribers → low-rate
capture, lazy JPEG encode), self-heal on camera read failures, and the
advisory `info.json` flags (`calibration_stale`, `frame_dark`) are retained.

**Removed from v2:** the dead legacy transport (`daemon/protocol.py`,
`daemon/client.py` — msgpack framing and newline-JSON control client,
already unreferenced).

### 4.2 `camera/` — camera identity and backends (daemon-only)

**What it does:** stable camera identity across reboots and re-plugs
(identity chain: libcamera id → serial/AVFoundation id → vid:pid@location →
fallbacks), the persistent registry (`registry.json`: identity → stable
enumeration number + camera directory), and the capture backends
(V4L2/AVFoundation via OpenCV, libcamera loopback via `v4l2-ctl` subprocess
reader, GStreamer, screen capture). Applies UVC settings from `config.json`
(`uvc-util`), re-applied on reopen. Only the daemon imports this package.

### 4.3 `vision/` — the detection engine (daemon-only)

**What it does:** stateless frame-level vision consumed by the daemon
pipeline: tag detection with illumination flattening / CLAHE / downscale
preprocessing (AprilTag families + ArUco 4x4), LK optical-flow tracking
between full detects, non-tag object detection (square/cube detector,
LAB-Gaussian and HSV color classifiers), and the OpenCV 4.6/4.7 ArUco
compatibility shim.

**Consolidation in v2:** today detection/tracking logic is split between
`core/` (detector, tracker, aprilcam pipeline, playfield boundary, motion)
and `vision/`. v2 merges the stateless vision code into `vision/` and the
stateful per-camera pipeline into `daemon/`; the duplicate second pipeline
(`core/pipeline.py` + `core/motion.py`, unused by the daemon) is removed.

### 4.4 `calibration/` — playfield calibration (daemon-only in v2)

**What it does:** establishes the pixel→world mapping for a camera from the
playfield definition: multi-frame averaged detection of corner ArUcos,
initial homography, augmentation with all surveyed markers, optional
distortion correction, static-marker recording for deskew-without-live-
corners, and metric deskew geometry (A1-centred world frame, +y north).
Writes `calibration.json` (regenerable state) while `config.json` stays
developer-owned.

**Changed in v2:** calibration **executes inside the daemon**, triggered by
a `Calibrate` RPC. Today the CLI and MCP server run the calibration math
locally (importing cv2), which breaks the vision boundary and duplicates
frame access. In v2, `aprilcam calibrate` and the MCP `calibrate_playfield`
tool are pure orchestrators: they call the RPC, report progress/results.
This removes OpenCV from every non-daemon component for real.

The parallax model and per-tag correction (height above field, mount
offset, mount yaw → robot centre) are applied in the daemon per frame,
sourced from the mobile-tag registry (4.1). Calibration itself never
contains robot-specific data.

### 4.5 `playfield/` — playfield definitions

**What it does:** the playfield definition schema and its handling. A
definition is the document we have today (`main-playfield.json` shape):
field dimensions, origin convention (`apriltag-center-a1`), and surveyed
positions of every feature — ArUco tags, AprilTags, colored rectangles,
colored dots.

Feature roles are explicit:

- **ArUco and AprilTags** are calibration ground truth.
- **Colored rectangles and dots** are *not* used for calibration, but
  their surveyed positions are queryable — "where is the red square"
  answers from the definition, not from vision.

**Lifecycle (changed in v2):** the definition's source of record is a
local file, typically in a repo. It is *pushed* to the daemon
(`SetPlayfield`); the daemon persists it, consumes it internally, and
serves it back to any client (`GetPlayfield`). Clients — local or remote
— always get the definition from the daemon, never from a shared file.
A playfield's document, when served, also reports which cameras are
currently linked to it (derived, not stored in the source file).

There is no workspace directory, no `paths.json`, and no shared
annotation state — drawing is a client-side library concern (§4.6).

### 4.6 `client/` — the thin client library

**What it does:** the only sanctioned way to talk to the daemon. Discovery
(explicit → env → local unix probe → mDNS), the typed gRPC wrapper
(`DaemonControl`), stream consumers (image / deskewed / tag), Pydantic
models (`TagRecord`, `TagFrame`, `ImageFrame`, …) with `from_proto` as the
single proto boundary, and base-26 host/camera codes (`A`, `FB`) backed by
the probe host store. cv2-free.

**Drawing library** *(new home in v2)*: a pure-Python drawing/annotation
layer in the client package — get a frame (raw or deskewed) from the
daemon, overlay paths, waypoints, markers, and shapes on it in world
coordinates, using the stream metadata for the cm↔px mapping. This is
the *only* way to draw on the playfield: it is in-process, per-client
state. Nothing about drawings goes through the daemon or gets persisted
by AprilCam; a program that wants to keep its paths keeps them itself.
(Replaces the path registry, `paths.json`, the daemon path proxy, and
the overlay push machinery.)

A program that wants agent-driven drawing — e.g. a richer visualization
GUI where an MCP agent places markers — embeds its *own* MCP server and
exposes its own drawing tools, built on this library. AprilCam supplies
the streams and the drawing primitives, not the collaboration service.

### 4.7 `mcp/` — the MCP server (today `server/`)

**What it does:** a FastMCP stdio server, a pure daemon client:

- **Perception tools**: cameras (list/open/close/capture), detection
  (get_tags, history, objects), frames (raw and deskewed), mobile-tag
  registration (per-tag height / mount offset / yaw), calibration
  trigger, `where` (feature positions from the playfield definition).
- **Configuration tools** (whole-document): get/set playfield definition,
  get/set/reset camera config, list playfields/cameras with their links.
- Pixel↔world conversion using the daemon-published homography/metadata.

**Slimmed in v2:** the frame-processing registry (`create_frame`,
`process_frame`, `get_frame_image`, …) and every local-cv2 code path are
removed — consumers needing pixel work call `get_frame` and process the
JPEG themselves (the vision-boundary contract). All path tools
(`create_path`, `delete_path`, `list_paths`, `clear_paths`) and the
live-view overlay tools are removed — drawing is the client-side library
(§4.6), not an agent service.

### 4.8 `view/` — the interactive viewer (today `cli/view_cli.py` + `ui/`)

**What it does:** the tkinter live viewer, deliberately **basic viewing
only**: subscribes to the deskewed stream and tag stream and renders tags
and its own local annotations via the client drawing library; collapsible
panels toggle rendering layers (the existing panel on/off behavior).
Anything more sophisticated is a separate program built on the client
library (§4.6), not a viewer feature. Becomes a proper subsystem instead
of an 1,185-line CLI module; the drawing helpers from `ui/display.py`
fold into the client drawing library. With the daemon publishing deskewed
frames, the viewer's cv2 dependency goes away entirely.

The unused `ui/tui.py` Rich dashboard is removed.

### 4.9 `cli/` — command-line entry points

**What it does:** the `aprilcam` dispatcher and one thin module per
subcommand: `daemon`, `mcp`, `view`, `calibrate`, `cameras`, `tags`,
`probe`, `mobile`, `config`, `init`, `taggen`, `tool`, `migrate-dirs`.
All are daemon clients or local utilities; none import cv2 (`taggen`
excepted only if tag rendering requires it — see 4.10). `web` is removed.

### 4.10 `tags/` — fiducial generation

**What it does:** AprilTag 36h11 and ArUco 4x4 image/PDF generation
(`aprilcam taggen`). Standalone; the one place outside the daemon where an
OpenCV import is tolerated today (ArUco dictionaries), acceptable because
it is an offline utility — or replaced with pre-rendered dictionaries to
make the base install fully cv2-free.

### 4.11 `proto/` + top-level `config.py`

Generated protobuf modules (regenerated by `scripts/compile_proto.py`) —
extended in v2 with the deskewed-stream and calibrate RPCs. `config.py`
keeps the FHS/XDG directory resolution, env cascade, and the
`CONFIG_VARS` self-documentation; the legacy `AppConfig`/`iohelpers`
loaders are removed.

## 5. Data and storage layout

All shared state below lives on the **daemon host**. The source of
record for configuration documents is wherever the developer keeps them
(typically a repo); they reach the daemon by being pushed, and the daemon
persists its copy so it survives restart. Clients never read these files
— they ask the daemon.

```
config_dir/                        # admin-owned documents (pushed or hand-placed)
  cameras/<cam>/config.json        # camera kind, gains/shutter/brightness &
                                   #   UVC controls, resolution, playfield
                                   #   link, camera_position
  playfields/<slug>.json           # playfield definition (ground truth)

data_dir/                          # regenerable / runtime state
  cameras/registry.json            # identity -> enum + dir
  cameras/<cam>/calibration.json   # homography, static markers, provenance
  cameras/<cam>/info.json          # advisory: calibration_stale, frame_dark
  tags.json                        # mobile-tag registry (per-tag mount params)
  hosts.json                       # probe results / host codes

socket_dir/                        # control.sock, per-camera stream sockets
log_dir/                           # daemon logs
```

## 6. Removed in v2

| What | Why |
|---|---|
| Web server (`server/web_server.py`, `aprilcam web`) | consumers are MCP, viewer, robots (direct client), future ROS; HTTP/WS adds a surface nobody needs |
| MCP frame-processing registry + all MCP-local cv2 | vision boundary; `get_frame` + caller-side processing covers it |
| Paths as a service: `PathRegistry`, per-camera `paths.json`, daemon `GetPaths`/`SetPaths` proxy, MCP path tools, `PublishOverlay`/`OverlayFrame` push | drawing is a client-side library (§4.6); no multi-client path collaboration needed |
| `core/pipeline.py`, `core/motion.py` (second pipeline) | duplicate of the daemon path |
| `daemon/protocol.py`, `daemon/client.py` | dead legacy transport |
| Root `stream.py`, `ui/tui.py`, `iohelpers.py`, `AppConfig` | dead / legacy, unreferenced or nearly so |
| `homocal` legacy CLI + upper-left-origin corner table in `homography.py` | superseded by playfield-def calibration; the old origin table is a foot-gun |

## 7. Retained invariants (hard-won, do not regress)

- World frame is **A1-centred, +x east, +y north**, units cm; origin
  shift is a pure subtraction on both axes — never a y-flip. Getting
  "up is up" right consumed enormous effort; treat every sign and axis
  in a geometry change as guilty until proven innocent.
- On the deskewed image: **north is up, east is right**. Image rows
  grow downward, so world→deskewed-pixel conversion flips y exactly
  once, inside the published mapping — never a second time in client
  code.
- Heading: `heading_rad = atan2(world_vy, world_vx)` — 0 = east,
  counter-clockwise positive.
- **Positions are stored and exchanged in world coordinates only.**
  Pixel positions in stored state are legitimate solely as calibration
  anchors (`corner_pixels`, `static_markers` — they *define* the
  pixel↔world mapping); anything else storing pixels is a bug.
- ArUco IDs map to detection ids as `-(N+1)` to avoid AprilTag collisions.
- `calibration.json` written before `config.json`; config write failures
  never roll back a calibration.
- Daemon reads `config.json` but never writes it except through the
  sanctioned `SetCameraConfig` proxy.
- Atomic file writes (tmp + fsync + rename) for all shared JSON.
- Stream backpressure: bounded queues, silent drop, change-driven tag
  publish with heartbeat.
- OV9782-class cameras need manual low exposure via config-applied UVC
  settings; auto-exposure-mode quirks are config, not code.

## 8. Future (explicit non-goals for v2, design must not preclude)

- **ROS bridge**: a standalone node consuming the client library
  (deskewed stream + tag stream) and republishing as ROS topics. Requires
  nothing from v2 except stable stream contracts.
- Streamable-HTTP MCP transport.
- Shared/persistent annotation state (collaborative paths), should the
  no-collaboration decision ever be revisited — it would layer on the
  daemon config service, not on shared files.

## 9. Resolved decisions (stakeholder, 2026-07-29)

1. **Deskewed frame bounding box:** default **800×800**, per-playfield
   override.
2. **No overlay RPC.** An agent that needs to draw into a human-visible
   view does so through a purpose-built program that embeds its own MCP
   server on top of the client + drawing libraries. `aprilcam view` is
   for basic viewing only.
3. **Viewer stays in the package**, deliberately basic: subscribe to the
   deskewed + tag streams and watch. Sophistication belongs in separate
   programs.
4. **`taggen` keeps its OpenCV dependency** — offline utility, already
   behind the `[daemon]` extra.
