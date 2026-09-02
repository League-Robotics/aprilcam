---
source_paths:
- /Volumes/Proj/proj/RobotProjects/aprilcam/src/aprilcam
---
# AprilCam System Design

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-21 · **Status:** stable

This is the system-level design document: the subsystem map, the global
conventions every subsystem may assume, and the boundaries that hold the
system together. Per-subsystem design lives in a co-located `DESIGN.md`
inside each package directory (linked from the table below) — this
document does not restate their content, only what every one of them is
allowed to assume. The requirements this design serves are in
[specification.md](specification.md).

The v1-to-v2 rebuild this document once tracked as "in-flux" is, as of
this review, substantially complete: every subsystem below has a real
implementation, the package layout the rebuild targeted
(`server/`→`mcp/`, `core/` dissolved into `vision/`+`daemon/`, the
viewer extracted into `view/`) has already happened, and the streaming,
config-service, calibration-in-daemon, and annotation-store capabilities
this document once listed as future work all exist in the code today.
What remains is recorded honestly in §9, not hidden — real, specific
gaps (an unverified libcamera capture path, a couple of spec/code drifts)
rather than a wholesale rebuild-in-progress.

---

## 1. System overview

AprilCam turns an overhead camera view of a fiducial-marked playfield into
four products, all served by a single daemon that is the sole owner of
the cameras and the sole vision authority:

1. **Deskewed, calibrated top-down images** of the playfield — computed
   once in the daemon and published to every subscriber, not warped
   independently by each client.
2. **Live tag records** — id, world position (cm), heading, velocity —
   corrected for lens distortion, parallax, and robot tag mounting.
3. **Configuration service** — playfield definitions and camera configs,
   pushed to the daemon as whole documents and served back whole to any
   client, local or remote.
4. **Shared annotation state** — world-coordinate paths, markers, and
   symbols pushed by any client, held in daemon memory (never persisted,
   never rasterized by the daemon itself), embedded in published
   deskewed frames for every other client to render locally.

Everything else is a thin client of the daemon: the MCP server (for AI
agents), the basic viewer, the CLI tools, and robot code using the client
library. A ROS bridge is designed for but not yet built (§9).

## 2. Architectural principles

These are binding on all subsystems; per-subsystem docs do not restate
them.

1. **Single vision authority.** All OpenCV work — capture, detection,
   tracking, homography, deskew, calibration — runs in the daemon
   process only. No other component opens a camera or imports cv2.
   (Exception: `tags/` generation, an offline utility — `cv2` is
   importable everywhere, including a base install, but as of sprint
   036 it is imported **lazily, at the point of use**, not at module
   scope, and generating an actual tag image without the `daemon` extra
   installed fails with a clear message rather than working. This
   revises sprint 028's original recorded decision to keep
   `opencv-contrib-python` a **permanent, unconditional** base
   dependency for `tags/`'s sake — see `tags/DESIGN.md` §1 and §6 for
   the reversal and its rationale. The import-boundary rule is enforced
   by convention in most subsystems and mechanically by test for
   `client/`, `view/`, and `mcp/`; §9 no longer lists a packaging gap
   here — see §9's revised item 1.)
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
6. **Annotations are daemon state; rendering is a client library.** The
   daemon stores world-coordinate, layer-keyed annotations as in-memory
   runtime state — like the mobile-tag registry, gone on restart — and
   embeds the current set in published deskewed frames. It never
   rasterizes. Clients (viewer, robot programs, MCP agents) author via
   RPC and render locally. The store is bounded (fixed per-layer
   annotation-count and per-shape point-count limits) so a misbehaving
   client cannot grow it or the wire payload without limit.
7. **Clients see handles and services, never file paths.**
8. **Config/state split.** `config_dir` = admin-owned documents;
   `state_dir` = regenerable state (see §7's naming note). Files never
   migrate implicitly.

## 3. Subsystem map

| Subsystem | Doc | Role | cv2? |
|---|---|---|---|
| `daemon/` | [DESIGN.md](../../src/aprilcam/daemon/DESIGN.md) | The `aprilcamd` process: camera ownership, per-camera detection pipeline, gRPC control plane + stream transport, config service, mobile-tag registry, annotation store | yes |
| `camera/` | [DESIGN.md](../../src/aprilcam/camera/DESIGN.md) | Camera identity resolution across re-plugs, the persistent registry, pluggable capture backends (OpenCV/v4l2loopback/uvc-util) | yes |
| `vision/` | [DESIGN.md](../../src/aprilcam/vision/DESIGN.md) | Stateless frame vision: preprocessing pipeline, AprilTag/ArUco raw detection, world-position kinematics | yes |
| `calibration/` | [DESIGN.md](../../src/aprilcam/calibration/DESIGN.md) | Pixel↔world calibration engine (homography, lens distortion, intrinsics), deskew geometry, mount/parallax correction, flat-field gain-map capture (sprint 037) | yes |
| `playfield/` | [DESIGN.md](../../src/aprilcam/playfield/DESIGN.md) | The surveyed playfield as a definition document plus feature queries ("where is the blue dot") | no |
| `config/` | [DESIGN.md](../../src/aprilcam/config/DESIGN.md) | `config_dir`/`state_dir`/`socket_dir` resolution, and the config-document schema/loader (camera configs, playfield definitions) | no |
| `client/` | [DESIGN.md](../../src/aprilcam/client/DESIGN.md) | The one sanctioned way to talk to a daemon: gRPC wrapper, discovery, stream consumers, `from_proto` model boundary, annotation renderer | no *(enforced)* |
| `mcp/` | [DESIGN.md](../../src/aprilcam/mcp/DESIGN.md) | FastMCP stdio server: perception, configuration, mobile-tag, and annotation tools — pure daemon client | no *(enforced)* |
| `view/` | [DESIGN.md](../../src/aprilcam/view/DESIGN.md) | Basic Tk live-view window: streams a daemon image feed (raw or deskewed) and renders it | no *(enforced)* |
| `cli/` | [DESIGN.md](../../src/aprilcam/cli/DESIGN.md) | `aprilcam` dispatcher + thin per-subcommand modules, all daemon clients | no *(not mechanically enforced — see its DESIGN.md §6)* |
| `tags/` | [DESIGN.md](../../src/aprilcam/tags/DESIGN.md) | Purely offline AprilTag/ArUco fiducial image generation | yes *(base dependency, offline-only)* |
| `proto/` | [DESIGN.md](../../src/aprilcam/proto/DESIGN.md) | Generated protobuf/gRPC wire contract (never hand-edited; source is `proto/aprilcam_v2.proto`) | no |

Top-level modules under `src/aprilcam/` shared by every subsystem above
(see [the root DESIGN.md](../../src/aprilcam/DESIGN.md)): `types.py`
(shared value vocabulary), `errors.py` (the client-visible error
taxonomy), `framing.py` (bulk-stream wire framing), `guides.py` (packaged
agent/robot guide text).

## 4. Process and deployment view

| Runtime piece | Kind | Notes |
|---|---|---|
| `aprilcamd` | long-running daemon | flock singleton per machine; unix socket + TCP 5280; mDNS `_aprilcam._tcp.local.` |
| `aprilcam mcp` | stdio process per agent session | pure daemon client |
| `aprilcam view` | GUI process | basic viewing only |
| CLI commands | short-lived | thin daemon clients |
| ROS node | future | client-library consumer republishing **streams only** to ROS topics ([adapters.md](adapters.md) §4.3) — not yet built |

Cardinality: a camera observes exactly one playfield; a playfield has
zero or more cameras. A camera config names its playfield; a served
playfield document reports its linked cameras (derived at serve time,
never stored — see `playfield/DESIGN.md`).

## 5. Wire contracts

- **Control plane:** gRPC (proto in `proto/aprilcam_v2.proto`, regenerated
  via `scripts/compile_proto.py`). Camera lifecycle, one-shot queries,
  stream endpoint allocation, calibration trigger, flat-field gain-map
  capture (`CaptureFlatField`/`GetFlatField`/`ClearFlatField` — sprint
  037; mirrors `Calibrate`/`GetCalibration`'s shape exactly, including
  `force`/`dry_run` request flags and a data-not-exception quality-gate
  result), config service (`SetPlayfield`/`GetPlayfield`,
  `SetCameraConfig`/`GetCameraConfig`/`ResetCameraConfig`), mobile-tag
  registry, annotation store
  (`PutAnnotation`/`RemoveAnnotation`/`ReplaceLayer`/
  `ClearAnnotations`/`GetAnnotations`, playfield-addressed). `GetStatus`
  additionally carries a `protocol_version` field (sprint 031) so a v2
  client can distinguish an incompatible daemon from a merely
  unreachable one — see `client/DESIGN.md` §4 and `proto/DESIGN.md` §6
  for the current, partial state of that check.
- **Bulk streams:** separate sockets (unix or TCP, allocated on demand),
  framing `[uint32 BE length][protobuf]`:
  - raw image stream (JPEG frames),
  - deskewed playfield stream — field warped aspect-preserved
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
  Verified empirically (not assumed) to match OpenCV's native ArUco/
  AprilTag detector output under a stated mounting convention — see
  `vision/DESIGN.md` §4.
- **World-only storage:** positions in stored state or on the wire are
  world cm. Pixel positions are legitimate solely as calibration
  anchors (`corner_pixels`, `static_markers`) and diagnostics.
- **Feature roles:** ArUco + AprilTags are calibration ground truth;
  colored rectangles/dots are queryable surveyed positions only, never
  vision-confirmed.
- **Atomic writes** (tmp + fsync + rename) for every shared JSON file.
- **Write ordering:** `calibration.json` before `config.json`; a config
  write failure never rolls back a calibration. (No single code path
  writes both today — see `config/DESIGN.md` §6.)
- **Proto boundary:** generated proto types cross into Python only via
  `from_proto` constructors in `client/models.py` on the client side,
  and are read only inside `daemon/rpc.py` on the server side.

## 7. Storage layout (daemon host)

```
config_dir/                        # admin-owned documents (pushed or hand-placed)
  cameras/<cam>/config.json        # camera kind, gains/shutter/brightness,
                                   #   UVC controls, resolution, playfield
                                   #   link, camera_position
  playfields/<slug>.json           # playfield definition (ground truth)
state_dir/                         # regenerable / runtime state
  cameras/registry.json            # identity -> enum + camera dir
  cameras/<cam>/calibration.json   # homography, static markers, provenance
  cameras/<cam>/flatfield.npy      # per-pixel/channel gain map (apply side
                                   #   shipped out-of-process, commit 7f4c0ac;
                                   #   capture side sprint 037)
  cameras/<cam>/flatfield.json     # metadata sidecar: captured_at, shape,
                                   #   gain_min/max, stale (sprint 037,
                                   #   mirrors calibration.json's own
                                   #   provenance+stale fields)
  cameras/<cam>/info.json          # advisory: calibration_stale, frame_dark
  mounts/registry.json             # TagId -> MountParameters (sprint 036;
                                   #  previously in-memory only, register at
                                   #  session start -- see daemon/DESIGN.md §3)
socket_dir/                        # control.sock + per-camera stream sockets
```

Named `state_dir` throughout the actual code (`Directories.state_dir`,
`APRILCAM_STATE_DIR`) — this document previously called it `data_dir`,
which does not exist anywhere in the codebase; corrected here (see
`config/DESIGN.md` §6). `config/` resolves these directories but writes
none of them itself — every write to a file under `config_dir`/
`state_dir` happens in a specific, named consumer (`cli/locate.py`,
`camera/registry.py`, `daemon/service.py`); see `config/DESIGN.md` §3.
Log output goes to stderr only — there is no `log_dir`.

## 8. Test fixtures — the camera image matrix

`tests/images/<camera-slug>/` holds the **canonical captured test
fixtures**: real playfield images taken from each configured camera
under a matrix of camera-configuration variations (exposure sweep
crossing the washout cliff, gain, auto-exposure, white balance, stress
combos), each with a `manifest.json` recording the full effective
controls, camera identity, playfield link, and field-state id
(`tests/images/field-state.json` records what was physically on the
field).

They are **regenerable, not sacred**: `testdev/capture_matrix.py`
(design in [docs/test-plans/camera-image-matrix.md](../test-plans/camera-image-matrix.md))
re-captures them on demand — it opens cameras directly with the
daemon's own `camera/` code (a sanctioned exception to the
daemon-sole-owner rule for offline test tooling; it refuses to run when
the daemon holds a device) and always restores baseline controls, kept
as a direct-open tool rather than routed through the daemon's RPC
surface (design decision recorded in the tool's own module docstring).
Pipeline, detection, calibration, and geometry-convention tests run
against these fixtures offline — no camera hardware needed to run the
suite.

## 9. Known gaps and drift

Honest, current items — not a snapshot of a rebuild-in-progress (see the
introduction above for why this section replaces the old "v2 gap" list).
Each is also recorded in its owning subsystem's own `DESIGN.md` under
"Open Questions / Known Limitations":

1. **Resolved, sprint 036: OpenCV is now a `daemon`-extra-only
   dependency.** `opencv-contrib-python` moved from `[project.dependencies]`
   to `[project.optional-dependencies.daemon]`. This closed two related
   gaps at once: `client/renderer.py` and `cli/locate.py` no longer
   transitively require `cv2` at all (`calibration/geometry.py`'s two
   genuinely `cv2`-dependent functions, `undistort_point`/`distort_point`,
   moved to a new `calibration/distortion.py` — see `calibration/DESIGN.md`
   §2 and Design Rationale), and `tags/generator.py`'s `cv2` import
   became lazy, at the point of use, rather than module-scope (see
   `tags/DESIGN.md` §1/§4/§6 for that reversal of sprint 028's original
   "permanent base dependency" decision). A base-only install now
   genuinely cannot import `cv2` anywhere in its reachable module graph.
2. **No libcamera capture backend exists yet** — only `/sys`-based
   enumeration (`camera/backends/libcamera.py`) and the backend-selection
   *branch point* (`camera/backends/resolve_capture_backend`, sprint
   031) are built. There is no v4l2loopback/Pi hardware in this
   development environment to validate a real capture path against.
3. **`protocol_version` (§5) is not yet consumed for a *responding*
   mismatched daemon.** The version-mismatch detection sprint 031 added
   covers a v1 daemon that fails a v2 client's first RPC outright; it
   does not yet compare a *successfully returned* `protocol_version`
   against what the client expects.
4. **`view/`'s per-layer toggle UI described in specification.md §4.8
   is not implemented** — the renderer supports filtering by layer, but
   the viewer never uses that parameter; every layer always renders.
5. **`camera probe`'s presentation currently disagrees with `camera
   list`** for calibration/playfield/skip state (filed:
   `clasi/issues/camera-probe-reports-everything-uncalibrated-and-renders-skipped-cameras-as-data.md`)
   — a `daemon/service.py` presentation bug, not a defect in `camera/`'s
   own `ProbeReport` contract.
6. **Resolved as a side effect of item 1, sprint 036: `cli/locate.py` no
   longer imports cv2 transitively at all** (the `calibration/geometry.py`
   split removed the accidental dependency, not a deliberate widening of
   `tests/test_no_cv2_in_client_modules.py`'s enforced package set — see
   `cli/DESIGN.md` §3/§6). `cli/` is still not in that test's mechanically
   enforced `_CV2_FREE_PACKAGES` set, so a future change reintroducing a
   `cv2` import into `cli/` would not be caught by that gate — this
   remains a discipline/code-review boundary, not a test-gated one.
