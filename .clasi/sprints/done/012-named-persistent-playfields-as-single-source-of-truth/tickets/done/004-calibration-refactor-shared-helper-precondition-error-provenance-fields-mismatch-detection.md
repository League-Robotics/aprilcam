---
id: '004'
title: 'Calibration refactor: shared helper, precondition error, provenance fields,
  mismatch detection'
status: done
use-cases:
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
- '002'
- '003'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Calibration refactor: shared helper, precondition error, provenance fields, mismatch detection

## Description

The largest structural ticket. Refactors `src/aprilcam/calibration/calibration.py`
to make the playfield definition the single source of truth for calibration
geometry, and ensures both the MCP tool and CLI share one code path.

### A. New `PlayfieldConfigError` exception

```python
class PlayfieldConfigError(Exception):
    """Raised when a camera has no playfield configured or the def is missing."""
```

### B. New shared function `calibrate_from_playfield_def`

```python
def calibrate_from_playfield_def(
    cap,                            # VideoCapture-compatible (read/get)
    camera_dir: Path | str,
    camera_slug: str,               # for error messages and provenance
    playfield_def_registry,         # PlayfieldDefinitionRegistry
    num_frames: int = 30,
    correct_distortion: bool = True,
    camera_position: "CameraPosition | None" = None,
) -> "CameraCalibration":
```

Internal steps:
1. Load `config.json` via `load_camera_config(camera_dir)`.
2. If `None` → raise `PlayfieldConfigError` with message:
   `"Camera '<camera_slug>' has no playfield configured. Create
   data/aprilcam/cameras/<camera_slug>/config.json with
   {\"playfield\": \"<name>\"}. Available playfields: [<names>]"`.
3. Extract `playfield_slug = config["playfield"]`.
4. Call `playfield_def_registry.get(playfield_slug)`. If `KeyError` → raise
   `PlayfieldConfigError`: `"Playfield '<slug>' not found in registry. Available
   playfields: [<names>]"`.
5. Read `def.corner_aruco_ids()` and `def.corner_world_coords()`.
6. Call `detect_all_tags(cap, num_frames)` to get current detections.
7. Match detected ArUco tags against the def's corner IDs. The def's IDs are
   positive integers (1/3/5/7), stored as negative tids (-2/-4/-6/-8) in the
   detection dict. Build `pixel_corners` and `world_corners` lists from the
   detected positions for those specific IDs. If fewer than 4 are found →
   raise `RuntimeError` with a clear message about which IDs were expected vs.
   found.
8. Call `compute_homography(pixel_pts, world_pts)` to get `H`.
9. Optionally run distortion correction and multi-tag refinement (same logic
   as `calibrate_single`; reuse or refactor `calibrate_single` to delegate
   here).
10. Build `static_markers` via `_build_static_markers(...)`.
11. Construct `CameraCalibration` with provenance fields:
    `calibrated_playfield=playfield_slug`, `calibrated_camera=camera_slug`.
12. Call `save_calibration_to_camera_dir(cal, camera_dir, def.width_cm,
    def.height_cm)`.
13. Return `CameraCalibration`.

### C. `CameraCalibration` new fields

Add two optional fields to the `CameraCalibration` dataclass:
```python
calibrated_playfield: Optional[str] = None
calibrated_camera: Optional[str] = None
```

Round-trip in `to_dict` / `from_dict`:
- `to_dict`: include `"calibrated_playfield"` and `"calibrated_camera"` keys when
  not `None`.
- `from_dict`: read them; default to `None` for legacy records.

### D. `load_calibration_from_camera_dir` mismatch detection

Extend the function signature:
```python
def load_calibration_from_camera_dir(
    camera_dir: str | Path,
    camera_config: dict | None = None,
    playfield_def: "PlayfieldDefinition | None" = None,
) -> Optional["CameraCalibration"]:
```

After loading, when both `camera_config` and `playfield_def` are provided:
- Compare `cal.calibrated_playfield` vs `camera_config.get("playfield")`.
- Compare `cal.playfield_width_cm` vs `playfield_def.width_cm` (tolerance: 0.01 cm).
- Compare `cal.playfield_height_cm` vs `playfield_def.height_cm`.
- If any mismatch, OR if `cal.calibrated_playfield is None` (legacy record):
  set `cal.calibration_stale = True`.

`calibration_stale` is a transient Python attribute (not a dataclass field,
not serialized). Set it via `object.__setattr__(cal, "calibration_stale", True)`
or by making `CameraCalibration` not a frozen dataclass (it is already not frozen).

Also log a `logging.warning(...)` when stale.

### E. `save_calibration_to_camera_dir` — preserve `calibrated_playfield` / `calibrated_camera`

These new fields must be in `_CALIBRATION_KEYS` (the set of owned keys that get
overwritten on save). When present on `cal`, they are written. When absent from
`cal` but present in the existing file (user-managed? no — they are owned), they
are dropped. The "owned" list already handles this pattern.

## Acceptance Criteria

- [x] `calibrate_from_playfield_def` raises `PlayfieldConfigError` with the
      exact guidance message when `config.json` is missing.
- [x] `calibrate_from_playfield_def` raises `PlayfieldConfigError` when the
      named playfield is not in the registry.
- [x] Error message from `PlayfieldConfigError` includes the list of available
      playfield names (e.g., `"Available playfields: [main-playfield]"`).
- [x] `CameraCalibration.to_dict()` includes `calibrated_playfield` and
      `calibrated_camera` when they are not `None`.
- [x] `CameraCalibration.from_dict()` restores them; returns `None` for legacy
      records that lack the keys.
- [x] `load_calibration_from_camera_dir` with mismatched provenance sets
      `cal.calibration_stale = True` on the returned object.
- [x] `load_calibration_from_camera_dir` with a legacy record (no
      `calibrated_playfield`) and a known def sets `cal.calibration_stale = True`.
- [x] `load_calibration_from_camera_dir` with matching provenance leaves
      `calibration_stale` falsy (not set or `False`).
- [x] Both `calibrate_from_playfield_def` and the error-path tests run without
      a live camera (use fixture data / mock `detect_all_tags` return values).
- [x] `uv run pytest tests/test_calibration_geometry_persist.py` passes.

## Implementation Plan

### Approach

`calibrate.py` is the focal point. `calibrate_from_playfield_def` is a new
function; existing functions (`calibrate_single`, etc.) are unchanged so legacy
callers continue to work. The two new optional fields on `CameraCalibration` are
backward-compatible. `load_calibration_from_camera_dir` gains optional params
with `None` defaults — existing callers are unaffected.

### Files to modify

- `src/aprilcam/calibration/calibration.py`
  - Add `PlayfieldConfigError(Exception)`.
  - Add `calibrate_from_playfield_def(...)` function.
  - Add `calibrated_playfield: Optional[str] = None` and
    `calibrated_camera: Optional[str] = None` to `CameraCalibration`.
  - Update `to_dict` / `from_dict` for the two new fields.
  - Add `"calibrated_playfield"`, `"calibrated_camera"` to `_CALIBRATION_KEYS`
    in `save_calibration_to_camera_dir`.
  - Extend `load_calibration_from_camera_dir` with optional mismatch detection.

### Testing plan

Extend `tests/test_calibration_geometry_persist.py`:
- `test_provenance_fields_round_trip(tmp_path)` — construct a `CameraCalibration`
  with `calibrated_playfield="main-playfield"`, `calibrated_camera="test-cam"`,
  save it, reload it, assert fields match.
- `test_mismatch_sets_stale(tmp_path)` — save a calibration with
  `calibrated_playfield="old-field"`, then call
  `load_calibration_from_camera_dir(dir, {"playfield": "main-playfield"},
  mock_def)` → assert `cal.calibration_stale is True`.
- `test_legacy_record_sets_stale(tmp_path)` — save a calibration without
  `calibrated_playfield`, reload with a known def → assert stale.
- `test_matching_provenance_not_stale(tmp_path)` — matching slug + dimensions
  → stale not set.

Add `tests/test_calibrate_from_def.py` (new):
- `test_precondition_no_config(tmp_path)` — no `config.json` → raises
  `PlayfieldConfigError` with guidance text.
- `test_precondition_missing_def(tmp_path)` — `config.json` present but def
  name not in registry → raises `PlayfieldConfigError`.
- `test_calibrate_from_def_uses_corner_ids(tmp_path, mock_capture)` — mock
  `detect_all_tags` to return IDs 1/3/5/7 at known pixel positions; assert
  resulting homography maps the expected world coords.

### Documentation updates

None for this ticket. Updated in ticket 007.
