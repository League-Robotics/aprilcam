# Calibration

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** in-flux

---

## 1. Purpose

Establishes and persists the pixel→world mapping for a camera looking at a
playfield: homography, optional lens-distortion model, metric deskew geometry,
and the static-marker reference set. The seam is *math and persistence, not
capture*: this package never opens a camera — it consumes an already-open
capture object and a playfield definition, and it alone owns the shape and
write discipline of `calibration.json`. Nothing else in the system may compute
or write a homography.

## 2. Orientation

Three modules with a deliberate layering:

- **`calibration.py`** — the data layer (`CameraCalibration`, `FieldSpec`,
  `CameraPosition`, load/save helpers, `parse_calibration_from_dict`) plus the
  workflow functions. The data layer is cv2-free (cv2 is imported lazily and
  tolerated missing) so thin clients can deserialize calibrations without
  OpenCV. The primary workflow entry point is `calibrate_from_playfield_def`
  (calibration.py:1103); legacy workflows (`calibrate_single`,
  `calibrate_joint`, `calibrate_secondary`, `calibrate`) remain for older
  paths.
- **`geometry.py`** — a pure leaf: no I/O, no detectors, only NumPy plus
  `cv2.getPerspectiveTransform`. Provides `corner_pixels_from_homography`
  (recover corner pixels from saved H + field dims via H⁻¹) and
  `metric_deskew_matrix` (warp a source polygon to a metric top-down
  rectangle). This is what makes static-camera deskew possible from saved
  geometry with no live corner detection.
- **`homography.py`** — `compute_homography` (DLT), `detect_all_tags`
  (multi-frame averaged AprilTag + ArUco detection), and a *legacy* `homocal`
  CLI with its own hardcoded corner-world table.

The main flow (`calibrate_from_playfield_def`): camera `config.json` →
playfield-definition link → corner ArUcos and surveyed world coordinates from
the definition → detect over N frames → homography → augmentation → optional
distortion → static-marker recording → `CameraCalibration` with provenance →
ordered writes. Consumers are the daemon pipeline, the CLI `calibrate`
command, and the MCP `calibrate_playfield` tool (the latter via `skip_save`).

## 3. Constraints and Invariants

- **ArUco ID N is stored as detection tid `-(N+1)`** — everywhere: detection
  dicts, corner matching, the augmentation map. Mixing conventions silently
  matches the wrong physical marker and produces a plausible-looking but
  wrong homography.
- **World frame is A1-centred, +y = north** (`geometry._world_corners`:
  UL=(-W/2, H/2), UR=(W/2, H/2), LR=(W/2, -H/2), LL=(-W/2, -H/2)). Origin
  shifts are pure subtraction on both axes — a y-flip (`origin_y - y`) has
  already caused an inverted north/south bug once. Never reintroduce it.
- **Write ordering: `calibration.json` before `config.json`** — the state
  write is time-critical (get_tags depends on it); a `config.json` write
  failure is logged and swallowed, never rolled back and never masks a
  successful calibration (calibration.py:1391-1411). Reordering breaks the
  freshness guarantee; propagating the config error would discard a good
  calibration.
- **`CONFIG_OWNED_CALIBRATION_KEYS` split** — `device_name`, `resolution`,
  `settings`, `camera_position`, `static_marker_ids` live in `config.json`
  only. `to_camera_json` / `save_calibration_to_camera_dir` strip them; the
  loader overlays them from `config.json` (config wins over legacy values).
  Duplicating them into `calibration.json` re-creates the two-sources-of-truth
  drift the split exists to kill.
- **`calibration_stale` is a transient attribute, not a field** — set by
  `load_calibration_from_camera_dir` on slug/dimension mismatch or missing
  provenance; never serialized. Callers must use
  `getattr(cal, "calibration_stale", False)`; adding it as a dataclass field
  would persist staleness across a recalibration.
- **Static-marker corner keys derive from PIXEL geometry, not world coords**
  (`_build_static_markers._corner_key`). The live movement check in
  `core/playfield.py` orders corners by pixel position; keying by world order
  mislabels LR↔LL and produces a spurious ~field-width "marker moved" flap.
- **AprilTags pad but do not constrain the homography** — their world
  positions are derived *through the initial H*, so they add points (enough
  for `calibrateCamera`) without adding independent information. Only ArUcos
  with surveyed positions genuinely constrain the fit. Do not treat
  `tags_used` as a count of independent ground-truth points.
- **Homography applies to raw pixels unless distortion was fitted** — when
  distortion correction runs, H is recomputed from *undistorted* points and
  RMS must use the same undistorted set (calibration.py:1342-1346).
- **`geometry.py` stays a pure leaf** — no I/O, no detection. It is
  single-sourced so daemon deskew and any display path share identical math.
- **Trap: `homography.py` ARUCO_CORNER_WORLD** is an upper-left-origin table
  (0,0 at UL) — the *opposite* convention from the A1-centred frame. It feeds
  only the legacy `homocal` CLI. Never use it in new code.
- **Trap: `CameraPosition` docstring says "origin is the playfield center"**
  while `correct_world_for_height` documents A1-centred offsets. In practice
  A1 sits at the field centre so the values coincide, but the authoritative
  convention is A1-centred.

## 4. Design

**Data.** `CameraCalibration` is the single persisted artifact: 3×3
pixel→world homography, optional intrinsics/dist_coeffs, playfield dims (cm),
`corner_pixels` (calibration-time [UL,UR,LR,LL] pixel positions, seeds
static deskew), `static_markers` (per-id pixel+world of the fixed reference
set, default `aruco_corners + apriltag:1`), and provenance
(`calibrated_playfield`, `calibrated_camera`) used for staleness detection.
`calibration.json` is regenerable state; unknown keys in an existing file are
preserved on save (user-managed), while keys in the owned `_CALIBRATION_KEYS`
set are dropped/rewritten.

**Flow of `calibrate_from_playfield_def`.** (1) load `config.json` (or accept
a pre-supplied dict from the MCP/RPC path); missing or unlinked →
`PlayfieldConfigError` with the available-playfields list. (2) fetch the
`PlayfieldDefinition` from the registry. (3) map the def's corner ArUco IDs to
tids and their surveyed world coords. (4) `detect_all_tags` over `num_frames`
(default 30), averaging pixel centers for jitter suppression. (5) require all
4 expected corners else `RuntimeError` naming expected vs. found. (6) initial
homography via `cv.findHomography(method=0)` — plain DLT, no RANSAC: with 4
hand-surveyed corners there are no outliers to reject, and RANSAC on minimal
sets is noise. (7) augment with every *other* detected ArUco the definition
surveys (edge midpoints, interior markers like west/east 9/10) at their TRUE
world xy — these genuinely improve the fit — plus AprilTags at H-derived
positions. (8) when `correct_distortion` and ≥6 points: single-planar-view
`cv.calibrateCamera`, `undistortPoints`, recompute H from undistorted points.
This is a rough model (one planar view under-constrains intrinsics) accepted
deliberately over a checkerboard procedure for operational simplicity.
(9) record corner pixels + static markers. (10-11) build the calibration with
provenance. (12-13) persist per the write-ordering rule, or with
`skip_save=True` stash the merged config as `cal._merged_config` for the
caller to push over RPC.

**Parallax.** `correct_world_for_height` applies
`w' = w + (h/H)·(offset − w)` per axis — a similar-triangles pull of an
elevated tag's apparent position toward the point under the camera. No-op when
camera height or tag height is zero. Robot-specific data (tag height, mount
offset) never enters calibration itself — it arrives per-tag at runtime.

**Deskew sizing.** `DEFAULT_PX_PER_CM = 10.0` in geometry.py sizes the metric
deskew output as `round(W·s) × round(H·s)`. This "choose a resolution
constant" approach is legacy thinking — see §6.

## 5. Interfaces

### Exposes

- `calibrate_from_playfield_def(cap, camera_dir, camera_slug, playfield_def_registry, num_frames=30, correct_distortion=True, camera_position=None, camera_config=None, skip_save=False, config_dir=None) -> CameraCalibration`
  — the entry point. Raises `PlayfieldConfigError` (no config / unlinked /
  unknown slug), `RuntimeError` (<4 corners). `skip_save=True` performs no
  filesystem access and attaches `_merged_config`. `config_dir` is
  *write-only* (sprint 019 config/state split); the fallback config read stays
  on `camera_dir`.
- `CameraCalibration` — `pixel_to_world(u, v)`, `undistort(frame)`,
  `correct_world_for_height(wx, wy, tag_height_cm)`, `to_dict()`,
  `to_camera_json()` (canonical `calibration.json` serializer — strips
  config-owned keys), `from_dict()` (tolerates legacy records: missing
  geometry fields default to None/0).
- `load_calibration_from_camera_dir(camera_dir, camera_config=None, playfield_def=None, config_dir=None)` —
  load + config overlay + optional staleness detection (sets transient
  `calibration_stale`). Returns `None` on missing/corrupt file.
- `save_calibration_to_camera_dir(cal, camera_dir, field_width_cm, field_height_cm, detection_fps=10)` —
  canonical writer; preserves user-managed keys and an existing
  `detection_fps`.
- `parse_calibration_from_dict(d)` — pure companion for RPC-blob consumers.
- `geometry.corner_pixels_from_homography(H, width_cm, height_cm)` and
  `geometry.metric_deskew_matrix(poly, width_cm, height_cm, px_per_cm)` —
  the static-deskew primitives. Corner order UL, UR, LR, LL throughout.
- `homography.compute_homography(pixel_pts, world_pts)` /
  `homography.detect_all_tags(cap, num_frames)`.
- Constants: `DEFAULT_STATIC_MARKER_IDS`, `CONFIG_OWNED_CALIBRATION_KEYS`,
  `geometry.DEFAULT_PX_PER_CM`, `geometry.DEFAULT_MOVEMENT_THRESHOLD_PX`.

### Consumes

- `aprilcam.camera.camera_config` (load/save config.json),
  `aprilcam.camera.camutil` (device names), `aprilcam.core.playfield_def`
  (definition registry protocol: `get(name)`, `list()`, `corner_aruco_ids()`,
  `corner_world_coords()`, `aruco_tags`, `width_cm`/`height_cm`),
  `aprilcam.vision.aruco_compat`, OpenCV (workflow only), NumPy.

## 6. Open Questions / Known Limitations

- **Runs in the wrong process today.** The CLI and MCP paths execute this
  math locally, importing cv2 and breaking the vision boundary. v2: a
  `Calibrate` RPC executes calibration *inside the daemon*; CLI/MCP become
  pure orchestrators (spec §4.4). The `skip_save` / `_merged_config` shim
  exists only to bridge until then.
- **`DEFAULT_PX_PER_CM` is superseded.** v2 replaces chosen-resolution deskew
  with fixed bounding-box sizing (default 800×800, per-playfield override);
  px-per-cm becomes a derived, reported property (spec §4.1, decision §9.1).
- **`homography.py` legacy half is removal-listed.** The `homocal` CLI and
  the upper-left-origin `ARUCO_CORNER_WORLD` table are superseded by
  playfield-def calibration and explicitly removed in v2 (spec §6), along
  with the unified-file scheme (`save_calibration`, `calibrate_joint`,
  `calibrate_secondary`, `discover_homography`) that nothing current uses.
- **Distortion model quality** — single planar view under-constrains
  `calibrateCamera`; treated as good-enough for a fixed overhead camera.
  Revisit only if RMS demands it.
- **Docstring contradiction** (`CameraPosition` "playfield center" vs.
  A1-centred) should be fixed when the module moves into the daemon.
