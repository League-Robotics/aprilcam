---
id: '003'
title: Calibration engine
status: open
use-cases:
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Calibration engine

## Description

Implements SUC-003 (parent UC-04, UC-15): the `Calibrator`/
`Calibration`/`CalibrationResult` implementations, `calibration.json`
persistence/reload, the `Calibrate`/`GetCalibration` RPCs, and
`aprilcam camera calibrate <camera>`. This is the sprint's geometric
core — every sign and axis here is guilty until proven innocent per
`docs/design/design.md` §6 / `specification.md` §7 (A1-centred origin
via pure subtraction, never a y-flip; heading/yaw `atan2`, 0=east
CCW positive).

**Known field-coverage risk to confront in this ticket, not discover
mid-run**: camera 3 (`arducam-ov9782-usb-camera`) currently sees ArUco
1, 2, 4, 5, 7, 10 but **not** 3, 6, 8, 9 on the physical
`main-playfield`. Calibration is all-or-nothing against
`PlayfieldDefinition.required_aruco_ids()` — `main-playfield`'s
required set is all 10 (perimeter 1–8 + interior 9, 10). As currently
aimed, camera 3 **cannot calibrate**. Before ticket 007 (hardware
verification) can run, this ticket must either (a) confirm the camera
needs repositioning (use ticket 002's `camera view` to re-aim and
re-check coverage) or (b) confirm the required-id set itself needs
revisiting (e.g., is interior marker 9/10 physically mounted and just
out of the current field of view, or genuinely absent from the built
field?). Do not assume either answer — check against the physical
field and record which it was in this ticket's implementation notes.
This does not block this ticket's headless work, but it blocks ticket
007 and must not be left for that ticket to discover cold.

Builds:

- `calibration/calibration.py` (new) — `Calibrator.calibrate(
  camera_slug)`: multi-frame averaged ArUco corner detection (via the
  existing `vision/detector.py` from sprint 021, not reimplemented),
  id matching against the linked `PlayfieldDefinition`'s surveyed
  positions, all-or-nothing acceptance against `required_aruco_ids()`,
  homography solve, degenerate-geometry rejection (near-collinear
  markers, singular homography), `CalibrationResult` construction
  (`matched_ids`, `missing_ids`, `reprojection_error_cm` — computed
  and reported, no threshold enforced, per sprint.md Open Question 1).
  `NoPlayfieldLinked` raised when the camera has no playfield link
  (precondition failure, UC-15.S-2 — distinct from a `CalibrationResult`
  failure).
- `calibration/geometry.py` (new) — pure pixel↔world math backing
  `Calibration.to_world`/`to_pixel` (the deskew warp itself is ticket
  005's concern; this ticket only needs the point-conversion half).
- `daemon/service.py` — `calibrate`/`get_calibration` implemented;
  `calibration.json` written under the camera's state directory on
  success, loaded at daemon startup.
- `errors.py` — `NoPlayfieldLinked(AprilCamError)`, new.
- `cli/calibrate.py` (new) — `aprilcam camera calibrate <camera>`, pure
  RPC orchestrator (no vision work client-side), reports matched/
  missing ids, refuses `--all`.

**Archive pointers**: mine `archive/src/aprilcam/calibration/
calibration.py`'s `calibrate_from_playfield_def`,
`_assign_corners_by_position`, `calibrate_from_corners`, and
`_reprojection_rms` for the id-matching and homography-solve approach
and the hard-won sign conventions — this file is 1805 lines of v1-era
code; take the matching/solving *approach*, not the surrounding
persistence/CLI/robot-specific code around it (rebuilt fresh against
v2's interfaces). Mine `archive/src/aprilcam/calibration/geometry.py`'s
`_world_corners`/`corner_pixels_from_homography` for the
point-conversion math. Do **not** port `archive/src/aprilcam/cli/
calibrate_cli.py` or any `homocal`-shaped upper-left-origin corner
table — `specification.md` §6 explicitly condemns that as a foot-gun.

## Acceptance Criteria

- [ ] **Geometry convention suite (permanent, never relaxed)**: from
      synthetic/fixture ArUco layouts with known east/north
      relationships — a marker surveyed east of another produces a
      **larger** `to_world(...).x`; a marker surveyed north of another
      produces a **larger** `to_world(...).y`; the origin shift is
      verified as pure subtraction, never a sign inversion on either
      axis.
- [ ] All 10 required ArUco ids (perimeter 1–8, interior 9 and 10)
      present in a fixture detection set → `CalibrationResult.
      succeeded=True`, `matched_ids` contains all 10, `missing_ids`
      empty (UC-04.S-1).
- [ ] One id absent from the fixture detection set (e.g., ArUco 6) →
      `succeeded=False`, `missing_ids == (6,)` exactly — never a count,
      never "9 of 10" (UC-04.S-2).
- [ ] A failed calibration attempt does not overwrite or clear an
      existing good `calibration.json` — a prior-good calibration is
      byte-identical after a failed retry.
- [ ] Calling `calibrate` on a camera with no playfield link raises
      `NoPlayfieldLinked` before any frame is captured — assert zero
      calls to the camera backend (UC-15.S-2).
- [ ] Degenerate/near-collinear marker geometry (synthetic fixture) is
      rejected with a `CalibrationResult.reason` naming the problem,
      not silently accepted as a valid-but-bad homography.
- [ ] `aprilcam camera calibrate --all` is refused, explaining
      calibration targets exactly one daemon (UC-15.S-4).
- [ ] `Calibration.calibrated_at`/`playfield` provenance is persisted
      and reloaded identically after a simulated daemon restart (fresh
      `DaemonService` reading the same `calibration.json`).
- [ ] Implementation notes in this ticket record the outcome of the
      camera-3 field-coverage check described above (repositioned
      camera vs. required-set understanding) — a blocking prerequisite
      for ticket 007, not optional documentation.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/vision/
  tests/aprilcam/daemon/test_service.py` — confirm sprint 021's
  uncalibrated detection path is untouched.
- **New tests to write**: `tests/aprilcam/calibration/
  test_calibration.py` (fixture-backed camera replaying baseline
  images from `tests/images/`, plus synthetic ArUco layouts for the
  geometry suite and degenerate-geometry cases); `tests/aprilcam/
  calibration/test_geometry.py` (pure `to_world`/`to_pixel` round-trip:
  `to_pixel(to_world(p)) ≈ p` within ε); `tests/aprilcam/daemon/
  test_service.py` additions for `calibrate`/`get_calibration`
  including the `NoPlayfieldLinked` path and restart-reload.
- **Verification command**: `uv run pytest && uv run mypy` clean; zero
  `typing.Protocol` — `Calibrator`/`Deskewer` derive from their
  declared base classes, `Calibration`/`CalibrationResult` are frozen
  `@dataclass(kw_only=True)` per `calibration/interfaces.py`; baseline
  388 passing, additive only.
