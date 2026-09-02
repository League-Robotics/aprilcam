---
id: "002"
title: "Daemon interface, DaemonService orchestration, and flat-field persistence"
status: open
use-cases: [SUC-001, SUC-002, SUC-003]
depends-on: ["001"]
github-issue: ""
issue: flat-field-gain-map-capture-as-a-daemon-workflow.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon interface, DaemonService orchestration, and flat-field persistence

## Description

Wire ticket 001's `FlatFieldCapturer` into the daemon: add
`capture_flat_field`/`get_flatfield`/`clear_flat_field` to
`daemon/interfaces.py`'s `Daemon` (plain base class, `NotImplementedError`
bodies) and implement them in `daemon/service.py`'s `DaemonService`, the
same thin-orchestrator split `calibrate`/`Calibrator` already established
(this file resolves the camera/mask/exposure and persists the result;
`calibration/flatfield.py` does the math). See this sprint's
`design/daemon-DESIGN.md` (the flat-field Constraints bullets, the
"Flat-field capture, component view" diagram, and the Design Rationale
entry) for the full contract.

Concretely: derive the field-interior mask from the camera's stored
`Calibration` when present (the playfield's four world corners —
`+-width_cm/2`, `+-height_cm/2`, reusing `deskew.py`'s `_world_corners`
via a small shared helper — converted through `Calibration.to_pixel`,
filled as a polygon), else from the linked playfield's required ArUco
corner ids detected directly in one frame via `vision.detector.TagDetector`
(convex hull of the detected corner points), else raise `NoFieldMask`
before any capture burst is taken. Push a temporary exposure override
(when `--exposure`/`exposure=` is given) before the K-frame capture and
restore the camera's prior stored/live configuration in a `finally` —
unconditionally, on success, a quality-gate refusal, or an exception.
Call `FlatFieldCapturer.build`, and on success (or `force=True`) persist
`flatfield.npy` (unchanged shape/location — the apply side already reads
it) plus a new `flatfield.json` metadata sidecar (`captured_at`, `shape`,
`gain_min`/`gain_max`, `stale`) atomically, next to `calibration.json`
(tmp+fsync+`os.replace`, mirroring `_persist_calibration`), then reload
the live map on the held-open `Camera` via its existing `reset()`/
`_load_flatfield` mechanism (already shipped, out-of-process — no change
needed there). `dry_run=True` evaluates and reports without writing or
reloading. Add an eighth in-memory store, `_flatfields: Dict[str,
FlatFieldInfo]`, reloaded at construction from every
`state_dir/cameras/*/flatfield.json` (`_load_flatfields`, tolerating a
missing/corrupt sidecar exactly like `_load_calibrations` tolerates a
missing/corrupt `calibration.json` — and a pre-existing `flatfield.npy`
with no sidecar, from the manual recipe, reports `present=True`,
`captured_at=None`, never a fabricated timestamp). Mark a camera's
flat-field state stale (`_mark_flatfield_stale`, a sibling of the
existing `_mark_stale`) on the same two events that already mark a
calibration stale: a successful self-heal/explicit reopen, and a
resolution-changing `set_camera_config` push. `clear_flat_field` deletes
both files and drops the live-applied map on the held-open camera, no
restart. Finally, add `flatfield_present: bool = False`/`flatfield_stale:
bool = False` to `types.CameraState` and overlay real values in
`list_cameras`, the same way `calibrated`/`calibration_stale` already are.

## Acceptance Criteria

- [ ] `daemon/interfaces.py`'s `Daemon` gains `capture_flat_field(camera,
      *, exposure=None, frames=None, force=False, dry_run=False) ->
      FlatFieldResult`, `get_flatfield(camera) -> Optional[FlatFieldInfo]`,
      `clear_flat_field(camera) -> None`.
- [ ] `DaemonService` derives the field mask from a stored `Calibration`
      when present, else from detected ArUco corners, else raises
      `NoFieldMask` before any capture.
- [ ] The camera's prior configuration is restored after capture on
      every exit path — success, quality-gate refusal, and an exception
      raised mid-capture.
- [ ] A successful (or forced) capture writes `flatfield.npy` +
      `flatfield.json` atomically and reloads the live map with no
      daemon restart, verified via a subsequent `get_frame`/read on the
      same held-open camera.
- [ ] `dry_run=True` writes nothing and does not change the live-applied
      map.
- [ ] A refused (non-forced) capture makes no filesystem write and does
      not touch the live-applied map.
- [ ] `get_flatfield` answers from `_flatfields`, reloaded at
      construction (`_load_flatfields`); a pre-existing `flatfield.npy`
      with no `.json` sidecar reports `present=True`, `captured_at=None`.
- [ ] `clear_flat_field` deletes both files and drops the live map, no
      restart.
- [ ] A camera reopen (self-heal or explicit) and a resolution-changing
      `set_camera_config` push both mark that camera's flat-field state
      stale.
- [ ] `types.CameraState` gains `flatfield_present`/`flatfield_stale`;
      `DaemonService.list_cameras` overlays real values for both, for a
      held-open and a not-held-open camera alike.

## Testing

- **Existing tests to run**: `tests/aprilcam/daemon/test_service.py`
  (no regressions, especially existing calibration/staleness tests).
- **New tests to write**: unit tests against `DaemonService` directly
  (not RPC — that is ticket 003) using this suite's existing fake
  camera/backend fixtures: mask derivation (calibrated path, ArUco
  fallback, `NoFieldMask` refusal with neither), exposure push/restore
  across success/refusal/exception, `flatfield.npy`+`.json` persistence
  round-trip (written, then reloaded correctly by a freshly constructed
  `DaemonService` against the same `state_dir`), staleness on reopen and
  on a resolution-changing config push, and `clear_flat_field`.
- **Verification command**: `uv run pytest tests/aprilcam/daemon/
  tests/aprilcam/calibration/`
