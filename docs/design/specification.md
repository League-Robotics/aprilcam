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
| `aprilcam view` | interactive GUI process | optional | thin subscriber: deskewed stream; renders embedded tags + annotation layers via the client renderer |
| CLI commands | short-lived processes | optional | thin daemon clients (calibrate, cameras, tags, mobile, probe…) |
| ROS node | future process | no | **streams only** — republishes image + tag streams as ROS topics; not a query interface ([adapters.md](adapters.md) §4.3) |
| Web server | — | **removed in v2** | HTTP/WS interface dropped; ROS + MCP + view cover the consumers |

The daemon is a small stable core; the CLI, MCP server, and ROS node are
**adapters** that translate it into a consumer's native idiom and hold no
authoritative state of their own. See [adapters.md](adapters.md).

There is **one daemon** serving both perception and configuration. The
playfield is not a separate service and not a shared directory: playfield
and camera configuration documents are pushed to the daemon, held by it,
and served back to any client, local or remote (§4.5). The daemon is the
only rendezvous point, which is what makes a Pi-over-the-field with
remote users work.

There is **one MCP server**, not two. It fronts perception and
configuration through the same daemon connection. Internally the tool
domains are separate modules with no shared state.

Drawing on the playfield — paths, markers, symbols — splits in two
(decision 8, 2026-07-31): **authoring is a daemon service, rendering is
a client-side library** (§4.6). Clients push world-coordinate
annotations into the daemon's in-memory, layer-keyed store; the daemon
embeds the current set in every published deskewed frame but never
rasterizes anything. Multi-client collaboration on drawings is the
point — one service draws, every consumer sees — and the MCP server
exposes drawing tools as thin RPC wrappers.

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
  trigger *(new in v2 — see 4.4)*, mobile-tag registry, the annotation
  store *(new in v2 — decision 8: `PutAnnotation`, `RemoveAnnotation`,
  `ReplaceLayer`, `ClearAnnotations`, `GetAnnotations`, all
  playfield-addressed)*, and the configuration service *(new in v2)*:
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
  **Boundary and hat:** each record also carries the tag's facing yaw
  (orientation — distinct from motion heading) and its boundary quad,
  ordered `(front-left, front-right, back-right, back-left)` so the
  corner 0→1 edge is the front edge the "hat" marker sits on. World
  corners are the contract on a calibrated camera; pixel corners, in
  raw-stream pixel space, exist for drawing on raw frames (where the
  client has no world→pixel mapping) and are the only representation
  on an uncalibrated camera. Corners outline the tag **as observed**
  — mount corrections apply to position and yaw, never to corners.
- **Frames carry tags.** When detection is running, every published
  frame — raw and deskewed, streamed or fetched via `CaptureFrame` —
  embeds the `TagFrame` from the detection pass nearest its capture.
  One frame is sufficient to render a fully annotated view: no second
  query, no join. Absent tags means detection is off; an empty
  `TagFrame` means detection ran and saw nothing; the embedded frame's
  own timestamp exposes any capture↔detection skew.
- **Frames carry annotations** *(decision 8)*. Deskewed frames also
  embed the linked playfield's current annotation set, with a monotonic
  revision so renderers can skip re-rasterizing an unchanged overlay.
  The addressing rule is the join: observations are camera-addressed;
  field state — surveyed features, annotations — is playfield-addressed;
  the embed connects them via the camera's playfield link. **Raw frames
  carry no annotations**: annotations are world-only and a raw frame
  gives the client no world→pixel mapping (UC-16, "raw means raw").
  The rejected alternative — embedding them unrenderably for agents
  that want the data anyway — loses to `GetAnnotations`, which serves
  that need directly.

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

There is no workspace directory and no `paths.json` — nothing about
drawings touches disk. Shared annotation state *does* exist in v2
(decision 8): the daemon holds each playfield's annotations in memory,
layer-keyed, and embeds them in published deskewed frames (§4.6).

### 4.6 `client/` — the thin client library

**What it does:** the only sanctioned way to talk to the daemon. Discovery
(explicit → env → local unix probe → mDNS), the typed gRPC wrapper
(`DaemonControl`), stream consumers (image / deskewed / tag), Pydantic
models (`TagRecord`, `TagFrame`, `ImageFrame`, …) with `from_proto` as the
single proto boundary, and base-26 host/camera codes (`A`, `FB`) backed by
the probe host store. cv2-free.

**Annotations and rendering** *(reshaped 2026-07-31, decision 8)*: two
halves with a hard line between them.

- **Authoring is a daemon RPC surface.** Clients draw by pushing
  world-coordinate annotations (markers, paths, polygons, circles,
  text) into the daemon's in-memory store — keyed (playfield, layer,
  id), upserted one at a time or swapped atomically per layer, cleared
  by layer or by id. A client needs no filesystem and holds no state:
  connect and draw. Because every service draws into the same store and
  every deskewed frame embeds it, annotations are how services
  communicate spatially — with each other and with humans watching the
  viewer.
- **Rendering is a client library.** A cv2-free `Renderer` takes an
  `ImageFrame` — with its embedded tags and annotations — and
  rasterizes overlays (tag boundaries and hats, annotation shapes) to
  PNG using the frame's published world→pixel metadata. The daemon
  never rasterizes. Purely local, ad-hoc drawing (debug overlays)
  passes extra `Annotation` values straight to the renderer — the same
  types, no second vocabulary, nothing sent to the daemon.

(The v1 mechanism this replaces — file-backed path registry,
`paths.json`, the daemon path proxy, and the overlay push/compositing
machinery — stays removed; §6.)

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
JPEG themselves (the vision-boundary contract).

**Drawing tools** *(returned 2026-07-31, decision 8)*: `draw_marker`,
`draw_path`, `draw_polygon`, `draw_circle`, `draw_text`,
`remove_annotation`, `clear_layer`, `list_annotations` — thin wrappers
over the daemon's annotation RPCs, world-coordinate only, no cv2; the
vision boundary is untouched. (v1's path tools died with the
paths-as-a-service mechanism; these are their daemon-backed
successors.)

### 4.8 `view/` — the interactive viewer (today `cli/view_cli.py` + `ui/`)

**What it does:** the tkinter live viewer, deliberately **basic viewing
only**: subscribes to the deskewed stream and renders the embedded tags
and annotation layers via the client renderer; collapsible panels
toggle layers by name — built-in layers (tags, features) plus each
named annotation layer (decision 8, the existing panel on/off
behavior).
Anything more sophisticated is a separate program built on the client
library (§4.6), not a viewer feature. Becomes a proper subsystem instead
of an 1,185-line CLI module; the drawing helpers from `ui/display.py`
fold into the client renderer. With the daemon publishing deskewed
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
                                   # (no tags.json — the mobile-tag registry
                                   #  is in-memory in v2; clients register
                                   #  mounts at session start. 2026-07-31)
                                   # (no hosts.json — host codes removed in v2)

socket_dir/                        # control.sock, per-camera stream sockets
log_dir/                           # daemon logs
```

## 6. Removed in v2

| What | Why |
|---|---|
| Web server (`server/web_server.py`, `aprilcam web`) | consumers are MCP, viewer, robots (direct client), future ROS; HTTP/WS adds a surface nobody needs |
| MCP frame-processing registry + all MCP-local cv2 | vision boundary; `get_frame` + caller-side processing covers it |
| Paths as a service: `PathRegistry`, per-camera `paths.json`, daemon `GetPaths`/`SetPaths` proxy, MCP path tools, `PublishOverlay`/`OverlayFrame` push | the v1 *mechanism* stays removed; the capability returned 2026-07-31 in a different shape — in-memory, playfield-addressed, layer-keyed annotation store, daemon still never renders (§9 #8) |
| `core/pipeline.py`, `core/motion.py` (second pipeline) | duplicate of the daemon path |
| `daemon/protocol.py`, `daemon/client.py` | dead legacy transport |
| Root `stream.py`, `ui/tui.py`, `iohelpers.py`, `AppConfig` | dead / legacy, unreferenced or nearly so |
| Host codes (`client/host_codes.py`, base-26 `A`/`FB` addressing, `hosts.json`) | **removed in v2** — cameras are addressed by their derived slug and daemons by hostname (`-d`); a second addressing scheme with its own persistent store is complexity the CLI does not need |
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
- **Persistent** annotation state — annotations surviving a daemon
  restart. (The *shared* half of this item happened 2026-07-31,
  decision 8 — and layered on the runtime-state model like the
  mobile-tag registry, not on the config service as this item
  predicted, because annotations are not configuration.)

## 9. Resolved decisions (stakeholder, 2026-07-29)

1. **Deskewed frame bounding box:** default **800×800**, per-playfield
   override.
2. **No overlay RPC.** An agent that needs to draw into a human-visible
   view does so through a purpose-built program that embeds its own MCP
   server on top of the client + drawing libraries. `aprilcam view` is
   for basic viewing only.
   *(Superseded 2026-07-31: see decision 8 — the daemon now holds
   annotation state. Rendering remains client-side.)*
3. **Viewer stays in the package**, deliberately basic: subscribe to the
   deskewed + tag streams and watch. Sophistication belongs in separate
   programs.
4. **`taggen` keeps its OpenCV dependency** — offline utility.
   *(Superseded 2026-07-29: OpenCV moved into the **base** install rather
   than the `[daemon]` extra, so tag generation works on any install —
   including one made only to talk to a remote daemon. See
   [use-cases/cli.md](use-cases/cli.md) UC-21. The vision **boundary** is
   unchanged: the daemon remains the sole vision authority and no client
   component does pixel work. OpenCV being importable is not permission
   to use it outside the daemon — that is now a discipline the tests
   enforce, not something packaging enforces for us.)*

5. **Tag history is kept** (2026-07-30) — the daemon's bounded ring
   buffer of per-frame tag records survives into v2 as a query. See
   [use-cases/daemon.md](use-cases/daemon.md) UC-26.

6. **The system carries its own instructions** (2026-07-30) — packaged
   guides served to agents, as v1's `AGENT_GUIDE.md` /
   `ROBOT_API_GUIDE.md` were. See
   [use-cases/daemon.md](use-cases/daemon.md) UC-27.

7. **Host codes are dropped** (2026-07-30) — no base-26 `A`/`FB`
   addressing, no `hosts.json`. Cameras are addressed by derived slug,
   daemons by hostname.

8. **Daemon-held annotation state** (2026-07-31, supersedes decision 2) —
   the daemon **stores** annotations (paths, markers, symbols); it never
   renders them. Clients author via RPC in world coordinates on a
   **playfield**; every published frame embeds the current annotation
   set exactly as frames embed the `TagFrame`, so one frame suffices to
   render a fully annotated view, and rendering stays a client-side
   library. Motivation: multi-service communication — one service draws,
   every service and viewer sees — and stateless clients that need no
   filesystem, just a daemon connection. Two sub-decisions are settled:
   - **In-memory only.** Annotations are runtime state like the
     mobile-tag registry, not configuration; they vanish on daemon
     restart and clients re-draw on reconnect. Nothing about them
     touches disk.
   - **Named layers.** Every annotation belongs to a client-chosen layer
     (e.g. `planner`, `robot-3`); clear/replace by layer or by id;
     **no ownership enforcement** — last write wins on a (layer, id)
     key. The viewer's collapsible panels toggle layers by name.
   The v1 paths-as-a-service *mechanism* (file-backed `PathRegistry`,
   daemon-side compositing via `PublishOverlay`) stays removed (§6).
   See [use-cases/daemon.md](use-cases/daemon.md) UC-28.
