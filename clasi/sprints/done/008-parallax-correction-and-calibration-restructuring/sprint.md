---
id: 008
title: Parallax correction and calibration restructuring
status: done
branch: sprint/008-parallax-correction-and-calibration-restructuring
use-cases:
- SUC-001
- SUC-002
- SUC-003
issues:
- parallax-correction-and-calibration-restructuring.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 008: Parallax correction and calibration restructuring

## Goals

1. Restructure `calibration.json` to use a `playfield` sub-dict for field
   dimensions and add `camera_position` and `tag_heights` sub-dicts.
2. Add a `correct_world_for_height()` method to `CameraCalibration` that
   implements the parallax correction math.
3. Apply the correction automatically in the daemon detection pipeline
   and expose it as an optional per-call override in `get_tags`.
4. Extend `calibrate_playfield` with camera position parameters so agents
   can record the physical camera setup in one call.

## Problem

Robot tags are mounted ~118 mm above the playfield surface. Because the
camera homography assumes all tags lie flat at z=0, elevated tags produce
world coordinates that are displaced toward the camera's nadir. The
displacement grows with tag height and with distance of the tag from
the camera's nadir point. For a 12 cm tag height on a typical overhead
camera at ~180 cm, the error can exceed 7 mm — significant for precise
robot positioning.

## Solution

### Phase 1 — Calibration struct and JSON format
Add `CameraPosition` dataclass. Add `camera_position`, `tag_heights`,
`playfield_width_cm`, and `playfield_height_cm` fields to
`CameraCalibration`. Update load/save functions to read the new JSON
sub-dict layout (`playfield.width`, `playfield.height`) with backward-
compatible fallback to old top-level keys. Add
`correct_world_for_height(wx, wy, h)` method.

### Phase 2 — Correction application
In the daemon pipeline, apply height correction per tag after homography
projection using the calibration's `tag_heights` map. In `get_tags`, add
`tag_heights_json` parameter for per-call height override. In
`calibrate_playfield`, add `camera_height_cm`, `camera_x_offset_cm`, and
`camera_y_offset_cm` parameters so the camera position is persisted on
calibration.

## Success Criteria

- Old `calibration.json` files (with `field_width_cm` / `field_height_cm`
  at the top level) still load correctly.
- New files write `playfield: {width, height}` format.
- `correct_world_for_height` returns identity when `camera_position` is
  absent or height is 0.
- Daemon pipeline corrects world coordinates automatically for tags whose
  IDs appear in `calibration.tag_heights`.
- `get_tags(source_id, tag_heights_json='{"5": 11.8}')` applies the
  correction at call time, overriding persisted values for that call only.
- `calibrate_playfield` accepts camera height/offset parameters and saves
  them in `calibration.json`.
- All existing tests pass.

## Scope

### In Scope

- `calibration.py`: `CameraPosition` dataclass, `CameraCalibration` new
  fields, `correct_world_for_height` method, updated load/save functions
- `camera_pipeline.py`: per-tag parallax correction in detection loop
- `mcp_server.py`: `calibrate_playfield` new params, `get_tags` override

### Out of Scope

- UI / display layer changes — `display.py` does not use
  `field_width_cm`/`field_height_cm` as attributes on `CameraCalibration`
  (confirmed by grep: no `.field_width_cm` attribute access exists)
- Streaming tools (`stream_tags`, `start_detection`): daemon applies
  correction automatically; no new parameters needed
- Multi-robot height tables beyond tag_heights dict
- Any calibration workflow changes beyond the three new parameters to
  `calibrate_playfield`

## Test Strategy

- Unit tests for `correct_world_for_height`: identity when height is 0,
  identity when no camera_position, correct formula at known values
- Load/save round-trip test: write new format, reload, check fields match
- Backward-compat load test: read old-format file, verify
  `playfield_width_cm` / `playfield_height_cm` populated from legacy keys
- Integration: manual verification per the issue file's Verification section

## Architecture Notes

The correction is a simple linear interpolation along the viewing ray.
It lives entirely in `CameraCalibration`, keeping the geometry isolated
from transport and pipeline logic. The daemon pipeline and MCP server
only call the method — they hold no geometry knowledge.

`playfield_width_cm` and `playfield_height_cm` are new attributes on
`CameraCalibration` (they do not exist today — the old JSON keys were
not deserialized into the dataclass). No existing code reads these as
object attributes, so no rename is required in pipeline or MCP code.

## GitHub Issues

None.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Calibration struct changes and JSON format | — |
| 002 | Parallax correction application | 001 |

Tickets execute serially in the order listed.
