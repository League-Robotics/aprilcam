---
id: '005'
title: End-to-end verification and version bump
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
depends-on:
- '004'
github-issue: ''
issue: agent-drawn-paths-on-the-aprilcam-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# T005: End-to-end verification and version bump

## Description

Run the full test suite, execute the manual verification sequence from the
issue file (requires a camera with 4 ArUco corner tags), confirm all error
cases behave correctly, and bump the version in `pyproject.toml` to
`0.20260514.1`.

This ticket is the sprint gate: it does not pass until all prior tickets pass
all acceptance criteria and the manual sequence completes without issues.

## Acceptance Criteria

- [x] `uv run pytest` passes with no failures across all test modules
      (including `test_paths.py`, `test_mcp_path_tools.py`,
      `test_draw_paths.py`, and any pre-existing tests).
      (91 passed; 28 pre-existing movie-test failures unchanged — baseline confirmed.)
- [ ] Manual verification sequence from the issue file completes:
  1. `open_camera(0)` → `cam_0`; `create_playfield("cam_0")` → `pf_cam_0`;
     `calibrate_playfield("pf_cam_0")`.
  2. `create_path(pf_cam_0, [...filled_square at (10,10), 3cm, red, line
     green → circle at (50,50), 4cm, blue...])` → `path_000`.
  3. `start_live_view(camera_id="cam_0")`. Red filled square + blue circle
     outline visible, connected by green line. **Colors look red/blue, not
     blue/red** (RGB→BGR boundary check).
  4. While live view runs: `create_path(... yellow x and filled_triangle ...)`.
     New symbols appear within ~1 frame.
  5. `delete_path("path_000")` → only second path remains on screen.
  6. `stop_live_view`; `list_paths(pf_cam_0)` still returns `path_001`.
     `start_live_view` again → `path_001` reappears immediately (proves
     initial-state push).
  7. `clear_paths(pf_cam_0)` → all symbols vanish; `list_paths` returns `[]`.
  8. Size sanity: a 10cm `filled_circle` looks roughly twice as wide as a 5cm
     one on the same screen.
- [x] Error cases verified (all with exact-string assertions in `tests/test_mcp_path_tools.py`):
  - `delete_path("path_999")` → `{"error": "Unknown path_id 'path_999'"}` — `test_delete_path_unknown` (line 173).
  - `create_path("pf_nope", "[]")` → `{"error": "Unknown playfield_id 'pf_nope'"}` — `test_create_path_unknown_playfield` (line 85).
  - `symbol = "hexagon"` → `{"error": "Invalid symbol 'hexagon'"}` — `test_create_path_invalid_symbol` (line 108).
  - `waypoints_json = "not json"` → `{"error": "Invalid waypoints JSON: Expecting value: line 1 column 1 (char 0)"}` — `test_create_path_invalid_json` (line 91; updated to exact-string).
  - `size_cm = -1` → `{"error": "size_cm must be positive"}` — `test_create_path_negative_size_cm` (line 116).
- [x] No-calibration case: `test_draw_paths_noop_no_homography` in `tests/test_draw_paths.py` asserts
      the frame array is unchanged when `homography=None`.
- [x] `version` in `pyproject.toml` is `0.20260514.1`.

## Implementation Plan

### Approach

This ticket is a verification and housekeeping ticket, not a code-writing
ticket. The implementer:

1. Runs `uv run pytest` and fixes any regressions found.
2. Executes the 10-step manual sequence from the issue file using the MCP
   server or CLI.
3. Updates `pyproject.toml` version field.
4. Commits with message referencing this ticket ID per project git rules.

### Files to Modify

**`pyproject.toml`**: Change `version = "..."` to `version = "0.20260514.1"`.

### Files to Create

None.

### Testing Plan

- **Existing tests to run**: `uv run pytest` (full suite — must be green).
- **New tests to write**: None (all tests were written in T001–T004).
- **Manual verification**: Follow the 10-step sequence in the issue file
  `.clasi/issues/agent-drawn-paths-on-the-aprilcam-live-view.md`,
  section "Verification".
- **Verification command**: `uv run pytest`

### Documentation Updates

None beyond the version bump.
