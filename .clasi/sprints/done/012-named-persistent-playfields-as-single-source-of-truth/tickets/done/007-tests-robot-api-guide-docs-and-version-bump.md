---
id: '007'
title: Tests, ROBOT_API_GUIDE docs, and version bump
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
depends-on:
- '001'
- '002'
- '003'
- '004'
- '005'
github-issue: ''
issue: plan-named-persistent-playfields-as-the-single-source-of-truth.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Tests, ROBOT_API_GUIDE docs, and version bump

## Description

Closing ticket. Consolidates test coverage, updates the agent-facing
documentation, and bumps the version. All test files specified in earlier
tickets should be complete before this ticket executes — this ticket adds the
final integration-level tests and verifies the full test suite, then updates
docs.

### Tests to confirm complete (written in earlier tickets)

From ticket 001:
- `tests/test_camera_config.py`

From ticket 002:
- `tests/test_playfield_def.py`

From ticket 004:
- Extensions to `tests/test_calibration_geometry_persist.py`
- `tests/test_calibrate_from_def.py`

From ticket 005:
- `tests/test_open_camera_rehydrate.py` or additions to `tests/test_mcp_path_tools.py`

### New integration-level checks to add in this ticket

- `test_full_sprint_use_case_coverage(tmp_path)` in a new
  `tests/test_sprint_012_integration.py` (or inline in the most appropriate
  existing file):
  - Verify `calibrate_from_playfield_def` and `calibrate_playfield` MCP handler
    both import from the same module (one-liner import trace test).
  - Verify `load_calibration_from_camera_dir` returns `calibration_stale=True`
    for a fixture calibration without provenance fields.
  - Verify `PlayfieldDefinitionRegistry` loads `main-playfield.json` from the
    actual data directory (integration smoke test — reads real file written by
    ticket 003).

- Extend `tests/unit/test_camera_registry.py` if any camera-registry behavior
  changed (unlikely; verify no regression).

### ROBOT_API_GUIDE updates

In `src/aprilcam/ROBOT_API_GUIDE.md`, update or add:

1. **Calibration workflow section**: replace the old
   `calibrate_playfield(width=..., height=...)` example with the new flow:
   - Ensure `config.json` exists for the camera (or use `set_camera_playfield`
     if ticket 006 was executed).
   - Call `calibrate_playfield(playfield_id=...)` — no width/height needed.
   - Dimensions come from the named playfield def automatically.

2. **open_camera auto-rehydration**: add a note that `open_camera` now returns
   `playfield_id` and `playfield_name` when a configured, calibrated camera is
   opened — no `create_playfield` call needed in normal use.

3. **calibration_stale warning**: add a note that `open_camera` may return
   `calibration_stale: true` when the stored calibration predates the current
   playfield definition. In that case, call `calibrate_playfield` to re-calibrate.

4. **Coordinate system note**: the world coordinate origin is the center of the
   playfield (AprilTag A1 = 0,0; X east, Y north). Distance unit is cm.

### Version bump

Run `dotconfig version bump` (or equivalent) to advance `pyproject.toml` version
per the `0.YYYYMMDD.N` scheme. Commit with `chore: bump version`.

## Acceptance Criteria

- [x] `uv run pytest` passes with zero failures.
- [x] No `xfail` markers masking failures from this sprint's work.
- [x] `ROBOT_API_GUIDE.md` calibration workflow example uses the new
      `calibrate_playfield` call without `width`/`height`.
- [x] `ROBOT_API_GUIDE.md` documents `open_camera` auto-rehydration behavior.
- [x] `ROBOT_API_GUIDE.md` documents `calibration_stale` warning.
- [x] `pyproject.toml` version is bumped from the pre-sprint value.
- [x] Integration smoke test confirms `main-playfield.json` loads correctly from
      the actual data directory.

## Implementation Plan

### Files to modify

- `src/aprilcam/ROBOT_API_GUIDE.md` — calibration workflow, auto-rehydration,
  stale warning, coordinate system.
- `pyproject.toml` — version bump.
- Any test gaps identified during final `uv run pytest` run.

### Testing plan

- Run `uv run pytest` with `--tb=short`; fix any remaining failures.
- Run `uv run pytest tests/test_playfield_def.py tests/test_camera_config.py
  tests/test_calibration_geometry_persist.py tests/test_mcp_path_tools.py -v`
  for targeted sprint-012 test output.

### Documentation updates

`ROBOT_API_GUIDE.md` — see above. No other documentation files require changes.
