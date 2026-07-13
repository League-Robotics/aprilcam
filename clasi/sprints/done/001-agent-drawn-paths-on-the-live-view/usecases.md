---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 001 Use Cases

## SUC-001: Submit a Waypoint Path to a Playfield

- **Actor**: AI agent (via MCP)
- **Preconditions**: A `playfield_id` exists in the playfield registry
  (playfield need not be calibrated yet).
- **Main Flow**:
  1. Agent calls `create_path(playfield_id, waypoints_json)` where
     `waypoints_json` is a JSON array of waypoint objects, each
     containing `x`, `y`, `size_cm`, `symbol`, `symbol_color`, and
     `line_color`.
  2. Server validates the playfield_id, parses the JSON, validates each
     waypoint field (type, range, symbol membership).
  3. Server creates a `Path` in `PathRegistry` with a monotonic
     `path_NNN` id.
  4. If a live view is running for that playfield's camera, server
     sends an `{"op": "add", "path": {...}}` command on the command pipe.
  5. Server returns `{"path_id": "path_000"}`.
- **Postconditions**: Path exists in registry; live view renders it on
  the next frame if running.
- **Acceptance Criteria**:
  - [ ] Valid input returns `{"path_id": "path_NNN"}`.
  - [ ] Unknown `playfield_id` returns `{"error": "Unknown playfield_id '...'"}`.
  - [ ] Empty waypoint list returns an error.
  - [ ] Invalid `symbol` value returns `{"error": "Invalid symbol '...'"}`.
  - [ ] `size_cm <= 0` returns `{"error": "size_cm must be positive"}`.
  - [ ] Non-parseable `waypoints_json` returns a JSON error message.
  - [ ] Colors with values outside `[0, 255]` or wrong length return an error.
  - [ ] Path IDs are monotonically increasing across calls.

## SUC-002: Delete a Path by ID

- **Actor**: AI agent (via MCP)
- **Preconditions**: A `path_id` may or may not exist.
- **Main Flow**:
  1. Agent calls `delete_path(path_id)`.
  2. Server looks up `path_id` in `PathRegistry`.
  3. If found, removes it; sends `{"op": "remove", "path_id": "..."}` on
     the command pipe to any running live view for that path's playfield.
  4. Returns `{"deleted": true, "path_id": "path_000"}`.
- **Postconditions**: Path is gone from registry; live view stops
  rendering it on the next frame.
- **Acceptance Criteria**:
  - [ ] Known `path_id` returns `{"deleted": true, "path_id": "..."}`.
  - [ ] Unknown `path_id` returns `{"error": "Unknown path_id '...'"}`.
  - [ ] Deleted path disappears from `list_paths` response.

## SUC-003: List Paths for a Playfield

- **Actor**: AI agent (via MCP)
- **Preconditions**: None (playfield may have zero paths).
- **Main Flow**:
  1. Agent calls `list_paths(playfield_id)`.
  2. Server returns all paths in `PathRegistry` for that playfield,
     including each path's full waypoint data.
- **Postconditions**: None (read-only).
- **Acceptance Criteria**:
  - [ ] Returns `{"playfield_id": "...", "paths": [...]}`.
  - [ ] Returns an empty `paths` list if none exist.
  - [ ] Each returned path includes `path_id`, `playfield_id`, and
        full `waypoints` array.
  - [ ] Unknown `playfield_id` returns an error.

## SUC-004: Clear All Paths for a Playfield

- **Actor**: AI agent (via MCP)
- **Preconditions**: None (zero paths is valid).
- **Main Flow**:
  1. Agent calls `clear_paths(playfield_id)`.
  2. Server removes all paths for that playfield from the registry.
  3. Sends `{"op": "clear"}` on the command pipe to any running live
     view for that playfield.
  4. Returns `{"cleared": ["path_000", "path_001", ...]}`.
- **Postconditions**: No paths remain for that playfield; live view
  shows no agent-drawn overlays.
- **Acceptance Criteria**:
  - [ ] Returns `{"cleared": [...]}` listing IDs that were removed.
  - [ ] Returns `{"cleared": []}` if no paths existed.
  - [ ] Subsequent `list_paths` returns an empty list.
  - [ ] Live view shows no paths after the next frame.

## SUC-005: Paths Render on the Live View in Real Time

- **Actor**: Live-view child process (rendering subsystem)
- **Preconditions**: Live view is running; at least one path exists for
  the playfield; playfield is calibrated (homography available).
- **Main Flow**:
  1. Child receives an `add` command via the command pipe.
  2. Child updates its local `paths` dict.
  3. On the next frame, `draw_paths` is called after `draw_overlays`.
  4. For each path, for each waypoint: world coords are mapped to source
     pixels via inverse homography, then to display pixels via
     `_map_points_to_display`.
  5. Lines are drawn first (segment from each waypoint to the next),
     then symbols (using per-waypoint `size_cm` scaled to pixels).
  6. RGB colors are converted to BGR only at the `cv.*` call site.
- **Postconditions**: Frame shows labeled waypoints connected by lines.
- **Acceptance Criteria**:
  - [ ] All 8 symbol types render without error.
  - [ ] `none` symbol draws no marker but lines still connect through it.
  - [ ] Symbol pixel size scales correctly with `size_cm`; a 10cm symbol
        appears roughly twice as wide as a 5cm one.
  - [ ] RGB colors appear correct (red `[255,0,0]` looks red, not blue).
  - [ ] Lines are drawn before symbols (symbols occlude line endpoints).
  - [ ] Paths created mid-run appear within one frame.
  - [ ] Uncalibrated playfield: `draw_paths` is a no-op; no crash.

## SUC-006: Paths Survive a Live-View Restart

- **Actor**: AI agent (via MCP)
- **Preconditions**: One or more paths exist in the registry; live view
  is stopped or not yet started.
- **Main Flow**:
  1. Agent (or system) stops the live view.
  2. Paths remain in `PathRegistry` (parent-process memory).
  3. Agent starts the live view again.
  4. Before `proc.start()`, `LiveViewProcess.set_initial_paths()` is
     called with all paths for that playfield serialized to dicts.
  5. Child reads the initial-paths JSON at startup and seeds its local
     dict before the first frame.
  6. First frame already shows all pre-existing paths.
- **Postconditions**: All pre-existing paths are visible immediately on
  the first frame of the restarted view.
- **Acceptance Criteria**:
  - [ ] After stop + restart, all paths from `list_paths` appear on
        the first frame of the new live view window.
  - [ ] No `add` command is needed after restart; the initial-state push
        is sufficient.
  - [ ] `list_paths` returns the same paths before and after restart.
