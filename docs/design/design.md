# AprilCam System Design

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** in-flux (v1 → v2 transition)

This is the system-level design document: the subsystem map, the global
conventions every subsystem may assume, and the boundaries that hold the
system together. Per-subsystem design lives in a co-located `DESIGN.md`
inside each package directory. The v2 requirements this design serves are
in [specification.md](specification.md).

---

## 1. System overview

AprilCam turns an overhead camera view of a fiducial-marked playfield into
four products, all served by a single daemon that is the sole owner of
the cameras and the sole vision authority:

1. **Deskewed, calibrated top-down images** of the playfield.
2. **Live tag records** — id, world position (cm), heading, velocity —
   corrected for lens distortion, parallax, and robot tag mounting.
3. **Configuration service** — playfield definitions and camera configs,
   pushed to the daemon as whole documents and served back whole to any
   client, local or remote.
4. **Shared annotation state** *(v2)* — world-coordinate paths, markers,
   and symbols pushed by any client, held in daemon memory, embedded in
   published deskewed frames for every other client to render.

Everything else is a thin client of the daemon: the MCP server (for AI
agents), the basic viewer, the CLI tools, robot code using the client
library, and (future) a ROS bridge.

## 2. Architectural principles

These are binding on all subsystems; per-subsystem docs do not restate
them.

1. **Single vision authority.** All OpenCV work — capture, detection,
   tracking, homography, deskew, calibration — runs in the daemon
   process only. No other component opens a camera or imports cv2.
   (Exception: `tags/` generation, an offline utility behind the
   `[daemon]` extra.)
2. **Compute once, publish to all.** Any derived product needed by more
   than one client (deskewed image, tag stream) is computed in the
   daemon and published; clients subscribe, never recompute.
3. **The daemon is the rendezvous point.** Shared state lives in the
   daemon on the camera host (e.g., a Raspberry Pi over the field).
   Clients never share files with each other.
4. **Whole-document configuration.** Playfield definitions and camera
   configs move as complete documents (`Set*`/`Get*`); there are no
   field-level edit RPCs. Source of record is the developer's repo;
   the daemon persists its pushed copy. This rule governs
   *configuration documents*; **runtime registrations** — mobile-tag
   parameters, annotations — are record-level by nature and are not
   configuration.
5. **Calibration is robot-agnostic.** Robot-specific facts (tag height,
   mount offset, mount yaw) are per-tag runtime registrations (mobile
   tag registry), applied by the daemon as post-processing.
5a. **World coordinates first; pixels only at the edges.** Convert to
   world coordinates as early in the pipeline as possible; track,
   store, and exchange world coordinates everywhere downstream.
   Positions are never stored in pixels. Pixel coordinates exist only
   (a) inside the daemon's detection internals and (b) at the final
   rendering step, where the client converts world → pixels for the
   image it is drawing on, using published stream metadata. Tag records
   are world-first; raw-pixel fields are diagnostic-only.
5b. **Geometry conventions are sacrosanct.** Up is up: A1-centred
   origin, +x east, +y north; north is up on the deskewed image; the
   origin shift is pure subtraction, never a y-flip; heading 0 = east,
   CCW positive. These took enormous effort to get right — no change
   touching geometry lands without demonstrating they still hold.
6. **Annotations are daemon state; rendering is a client library.**
   *(Reversed 2026-07-31 — spec §9 #8; previously "drawing is a
   library, not a service".)* The daemon stores world-coordinate,
   layer-keyed annotations as in-memory runtime state — like the
   mobile-tag registry, gone on restart — and embeds the current set in
   published deskewed frames. It never rasterizes. Clients (viewer,
   robot programs, MCP agents) author via RPC and render locally.
7. **Clients see handles and services, never file paths.**
8. **Config/state split.** `config_dir` = admin-owned documents;
   `data_dir` = regenerable state. Files never migrate implicitly.

## 3. Subsystem map

| Subsystem | Doc | Role | cv2? |
|---|---|---|---|
| `daemon/` | [DESIGN.md](../../src/aprilcam/daemon/DESIGN.md) | The aprilcamd process: camera ownership, per-camera pipeline, gRPC control plane, stream publishing, config service, mobile-tag registry, annotation store | yes |
| `camera/` | [DESIGN.md](../../src/aprilcam/camera/DESIGN.md) | Camera identity, persistent registry, capture backends, UVC settings | yes |
| `vision/` | [DESIGN.md](../../src/aprilcam/vision/DESIGN.md) | Stateless frame vision: tag detection, tracking, object/color detection, OpenCV compat | yes |
| `calibration/` | [DESIGN.md](../../src/aprilcam/calibration/DESIGN.md) | Pixel→world calibration from playfield definitions; deskew geometry | yes |
| `playfield/` *(v2)* | — | Playfield definition schema + daemon-side document handling | no |
| `client/` | [DESIGN.md](../../src/aprilcam/client/DESIGN.md) | Discovery, gRPC wrapper, stream consumers, models, renderer *(v2)* | no |
| `mcp/` *(today `server/`)* | [DESIGN.md](../../src/aprilcam/server/DESIGN.md) | MCP stdio server: perception + configuration tools, pure daemon client | no *(v2)* |
| `view/` *(today in `cli/`+`ui/`)* | [DESIGN.md](../../src/aprilcam/ui/DESIGN.md) | Basic live viewer: deskewed stream with embedded tags + annotation layers | no *(v2)* |
| `cli/` | [DESIGN.md](../../src/aprilcam/cli/DESIGN.md) | `aprilcam` dispatcher + thin subcommand modules | no |
| `tags/` | [DESIGN.md](../../src/aprilcam/tags/DESIGN.md) | AprilTag/ArUco image + PDF generation | yes (offline) |
| `proto/` | [DESIGN.md](../../src/aprilcam/proto/DESIGN.md) | Generated protobuf wire contract (never hand-edited) | no |
| `core/` *(v1, dissolving)* | [DESIGN.md](../../src/aprilcam/core/DESIGN.md) | v1 pipeline + playfield boundary; v2 splits into `vision/` (stateless) and `daemon/` (stateful) | yes |

Top-level modules: `config.py` (FHS/XDG directory resolution, env
cascade, `CONFIG_VARS` self-documentation), `errors.py`, `guides.py`
(packaged agent/robot guides).

## 4. Process and deployment view

| Runtime piece | Kind | Notes |
|---|---|---|
| `aprilcamd` | long-running daemon | flock singleton per machine; unix socket + TCP 5280; mDNS `_aprilcam._tcp.local.` |
| `aprilcam mcp` | stdio process per agent session | pure daemon client |
| `aprilcam view` | GUI process | basic viewing only |
| CLI commands | short-lived | thin daemon clients |
| ROS node | future | client-library consumer republishing **streams only** to ROS topics ([adapters.md](adapters.md) §4.3) |

Cardinality: a camera observes exactly one playfield; a playfield has
zero or more cameras. A camera config names its playfield; a served
playfield document reports its linked cameras (derived at serve time).

## 5. Wire contracts

- **Control plane:** gRPC (proto in `proto/aprilcam.proto`, regenerated
  via `scripts/compile_proto.py`). Camera lifecycle, one-shot queries,
  stream endpoint allocation, calibration trigger, config service
  (`SetPlayfield`/`GetPlayfield`, `SetCameraConfig`/`GetCameraConfig`/
  `ResetCameraConfig`), mobile-tag registry, annotation store
  (`PutAnnotation`/`RemoveAnnotation`/`ReplaceLayer`/
  `ClearAnnotations`/`GetAnnotations`, playfield-addressed).
- **Bulk streams:** separate sockets (unix or TCP, allocated on demand),
  framing `[uint32 BE length][protobuf]`:
  - raw image stream (JPEG frames),
  - deskewed playfield stream *(v2)* — field warped aspect-preserved
    into a bounding box (default 800×800, per-playfield override);
    px-per-cm is derived and reported in stream metadata, never chosen,
  - tag stream (`TagFrame`, change-driven, rate-capped, heartbeat);
    records include the tag's facing yaw and boundary corners so
    clients can draw outlines and front "hats" without vision work.

  When detection is running, image frames (raw and deskewed alike)
  embed the latest `TagFrame`; deskewed frames additionally embed the
  playfield's current annotation set, with a monotonic revision so
  renderers skip re-rasterizing an unchanged overlay. One deskewed
  frame carries everything needed to render a fully annotated view.
  Raw frames carry no annotations — annotations are world-only and a
  raw frame gives the client no world→pixel mapping.
- **Backpressure:** bounded per-subscriber queues, silent drop. Slow
  consumers lose frames, never stall the pipeline.

## 6. Global conventions (every subsystem may assume these)

- **World frame:** A1-centred, +x east, +y north, cm units. The origin
  shift is a pure subtraction on both axes — never a y-flip. Violating
  this silently mirrors the field north/south.
- **Deskewed image orientation:** north is up, east is right. Image
  rows grow downward, so world→pixel conversion flips y exactly once,
  inside the published mapping — never again in client code.
- **Heading:** `atan2(world_vy, world_vx)`; 0 = east, CCW positive.
- **Tag yaw:** the direction the tag's front faces; 0 = east, CCW
  positive (same convention as heading). Yaw is orientation, heading
  is motion — a robot reversing has them 180° apart.
- **Corner order:** tag boundary quads are `(front-left, front-right,
  back-right, back-left)` in the tag's own frame; the corner 0→1 edge
  is the front edge. Same physical order in world and pixel space.
  Corners are the observed tag outline and are never mount-corrected.
- **World-only storage:** positions in stored state or on the wire are
  world cm. Pixel positions are legitimate solely as calibration
  anchors (`corner_pixels`, `static_markers`) and diagnostics.
- **ID space:** ArUco marker N is recorded as tid `-(N+1)`; AprilTags
  use their native non-negative ids. Playfield ArUco layout: perimeter
  ids 1–8, interior ids ≥ 9.
- **Feature roles:** ArUco + AprilTags are calibration ground truth;
  colored rectangles/dots are queryable surveyed positions only.
- **Atomic writes** (tmp + fsync + rename) for every shared JSON file.
- **Write ordering:** `calibration.json` before `config.json`; a config
  write failure never rolls back a calibration.
- **Proto boundary:** generated proto types cross into Python only via
  `from_proto` constructors in `client/models.py`.

## 7. Storage layout (daemon host)

```
config_dir/                        # admin-owned documents (pushed or hand-placed)
  cameras/<cam>/config.json        # camera kind, gains/shutter/brightness,
                                   #   UVC controls, resolution, playfield
                                   #   link, camera_position
  playfields/<slug>.json           # playfield definition (ground truth)
data_dir/                          # regenerable / runtime state
  cameras/registry.json            # identity -> enum + camera dir
  cameras/<cam>/calibration.json   # homography, static markers, provenance
  cameras/<cam>/info.json          # advisory: calibration_stale, frame_dark
                                   # (no tags.json — mobile-tag registry is
                                   #  in-memory; register at session start)
                                   # (no hosts.json — host codes removed in v2)
socket_dir/                        # control.sock + per-camera stream sockets
log_dir/
```

## 8. Test fixtures — the camera image matrix

`tests/images/<camera-slug>/` holds the **canonical captured test
fixtures**: real playfield images taken from each configured camera
under a matrix of camera-configuration variations (exposure sweep
crossing the washout cliff, gain, auto-exposure, white balance, stress
combos), each with a `manifest.json` recording the full effective
controls, camera identity, playfield link, and field-state id
(`tests/images/field-state.json` records what was physically on the
field). First capture 2026-07-29: 19 images from the Arducam OV9782,
9 from the HD USB CAMERA (whose exposure/WB controls do not apply on a
live handle — variation coverage grows once the tool toggles manual
modes first).

They are **regenerable, not sacred**: `testdev/capture_matrix.py`
(design in [docs/test-plans/camera-image-matrix.md](../test-plans/camera-image-matrix.md))
re-captures them on demand — it opens cameras directly with the
daemon's own `camera/` code (a sanctioned exception to the
daemon-sole-owner rule for offline test tooling; it refuses to run when
the daemon holds a device) and always restores baseline controls.
Pipeline, detection, calibration, and geometry-convention tests run
against these fixtures offline — no camera hardware needed to run the
suite.

## 9. Current state vs. this design (the v2 gap)

This design describes the v2 target. The code is v1; the deltas are
tracked as CLASI issues. Summary of the gap:

1. No deskewed stream yet — clients warp raw frames themselves.
2. Calibration executes in CLI/MCP with local cv2, not in the daemon.
3. No `SetPlayfield`/`GetPlayfield`/`ResetCameraConfig`; playfield defs
   are hand-placed files on the daemon host.
4. Paths are a file-backed service (PathRegistry, `paths.json`, daemon
   file proxy, MCP path tools, `PublishOverlay` compositing) instead of
   the v2 shape: an in-memory daemon annotation store plus a
   client-side renderer (spec §9 #8).
5. The web server exists; v2 removes it.
6. The MCP server carries a frame-processing registry and other
   local-cv2 tools; v2 removes them.
7. Package layout: `server/` → `mcp/`, `core/` dissolves into
   `vision/` + `daemon/`, viewer extracted from `cli/`+`ui/` into
   `view/`, dead modules deleted (`daemon/protocol.py`,
   `daemon/client.py`, root `stream.py`, `ui/tui.py`, `iohelpers.py`,
   `AppConfig`, legacy `homocal`).
