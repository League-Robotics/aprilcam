---
status: pending
---

# Plan: Named, persistent playfields as the single source of truth

## Context

Today AprilCam has **two disconnected notions of "playfield"** and they disagree:

- **Geometry definition** — [data/aprilcam/playfield.json](data/aprilcam/playfield.json):
  the real field. **134.3 × 89.3 cm, center origin** (AprilTag A1 = 0,0; X east, Y
  north), with the full marker map (april_tags, 8 perimeter aruco_tags at the
  cardinal/diagonal positions, rectangles, dots). Used only by the `where` tool via
  [playfield_query.py](src/aprilcam/core/playfield_query.py). **This is the correct,
  authoritative geometry.**
- **Per-camera calibration** — e.g.
  [calibration.json](data/aprilcam/cameras/arducam-ov9782-usb-camera/calibration.json):
  stores its *own* `playfield: {width: 109, height: 79.5}` plus `static_markers` whose
  world coords are nonsense (three "corners" on the same line). This geometry is
  **wrong** and must be discarded.
- The runtime context (`PlayfieldEntry`, `pf_<camera_id>`) is **in-memory only** and
  rebuilt by an explicit `create_playfield` call every session — nothing loads at
  startup.

**Goal:** Make the playfield definition the single source of truth. Playfields become
named, persistent files in a `playfields/` directory, loaded at startup. A camera
references one playfield via a new per-camera `config.json` we own (the daemon never
writes it). Calibration *always* pulls its dimensions, origin, and corner world
positions from the referenced playfield — so a camera/playfield mismatch is
structurally impossible. Calibrating without a configured playfield is a hard error
that tells the user exactly what to set up.

### Decisions locked with the stakeholder
- **Naming:** `playfields/<slug>.json`; inner `name` == filename stem, plus optional
  `display_name` for human labels.
- **Calibration source of truth:** the playfield definition. `calibration.json` no
  longer owns dimensions — it records *which* playfield + camera it was calibrated to
  (provenance) and a snapshot, but the def always wins.
- **Camera→playfield link:** new `data/aprilcam/cameras/<slug>/config.json`, owned by
  us, daemon never overwrites. Sole key: `playfield`.
- **Migration:** move `playfield.json` → `playfields/main-playfield.json`; write
  `config.json` referencing `main-playfield` for the three calibrated cameras
  (arducam-ov9782, hd-usb-camera, global-shutter-camera).

### Consequence the stakeholder accepted
Making the def authoritative changes the world-coordinate system to the def's **center
origin** (was corner origin). All existing calibrations are therefore wrong and must be
re-run; existing stored paths (in cm) no longer align and should be cleared. This is
expected — the old calibration was broken.

---

## Design

### Data model (on disk)

**`data/aprilcam/playfields/<slug>.json`** — the migrated/renamed definition, unchanged
schema plus identity fields:
```jsonc
{
  "name": "main-playfield",          // == filename stem; canonical reference id
  "display_name": "Main Playfield",  // optional, human label
  "playfield": { "width_cm": 134.3, "height_cm": 89.3, "origin": "apriltag-center-a1", "description": "..." },
  "april_tags": [...], "aruco_tags": [...], "rectangles": [...], "dots": [...]
}
```
The 4 **corner** ArUco markers are the diagonal cardinals already present
(`northwest`/`northeast`/`southeast`/`southwest` → UL/UR/LR/LL) at (±67, ±44.65). These
supply both the deskew rectangle and the world coordinates used to compute the
homography. Corner ArUco **IDs come from the def** (1/3/5/7 here), replacing the
hardcoded "IDs 0-3" assumption in the current detector.

**`data/aprilcam/cameras/<slug>/config.json`** — new, we own it, daemon never writes:
```json
{ "playfield": "main-playfield" }
```

**`calibration.json`** — keeps homography/camera_matrix/dist_coeffs/camera_position/
settings, but:
- `playfield: {width,height}` becomes a **derived snapshot** of the def (not a source).
- Add provenance: `calibrated_playfield: "<slug>"`, `calibrated_camera: "<slug>"`.
- On load, if `calibrated_playfield` ≠ current camera `config.json` playfield, or the
  def's dimensions changed since, mark the calibration **stale/mismatched** and warn.

### Code components

1. **Playfield definition model + registry** — new
   `src/aprilcam/core/playfield_def.py`: `PlayfieldDefinition` dataclass (load from
   `<slug>.json`, expose `name`, `display_name`, `width_cm`, `height_cm`, `origin`,
   marker lists, and a `corner_world_coords()` / `corner_aruco_ids()` helper).
   `PlayfieldDefinitionRegistry.load_all(dir)` scans `playfields/` at startup.
2. **Config plumbing** — extend [config.py](src/aprilcam/config.py) with
   `playfields_dir` (default `data/aprilcam/playfields/`) and a
   `playfield_def_path(name)` helper. Add per-camera config read/write helpers
   (`load_camera_config(camera_dir)` / `save_camera_config(...)`) in a small module;
   the daemon's [info.json writer](src/aprilcam/daemon/camera_pipeline.py) is left
   untouched (it only ever writes `info.json`, so `config.json` is safe).
3. **Startup load** — in [mcp_server.py `main()`](src/aprilcam/server/mcp_server.py)
   (after the module registries near line 235), build the
   `PlayfieldDefinitionRegistry`. Repoint the `where` tool /
   [playfield_query.load_playfield](src/aprilcam/core/playfield_query.py) at the
   registry / new location (keep a fallback to the old path during transition).
4. **Auto-wire on open** — in
   [`_handle_open_camera`](src/aprilcam/server/mcp_server.py#L455): read the camera's
   `config.json` → playfield slug → load def + calibration → reconstruct the runtime
   `PlayfieldEntry` from disk. This delivers "playfields exist at startup; no explicit
   create needed." `create_playfield` remains for first-time/un-calibrated bootstrap.
5. **Calibration refactor (shared)** — a single helper used by **both** entry points
   ([MCP `calibrate_playfield`](src/aprilcam/server/mcp_server.py#L1756) and the
   `aprilcam calibrate` CLI that calls
   [save_calibration_to_camera_dir](src/aprilcam/calibration/calibration.py#L340)):
   - Resolve camera → `config.json` → playfield slug. **If missing → hard error**:
     `"Camera '<id>' has no playfield configured. Create
     data/aprilcam/cameras/<slug>/config.json with {\"playfield\": \"<name>\"}.
     Available playfields: [...]"`. If the named def doesn't exist → error listing
     available defs.
   - Pull dimensions + corner ArUco IDs + corner **world** coords (center origin) from
     the def. Detect those corner IDs in pixels, compute pixel→world homography in the
     def's coordinate system. Drop the `width`/`height` parameters as a source of truth
     (derive instead).
   - Write `calibration.json` with the derived dimension snapshot + provenance fields.
6. **Mismatch detection** — in
   [load_calibration_from_camera_dir](src/aprilcam/calibration/calibration.py#L305) /
   the open-camera rehydrate path: compare provenance against current config + def;
   surface a warning and a `calibration_stale`/`mismatch` flag rather than silently
   trusting stale geometry.
7. **(Optional ergonomics)** MCP tool `set_camera_playfield(camera_id, playfield)` that
   writes `config.json`, so an agent can wire a camera without hand-editing files.

### Migration (one-time, scripted as part of execution)
- `git mv data/aprilcam/playfield.json data/aprilcam/playfields/main-playfield.json`;
  add `name: "main-playfield"`, `display_name: "Main Playfield"`.
- Write `config.json` = `{"playfield": "main-playfield"}` into the dirs of
  arducam-ov9782-usb-camera, hd-usb-camera, global-shutter-camera.
- Do **not** rewrite the broken homographies. They will be flagged stale/mismatched and
  must be recalibrated; clear their stale `paths.json` (already cleared for arducam).

---

## Critical files
- New: [src/aprilcam/core/playfield_def.py](src/aprilcam/core/playfield_def.py),
  per-camera config helpers (new small module or in
  [config.py](src/aprilcam/config.py)).
- [src/aprilcam/server/mcp_server.py](src/aprilcam/server/mcp_server.py) — startup load,
  `_handle_open_camera` rehydrate, `calibrate_playfield` precondition + def sourcing,
  `create_playfield` reconciliation, optional `set_camera_playfield`.
- [src/aprilcam/calibration/calibration.py](src/aprilcam/calibration/calibration.py) —
  shared calibrate-from-def helper, provenance fields, mismatch detection in loader.
- [src/aprilcam/core/playfield_query.py](src/aprilcam/core/playfield_query.py) /
  `where` tool — read from registry / new location.
- [src/aprilcam/config.py](src/aprilcam/config.py) — `playfields_dir`.
- The `aprilcam calibrate` CLI entry point (shares the new helper).
- Migration of `data/aprilcam/playfield.json` and three `config.json` files.

## Reuse (don't reinvent)
- `PlayfieldDefinitionRegistry` mirrors the existing
  [PathRegistry](src/aprilcam/server/paths.py)/`PlayfieldRegistry` patterns.
- Corner→world homography: reuse `calibrate_from_corners` /
  `corner_pixels_from_homography` / `metric_deskew_matrix` in
  [calibration/](src/aprilcam/calibration/calibration.py) and `geometry.py`; feed them
  per-corner world coords from the def instead of an assumed 0,0..W,H rectangle.
- Atomic write pattern (`.tmp` + `os.replace`) already used for paths/calibration/info.

## Likely ticket breakdown (for CLASI sprint execution)
1. Config: `playfields_dir` + per-camera `config.json` read/write helpers.
2. `PlayfieldDefinition` + registry + startup load; repoint `where`.
3. Migrate `playfield.json` → `playfields/main-playfield.json`; write 3 `config.json`.
4. Calibration refactor: derive geometry from def, precondition error, provenance;
   wire both MCP and CLI calibrate entry points.
5. `open_camera` auto-rehydrate of `PlayfieldEntry` from disk; mismatch flagging.
6. (Optional) `set_camera_playfield` MCP tool.
7. Tests + docs (ROBOT_API_GUIDE/AGENT_GUIDE) + version bump.

---

## Risks / consequences
- **Coordinate-system change (center origin).** World coords from calibrated cameras
  change. All cameras must be recalibrated; existing paths in cm are invalidated (clear
  them). Accepted by stakeholder.
- **Corner ArUco IDs change** from hardcoded 0-3 to the def's diagonal-cardinal IDs
  (1/3/5/7). The detector must read IDs from the def; verify the physical field markers
  match the def.
- **Two calibrate entry points** (MCP + CLI) must share one code path or they'll
  diverge — enforce via the shared helper.
- **Daemon boundary:** the daemon consumes the stored homography only; it needs no
  playfield awareness. Confirm it never writes `config.json` (it writes only
  `info.json`/calibration via the calibrate path).

## Verification
1. **Unit:** def load + registry scan; `config.json` round-trip; calibrate precondition
   raises the guidance error when no playfield configured / def missing; calibrate
   derives dims+corners from the def (assert ignores any stale calibration numbers);
   mismatch detection flags provenance drift; startup rehydrate builds a `PlayfieldEntry`
   from disk with no `create_playfield` call. Run `pytest`; extend
   [test_calibration_geometry_persist.py](tests/test_calibration_geometry_persist.py),
   [test_camera_registry.py](tests/unit/test_camera_registry.py),
   [test_mcp_path_tools.py](tests/test_mcp_path_tools.py); add
   `tests/test_playfield_def.py`.
2. **End-to-end (MCP):** restart server → `list_cameras` → `open_camera` on the arducam
   → confirm a playfield context exists without calling `create_playfield`, and that a
   mismatch warning fires against the old calibration. Then `calibrate_playfield` →
   confirm it reads 134.3×89.3 from the def, writes provenance, and `pixel_to_world` of
   AprilTag A1 ≈ (0,0). `clear_paths` then works through the registry and rewrites
   `paths.json`.
3. **Negative:** `open_camera` on a camera with no `config.json` → `calibrate_playfield`
   returns the precise "create config.json with {playfield: ...}" error.
4. Bump version in [pyproject.toml](pyproject.toml) per project convention.

> Execution note: this is a multi-ticket architectural change in a CLASI project. After
> approval, run it as a CLASI sprint (plan-sprint → tickets → execute) rather than ad-hoc
> edits.
