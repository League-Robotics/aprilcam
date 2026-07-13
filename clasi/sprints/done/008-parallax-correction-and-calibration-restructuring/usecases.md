---
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 008 Use Cases

## SUC-001: Store and load structured calibration data

- **Actor**: Robotics developer / calibration tooling
- **Preconditions**: A `calibration.json` file exists at
  `<cameras_dir>/<cam_name>/calibration.json`, either in the old format
  (top-level `field_width_cm` / `field_height_cm`) or the new format
  (`playfield.width` / `playfield.height`, `camera_position`,
  `tag_heights`).
- **Main Flow**:
  1. Caller invokes `load_calibration_from_camera_dir(camera_dir)`.
  2. System reads the JSON file and populates a `CameraCalibration` object
     including `playfield_width_cm`, `playfield_height_cm`,
     `camera_position`, and `tag_heights` from whichever format is present.
  3. Caller invokes `save_calibration_to_camera_dir(cal, camera_dir, ...)`.
  4. System writes the new-format JSON with `playfield: {width, height}`,
     `camera_position`, and `tag_heights` sub-dicts.
- **Postconditions**: The `CameraCalibration` object faithfully represents
  the physical setup. Old-format files load without error.
- **Acceptance Criteria**:
  - [ ] New-format file: `playfield_width_cm` and `playfield_height_cm`
        are populated on load.
  - [ ] Old-format file (top-level `field_width_cm`): loads with the same
        values in `playfield_width_cm` / `playfield_height_cm`.
  - [ ] `camera_position` is `None` when absent from JSON.
  - [ ] `tag_heights` is an empty dict when absent from JSON.
  - [ ] Save produces `playfield: {width, height}` sub-dict; old top-level
        keys are not written.
  - [ ] Round-trip: save then load produces equal values.

---

## SUC-002: Correct world coordinates for tag height above playfield

- **Actor**: AI agent / detection pipeline
- **Preconditions**: A `CameraCalibration` with a `camera_position` whose
  `height > 0` is loaded. A tag's raw homography result `(wx0, wy0)` is
  available. The tag's height above the playfield `h` is known.
- **Main Flow**:
  1. Caller invokes
     `calibration.correct_world_for_height(wx0, wy0, h)`.
  2. System computes `r = h / H` where `H` is `camera_position.height`.
  3. System returns
     `(wx0 + r * (cx - wx0), wy0 + r * (cy - wy0))` where `cx`, `cy`
     are the camera's horizontal offsets.
- **Postconditions**: Returned coordinates represent the tag's true
  playfield position, corrected for vertical displacement.
- **Acceptance Criteria**:
  - [ ] When `h = 0`, returns `(wx0, wy0)` unchanged.
  - [ ] When `camera_position` is `None`, returns `(wx0, wy0)` unchanged.
  - [ ] When `camera_position.height = 0`, returns `(wx0, wy0)` unchanged.
  - [ ] For a camera at `(0, 0, 180)` and tag at `(50, 50)` with `h = 12`,
        returns `(50 + (12/180)*(0-50), 50 + (12/180)*(0-50))` =
        approximately `(46.67, 46.67)`.

---

## SUC-003: Apply parallax correction in daemon pipeline and MCP tools

- **Actor**: AI agent using the MCP `get_tags` tool
- **Preconditions**: Detection loop is running on a calibrated playfield.
  `calibration.json` has `camera_position` with `height > 0` and
  `tag_heights` with at least one entry.
- **Main Flow (daemon, automatic)**:
  1. Detection loop produces raw `world_xy` for each tag via homography.
  2. Pipeline checks `calibration.tag_heights` for the tag's ID.
  3. If height > 0, pipeline calls `correct_world_for_height` and
     replaces `tag.world_xy` with the corrected value.
  4. Agent calls `get_tags`; corrected coordinates are returned.
- **Main Flow (per-call override)**:
  1. Agent calls `get_tags(source_id, tag_heights_json='{"5": 11.8}')`.
  2. MCP server merges the JSON dict over `calibration.tag_heights`.
  3. Correction is applied per tag using merged heights.
  4. Corrected `world_xy` values are returned for that call only.
- **Postconditions**: Tags with known heights return corrected world
  coordinates; tags without a height entry are unaffected.
- **Acceptance Criteria**:
  - [ ] Daemon pipeline applies correction for tag IDs in
        `calibration.tag_heights` when `camera_position.height > 0`.
  - [ ] Tags not in `tag_heights` are not corrected.
  - [ ] `get_tags` `tag_heights_json` parameter overrides persisted values
        for the duration of that call only.
  - [ ] `get_tags(tag_heights_json='{"5": 0}')` returns uncorrected
        world_xy for tag 5 (overrides any persisted height with 0).
  - [ ] `calibrate_playfield` accepts `camera_height_cm`,
        `camera_x_offset_cm`, `camera_y_offset_cm` and writes them to
        `calibration.json` as `camera_position`.
