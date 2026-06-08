---
id: '004'
title: Persist reference geometry into the calibration record
status: open
use-cases: [SUC-001, SUC-002]
depends-on: []
github-issue: ''
issue: static-camera-deskew-from-homography.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Persist reference geometry into the calibration record

## Description

Persist the reference geometry that static deskew and fill-in need. This is a
save-side capture of data calibration already computes — no new vision work.

Today `CameraCalibration.to_dict`/`from_dict`
(`src/aprilcam/calibration/calibration.py` ~L125-159) round-trip only
`homography`, `camera_matrix`, `dist_coeffs`, `tags_used`, `rms_error`,
`settings`, `pipeline`. They drop `playfield_width_cm`/`playfield_height_cm`
(the example `global-shutter-camera/calibration.json` has a `playfield` block
that the dataclass does not currently parse) and never stored corner or
static-marker pixels.

This ticket extends the record and its serialization with:

- `playfield: {width, height}` — round-trip `playfield_width_cm` /
  `playfield_height_cm` (read back the existing `playfield` block on load).
  These cm dimensions are the metric deskew target consumed by ticket 005, so
  they must round-trip as real-world centimetres (not pixels).
- `corner_pixels` — the four calibration-time corner pixel positions
  (UL, UR, LR, LL) already available as `pixel_pts` in `calibrate_single`.
- `static_markers` — per-id `{pixel:[u,v], world:[x,y]}` for the static set
  (ArUco corners + AprilTag 1), derived from the per-tag pixels and world
  positions `calibrate_single` already computes.
- `static_marker_ids` — the configurable static set (default
  `aruco_corners + apriltag:1`) recorded with the camera.

`calibrate_single` (and `calibrate_secondary` where it has the data) capture
these into the returned `CameraCalibration`. Old `calibration.json` files
without the new fields must still load (fields default to empty/None).

## Acceptance Criteria

- [ ] `to_dict`/`from_dict` round-trip `playfield` (width/height),
  `corner_pixels`, `static_markers`, and `static_marker_ids`.
- [ ] `calibrate_single` populates `corner_pixels` and `static_markers` (for
  ArUco corners + AprilTag 1) from the data it already computes.
- [ ] A pre-existing `calibration.json` lacking the new fields loads without
  error; the new fields default to empty/None.
- [ ] The existing `playfield` block in
  `global-shutter-camera/calibration.json` is read back into
  `playfield_width_cm`/`playfield_height_cm` on load.
- [ ] The static-marker set defaults to `aruco_corners + apriltag:1` and is
  stored per-camera.
- [ ] `uv run pytest` passes.

## Implementation Plan

- **Approach**: Pure data-model change plus capture at save time. Add fields to
  the dataclass, extend `to_dict`/`from_dict`, and capture in the calibration
  builders.
- **Files to modify**: `src/aprilcam/calibration/calibration.py`.
- **Files to create**: none.
- **Testing plan**: see Testing; use a synthetic homography + corner/tag set.
- **Docs**: document the new calibration fields in the dataclass docstring.

## Testing

- **Existing tests to run**: `uv run pytest tests -k calibration`, then full
  `uv run pytest`.
- **New tests to write**:
  - round-trip: build a `CameraCalibration` with geometry, `to_dict` →
    `from_dict`, assert equality of `playfield`, `corner_pixels`,
    `static_markers`, `static_marker_ids`.
  - backward-compat: load a dict without the new fields → defaults, no error.
  - load the committed `global-shutter-camera/calibration.json` and assert
    `playfield_width_cm`/`height_cm` are populated.
  - Mark OpenCV-dependent tests `needs_cv2`.
- **Verification command**: `uv run pytest`
