---
sprint: '012'
---
<!-- CLASI: Sprint use cases for Sprint 012 -->

# Sprint 012 Use Cases

## SUC-001: Startup auto-rehydration of playfield context

- **Actor**: MCP server (at camera open time)
- **Preconditions**:
  - `data/aprilcam/playfields/<slug>.json` exists and is well-formed.
  - `data/aprilcam/cameras/<slug>/config.json` contains `{"playfield": "<slug>"}`.
  - `data/aprilcam/cameras/<slug>/calibration.json` contains a valid homography
    and provenance fields matching the current config.
- **Main Flow**:
  1. `open_camera` opens the daemon camera and resolves the camera directory.
  2. Server reads `config.json` → playfield slug.
  3. Server loads `PlayfieldDefinition` from the registry.
  4. Server loads `calibration.json` and checks provenance against the def.
  5. Server reconstructs a `PlayfieldEntry` with the stored homography and
     def-derived field spec; registers it in `playfield_registry`.
  6. Returns `camera_id`, `playfield_id`, and `playfield_name` in the response.
- **Postconditions**:
  - Agent can immediately call `get_tags`, `pixel_to_world`, etc. without
    calling `create_playfield`.
- **Acceptance Criteria**:
  - [ ] `open_camera` on a configured, calibrated camera returns a `playfield_id`
        in its response without any `create_playfield` call.
  - [ ] `pixel_to_world` on a rehydrated `PlayfieldEntry` returns world coords in
        the center-origin frame.
  - [ ] Provenance mismatch surfaces `"calibration_stale": true` in the response
        rather than crashing.

---

## SUC-002: Calibration derives all geometry from the playfield definition

- **Actor**: Developer / agent calibrating a camera
- **Preconditions**:
  - Camera `config.json` exists with `{"playfield": "<slug>"}`.
  - `data/aprilcam/playfields/<slug>.json` exists.
  - Camera is open; enough tags are visible to detect the 4 diagonal-cardinal
    ArUco corners specified by the def.
- **Main Flow**:
  1. Shared helper resolves `config.json` → playfield slug → `PlayfieldDefinition`.
  2. Helper reads `width_cm`, `height_cm`, and corner ArUco IDs + world positions
     from the def (center origin; A1 = 0,0).
  3. Helper detects those specific ArUco IDs in the live frame; computes homography.
  4. Saves `calibration.json` with derived dimension snapshot and provenance fields
     (`calibrated_playfield`, `calibrated_camera`).
  5. Returns success with `width_cm` and `height_cm` from the def.
- **Postconditions**:
  - `pixel_to_world` of AprilTag A1 pixel ≈ (0, 0).
  - World coordinate system matches the def's center-origin convention.
- **Acceptance Criteria**:
  - [ ] Calibration no longer accepts user-supplied width/height; dimensions come
        from the def only.
  - [ ] Both `calibrate_playfield` (MCP) and `aprilcam calibrate` (CLI) call the
        same shared helper — single code path verified by tests.
  - [ ] Written `calibration.json` contains `calibrated_playfield` and
        `calibrated_camera` provenance fields.
  - [ ] Corner ArUco IDs used are those specified by the def (1/3/5/7 for
        main-playfield), not the old hardcoded 0-3.

---

## SUC-003: Hard error when calibrating without a playfield configured

- **Actor**: Developer / agent attempting calibration on an unconfigured camera
- **Preconditions**:
  - Camera exists in the daemon registry.
  - Camera directory has no `config.json`, OR `config.json` references a
    playfield slug not present in the registry.
- **Main Flow**:
  1. Shared helper attempts to resolve `config.json` → playfield slug.
  2. No `config.json`: raises a hard error with exact instructions:
     `"Camera '<id>' has no playfield configured. Create
     data/aprilcam/cameras/<slug>/config.json with {\"playfield\": \"<name>\"}.
     Available playfields: [...]"`.
  3. `config.json` present but def missing: raises a hard error listing available
     playfields.
  4. Both MCP tool and CLI surface this error identically.
- **Postconditions**:
  - No partial calibration written. User receives actionable instructions.
- **Acceptance Criteria**:
  - [ ] `calibrate_playfield` (MCP) on unconfigured camera returns the guidance
        error, not a silent failure.
  - [ ] `aprilcam calibrate` (CLI) on unconfigured camera prints the guidance
        error and exits non-zero.
  - [ ] Error message from both entry points is produced by the same helper
        function (verified by unit test).

---

## SUC-004: Provenance mismatch detection on calibration load

- **Actor**: MCP server (background, during open_camera rehydrate path)
- **Preconditions**:
  - `calibration.json` contains `calibrated_playfield` provenance field.
  - Either the camera's `config.json` references a different playfield slug, or
    the def's dimensions differ from the snapshot stored in `calibration.json`.
- **Main Flow**:
  1. Loader reads `calibration.json`; extracts `calibrated_playfield` and
     `calibrated_camera` provenance fields.
  2. Loads current `config.json` → expected playfield slug.
  3. If slugs differ, or def dimensions differ from stored snapshot: sets
     `calibration_stale = True` on the returned object and logs a warning.
  4. Rehydrate path surfaces `"calibration_stale": true` in the `open_camera`
     response.
- **Postconditions**:
  - No silent use of stale geometry. Agent knows to recalibrate.
- **Acceptance Criteria**:
  - [ ] `load_calibration_from_camera_dir` returns a `calibration_stale` flag
        when provenance does not match.
  - [ ] `open_camera` response includes `"calibration_stale": true` when a
        mismatch is detected.
  - [ ] The mismatch detection is testable without live camera hardware (using
        fixture files).

---

## SUC-005: One-time data migration to named playfields structure

- **Actor**: Ticket executor (developer)
- **Preconditions**:
  - `data/aprilcam/playfield.json` exists at the legacy location.
  - `data/aprilcam/playfields/` directory does not yet exist.
- **Main Flow**:
  1. Move `playfield.json` → `playfields/main-playfield.json`; add `name` and
     `display_name` identity fields.
  2. Write `config.json` = `{"playfield": "main-playfield"}` into the three
     calibrated camera directories (arducam-ov9782-usb-camera, hd-usb-camera,
     global-shutter-camera).
  3. Clear stale `paths.json` entries (write `[]`) for those three cameras.
  4. Existing `calibration.json` homographies are NOT rewritten — they remain
     stale and will be flagged by mismatch detection on the next `open_camera`.
- **Postconditions**:
  - Repository data directory reflects the new layout.
  - Old `playfield.json` path is absent.
  - Three cameras are linked to `main-playfield`.
- **Acceptance Criteria**:
  - [ ] `data/aprilcam/playfields/main-playfield.json` exists with `name` and
        `display_name` fields.
  - [ ] `data/aprilcam/cameras/arducam-ov9782-usb-camera/config.json` (and the
        other two) contain `{"playfield": "main-playfield"}`.
  - [ ] `PlayfieldDefinitionRegistry.load_all(playfields_dir)` returns exactly
        one entry named `main-playfield`.
  - [ ] `open_camera` on any of the three cameras fires the stale calibration
        warning (because stored homographies predate provenance tracking).
