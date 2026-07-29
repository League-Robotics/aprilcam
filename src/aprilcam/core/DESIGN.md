# Core (`aprilcam.core`)

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** in-flux

---

## 1. Purpose

`core/` is the stateful vision engine the daemon runs per frame: tag
detection/tracking cadence, playfield polygon geometry and filtering,
velocity estimation, world-frame conversion conventions, and the immutable
record types (`TagRecord`/`FrameRecord`/`RingBuffer`) that everything
downstream serialises. The seam it owns is *frame in → tag records out* with
all the temporal state (tracks, tag models, EMA smoothers, held static
markers) that a single stateless detector cannot carry. It also holds the
cv2-free playfield-definition schema (`playfield_def.py`) and the "where is
X" query engine (`playfield_query.py`) consumed by daemon RPCs.

## 2. Orientation

**The daemon's one true path is `aprilcam.py:AprilCam.process_frame()`** —
called by `daemon/camera_pipeline.py` per captured frame. It orchestrates:
`detector.py:TagDetector` (stateless per-family ArUco/AprilTag detection
with illumination-flattening preprocessing), LK optical-flow tracking
between detects, `playfield.py:PlayfieldBoundary` (polygon detection/
seeding, static-marker fill-in, in-polygon filtering, velocity), and
`models.py:AprilTag` (per-tag pose model, yaw convention). Results become
frozen `detection.py:TagRecord`s.

**A second, duplicate pipeline exists and the daemon does NOT use it**:
`pipeline.py:DetectionPipeline` + `motion.py:VelocityEstimator` +
`tracker.py:OpticalFlowTracker` + the user-facing `playfield.py:Playfield`
class + `tag.py:Tag` + `detection.py:DetectionLoop`. It reimplements the
same detect/track/velocity/buffer flow behind a library-style API. Treat it
as **deprecated — v2 removes `pipeline.py` and `motion.py` outright** (spec
§4.3, §6) and folds the surviving stateless pieces into `vision/`.

`playfield_def.py` (`PlayfieldDefinition`, `PlayfieldDefinitionRegistry`)
loads/serves named playfield JSON documents; `playfield_query.py` answers
keyword "where is X" queries over them. Both are cv2-free, as is
`detection.py`; the package `__init__` lazy-loads the cv2-requiring names.

## 3. Constraints and Invariants

- **Never y-flip world coordinates or headings.** The def-driven homography
  is A1-centred with +y=north; world deltas out of it are already in the
  reported y-up frame. `models.world_yaw()` (which negates y) is *only* for
  the raw y-down pixel fallback — using it on world-space deltas inverts
  every heading (`models.py:41-60`). Same rule in
  `PlayfieldBoundary.add_tag`'s world-velocity path (`playfield.py:531-546`).
- **Pre-filter detections must reach the boundary before polygon
  filtering** (`aprilcam.py:585-588`): static AprilTag sentinels (e.g. tag
  1 under the field edge) can sit outside the polygon; feeding only
  post-filter tags starves fill-in and movement invalidation.
- **`isIn()` returns True when no polygon is known** — no polygon means no
  filtering, not "reject everything". Detection must degrade gracefully on
  an uncalibrated camera.
- **Velocity deadband**: EMA-smoothed speed below 50 px/s reports velocity
  (0,0) and speed 0 (`playfield.py:498-528`). Consumers must not treat zero
  velocity as "perfectly still" — it means "below the noise floor".
  World velocity is computed only when `speed_px > 0` (deadband passed).
- **Tag records age, they don't vanish**: a tag unseen ≤ 1 s is still
  emitted with `age > 0` (position frozen at last sighting); models are
  pruned only after 1 s (`aprilcam.py:612-664`). Consumers filter on `age`,
  not presence.
- **`calibration_stale` is sticky with a symmetric reset**: set when any
  detected static marker drifts beyond the movement threshold; cleared only
  when a cycle checks ≥ 1 marker and *all* checked markers are back within
  threshold. A cycle with nothing detected leaves the flag untouched — no
  positional evidence, no change (`playfield.py:331-365`).
- **Corner layouts**: live-corner mode accepts ArUco IDs 0-3 or the
  8-marker perimeter's 1,3,5,7 as corners (`_order_poly`); ordering is
  geometric (sort by y then x), tolerating ID swaps. The playfield-def
  world uses ArUco IDs 1-8 for the perimeter; interior markers are ≥ 9.
- **Yaw convention** (`models.py`): ENU/REP-103 math angles — x right,
  y up (north), 0 = +X, CCW positive; `forward = (cos yaw, sin yaw)`. With
  a homography, heading is measured in *world* space (centre → top-mid
  through H) so camera rotation cancels.
- **`TagRecord`/`FrameRecord` are frozen and numpy-free** — they cross
  thread and serialisation boundaries; keep them plain-Python.
- **cv2-free import discipline**: `detection`, `playfield_def`,
  `playfield_query`, `models`, `tag`, `motion` must import without cv2;
  `playfield.py` resolves cv2 lazily per-method. `__init__` lazy-maps
  `TagDetector`/`OpticalFlowTracker`/`DetectionPipeline`.

## 4. Design

**Detect-vs-track cadence** (`AprilCam.process_frame`). Full detection runs
when `detect_interval <= 1`, every `detect_interval`-th frame (default 3),
on the first frame, or when no tracks survive. Between detects, each
tracked tag's 4 corners are advanced by pyramidal LK optical flow
(21×21 window, 3 levels); a tag whose flow fails (< 4 good points) is
dropped from tracks. If *every* track is lost mid-interval, the frame falls
back to a full detection immediately — tracking is an optimisation, never a
correctness dependency. Rationale: full multi-family ArUco detection at
1080p is the per-frame cost driver; LK at interval 3 roughly triples
throughput with sub-pixel continuity.

**Per-frame order matters**: detect/track → boundary update (with
pre-filter tag centres) → polygon `isIn` filter (tracks re-synced to
survivors) → tag-model update + `boundary.add_tag` (velocity) → prune stale
models → build current (age 0) records → append aged records.

**PlayfieldBoundary two modes.** *Live-corner mode* (no stored static
markers): detect 4x4 ArUco corners every `corner_detect_interval` (30)
frames, EMA-smooth the polygon (`ema_alpha`) against jitter. *Static mode*
(calibration carries `static_markers` — auto-on, overridable via
`APRILCAM_STATIC_DESKEW`): stored pixel positions of corners and
`apriltag:N` sentinels are the working set; undetected static markers HOLD
their stored position (fill-in — the field edge can be occluded
indefinitely), detected ones update the held position and are compared to
the stored position for movement invalidation (> threshold ⇒
`calibration_stale`). Dynamic tags (any id not declared static) are never
held. The polygon is rebuilt each frame from held+live corner keys. Seeding
comes from the daemon (`_seed_static_geometry`): stored `corner_pixels`, or
corners derived from H⁻¹ + field dimensions when absent, so deskew works
before any live ArUco detection.

**Velocity** (`PlayfieldBoundary.add_tag`): finite difference of tag centre
vs `_last_seen` (dt floored at 1 ms), instantaneous speed EMA-smoothed,
deadband applied (see §3). World velocity transforms *both* p and p+v
through H and differences them — correct under projective H where a linear
map of v alone is not. Heading = `atan2(wvy, wvx)` directly (no flip).

**Deskew** (`deskew_transform`): with known field dimensions the output is
a *metric* rectangle `(W·px_per_cm, H·px_per_cm)` via
`calibration.geometry.metric_deskew_matrix` — pixel positions in the
deskewed image are linear in cm. Without dimensions it falls back to a
rectangle sized from polygon edge lengths. `deskew()` masks to the polygon
before warping so out-of-field pixels don't bleed in.

**Records.** `RingBuffer` is a lock-guarded `deque(maxlen=300)` (~10-30 s of
history); `TagRecord.estimate(t)` linearly extrapolates position by stored
velocity for latency compensation on the client side.

**Playfield definitions.** `PlayfieldDefinitionRegistry` is populated once
(from disk via `load_all`, or from `ListPlayfields` RPC blobs via
`add_from_dict` on the MCP side); no I/O at access time. Corner ArUcos are
the entries whose `cardinal` is a diagonal (NW/NE/SE/SW).

## 5. Interfaces

### Exposes

- `AprilCam(index, …, cap=, homography=, headless=True, **pipeline_cfg)` —
  constructor kwargs double as the `calibration.json` `pipeline` section
  schema. `process_frame(frame_bgr, timestamp) → list[TagRecord]` is the
  daemon contract: stateful, no I/O, no windows, never reads a camera.
  `homography` is assignable live (calibration reload). `run()` is the
  interactive debug loop (keyboard-driven views/object detection) — not a
  service path.
- `PlayfieldBoundary` — seeding fields (`homography`, `width_cm`,
  `height_cm`, `px_per_cm`, `static_markers`, `static_marker_ids`,
  `static_mode`, `movement_threshold_px`), `update(frame, gray, apriltags)`,
  `get_polygon()`, `isIn(pts|xy)`, `add_tag(tag, homography)`,
  `get_flows()`, `deskew_transform()/deskew()/get_deskew_matrix()`,
  `calibration_stale` (read-only property).
- `TagRecord` / `FrameRecord` / `RingBuffer` — the serialisation-stable
  record types (`to_dict()`, `estimate(t)`, `from_apriltag(...)`).
- `TagDetector` / `DetectorConfig` / `Detection` — stateless detection
  (proc_width downscale with corner rescale, highpass/CLAHE/sharpen
  preprocessing, per-family detector objects built once).
- `models.AprilTag`, `AprilTagFlow`, `world_yaw` — pose model + frame
  conventions.
- `PlayfieldDefinition` / `PlayfieldDefinitionRegistry`;
  `playfield_query.load_playfield_map` / `iter_features` / `where`.
- Deprecated surface (second pipeline): `Playfield` (user-facing class),
  `DetectionPipeline`, `OpticalFlowTracker`, `VelocityEstimator`, `Tag`,
  `DetectionLoop`.

### Consumes

- `aprilcam.vision.aruco_compat` — OpenCV 4.6/4.7 ArUco API shim (owned by
  vision/).
- `aprilcam.calibration.geometry` — `corner_pixels_from_homography`,
  `metric_deskew_matrix`, defaults (owned by calibration/).
- `aprilcam.camera.camutil` — device names, enumeration, failure diagnosis
  (owned by camera/).
- `aprilcam.ui.display.PlayfieldDisplay` — pulled in by `AprilCam.__init__`
  even headless (legacy coupling).
- `aprilcam.config.AppConfig` / `aprilcam.iohelpers` — legacy loaders still
  imported by `aprilcam.py` (v2 removes both, spec §4.11, §6).

## 6. Open Questions / Known Limitations

- **Duplicate second pipeline**: `pipeline.py` + `motion.py` (plus
  `tracker.py`, `tag.py`, the `Playfield` class, `DetectionLoop`) duplicate
  the daemon path with subtly independent state handling — two velocity
  implementations, two LK trackers. **v2 removes `core/pipeline.py` and
  `core/motion.py`** (spec §6) as unused-by-daemon duplicates; do not add
  features there.
- **v2 consolidation (spec §4.3)**: the stateless pieces (`TagDetector`,
  tracking) merge into `vision/`; the stateful per-camera pipeline
  (`AprilCam.process_frame` and its state) moves into `daemon/`. This
  package as a standalone subsystem largely dissolves in v2; the record
  types and playfield-definition handling live on (`playfield/` becomes
  its own subsystem per spec §4.5).
- **`AprilCam` carries dead interactive weight** on the daemon path: TUI
  rendering, `run()`'s keyboard loop, `PlayfieldDisplay` construction, and
  legacy `AppConfig`/`iohelpers` imports (spec §6 removes the legacy
  loaders; the viewer becomes its own subsystem, spec §4.8).
- **ArUco id namespace**: `process_frame` emits ArUco 4x4 detections with
  their raw non-negative ids in the same space as AprilTags when
  `detect_aruco_4x4` is enabled; the `-(N+1)` negative-id convention that
  prevents collisions is applied in `calibration/` (e.g.
  `calibration.py:1257`), not here — a retained system invariant (spec §7)
  that this layer silently depends on.
- **`_update_playfield` swallows all exceptions**; geometry bugs degrade to
  "no polygon" silently. Acceptable for robustness, hostile to debugging.
- `Playfield._seed_boundary_geometry` and the daemon's
  `_seed_static_geometry` are parallel implementations of the same seeding
  logic and can drift; v2's pipeline consolidation should leave exactly one.
