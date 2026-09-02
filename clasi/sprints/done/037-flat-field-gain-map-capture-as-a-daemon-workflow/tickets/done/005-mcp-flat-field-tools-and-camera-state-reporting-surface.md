---
id: '005'
title: MCP flat-field tools and camera-state reporting surface
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '004'
github-issue: ''
issue: flat-field-gain-map-capture-as-a-daemon-workflow.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# MCP flat-field tools and camera-state reporting surface

## Description

Register the agent-facing MCP tools and finish surfacing flat-field
state everywhere calibration state already appears — the last two
pieces of SUC-001/002/003's user-visible surface. In
`mcp/tools/configuration.py`, add `capture_flatfield`/`get_flatfield`/
`clear_flatfield`, following the identical `register(mcp, connection,
temp_dir)` + `@mcp.tool()` + `@handle_tool_errors` pattern every tool in
this module already uses, with `exposure`/`frames`/`force`/`dry_run`
optional parameters mirroring `calibrate`'s `estimate_intrinsics` shape.
Return `calibration.interfaces.FlatFieldResult`/`FlatFieldInfo` — the
cv2-free interface types — never anything from `calibration.flatfield`
(which imports `cv2` at module scope, same reason `calibrate`/
`get_calibration` already avoid `calibration.calibration`).

Ticket 002 added `CameraState.flatfield_present`/`flatfield_stale`;
this ticket wires their *display* everywhere `calibrated`/
`calibration_stale` already appear: `cli/_common.py`'s
`format_camera_row`/`print_cameras`/`print_probe_report` (so `camera
list`/`camera probe` show flat-field presence/staleness alongside
calibration), and any MCP `list_cameras`/`get_camera_config` tool output
shape that currently surfaces the calibration fields.

## Acceptance Criteria

- [x] `mcp/tools/configuration.py` registers `capture_flatfield`,
      `get_flatfield`, `clear_flatfield`, matching this module's
      existing tool-registration shape exactly.
- [x] The three tools return the cv2-free `calibration.interfaces`
      types (or this module's existing `.to_dict()`-equivalent
      convention for a type whose wire shape has diverged), never the
      concrete `calibration.flatfield` module.
      (`capture_flatfield` uses the explicit-dict convention:
      `FlatFieldResult.gain` is `Optional[numpy.ndarray]`, and FastMCP's
      pydantic-based schema introspection cannot build a schema for a
      bare `numpy.ndarray` field -- confirmed directly, it broke
      `build_server()` for every tool, not just this one, until
      `capture_flatfield` was changed to return a plain dict built by
      `_flatfield_result_to_dict`, omitting `gain` -- which was never to
      be exposed here anyway. `get_flatfield` has no such field and
      returns `FlatFieldInfo` verbatim.)
- [x] `tests/test_no_cv2_in_client_modules.py` continues to pass after
      this ticket.
- [x] `cli/_common.py`'s camera-row formatting and `camera list`/`camera
      probe` output display flat-field presence and staleness alongside
      calibration state.
- [x] MCP's `list_cameras`/`get_camera_config` tool output includes the
      new `CameraState` fields.
      (`list_cameras` returns `CameraState` verbatim, so
      `flatfield_present`/`flatfield_stale` already reach the caller
      automatically -- verified with a new test. `get_camera_config`
      returns `CameraConfig`, the settings *document*, which has never
      carried any `CameraState` field, calibration included -- there is
      nothing to add there; `list_cameras` is the tool that actually
      surfaces camera state over MCP.)

## Testing

- **Existing tests to run**: `tests/aprilcam/mcp/`,
  `tests/aprilcam/cli/` (camera list/probe output tests) — no
  regressions.
- **New tests to write**: MCP tool tests for all three new tools using
  a fake `ConnectionManager`/`Daemon` (success, refusal, error
  mapping); CLI output tests confirming flat-field state appears in
  `camera list`/`camera probe`.
- **Verification command**: `uv run pytest tests/aprilcam/mcp/
  tests/aprilcam/cli/ && uv run pytest tests/test_no_cv2_in_client_modules.py`
