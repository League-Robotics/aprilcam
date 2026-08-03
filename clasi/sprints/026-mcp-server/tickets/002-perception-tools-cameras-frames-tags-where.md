---
id: '002'
title: Perception tools (cameras, frames, tags, where)
status: open
use-cases:
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: mcp-server-the-agent-facing-product.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Perception tools (cameras, frames, tags, where)

## Description

`mcp/tools/perception.py`: the read-only query surface — `list_cameras`,
`probe_cameras`, `get_frame`, `get_tags`, `get_tag`, `get_tag_history`,
`where`. Each tool is a thin wrapper (per `adapters.md`'s "translation
layer and nothing more"): resolve a `Daemon` via ticket 001's
`ConnectionManager`, call the matching `Daemon` method, shape the
return value into MCP tool output. `get_frame` is the only tool in this
group that touches `mcp/images.py`; every tool is wrapped by ticket
001's error-mapping decorator — no tool re-derives its own try/except.

Runs strictly after ticket 001 on the shared sprint branch (no
worktree parallel execution opted into this sprint); do not start
before 001 merges.

## Acceptance Criteria

- [ ] `list_cameras()` -> every `CameraState` (name, present, usable,
      playfield link, calibrated, calibration_stale, frame_dark,
      number) as structured tool output — an absent-but-registered
      camera is listed and flagged, never omitted (UC-25).
- [ ] `probe_cameras()` -> the full `ProbeReport` (refreshed cameras
      plus any skipped backends, named) — a partial probe never reads
      as a complete one (UC-25.S-7).
- [ ] `get_frame(camera, deskewed=False, encoding="jpeg", format="base64")`
      -> uses `mcp/images.py` for output shaping; a `deskewed=True`
      request on an uncalibrated camera surfaces the daemon's
      `NotCalibrated` error as a tool error, **never** a silently
      substituted raw frame.
- [ ] `get_tags(camera)` / `get_tag(camera, family, number)` /
      `get_tag_history(camera, family=None, number=None, frames=None)`
      -> world-first `TagRecord`/`TagFrame` shapes; `get_tag` returns
      null for "not detected", never a fabricated `(0, 0)` position.
- [ ] `where(playfield, query)` -> wraps `Daemon.find_feature`.
      Ambiguous queries return **every** match, never an arbitrary pick
      (UC-08.S-2); no match returns an empty list, not an error
      (UC-08.S-3).
- [ ] Every tool above wrapped with ticket 001's error-mapping
      decorator; no per-tool try/except duplicating it.
- [ ] `uv run mypy` clean; zero `typing.Protocol` in this module.
- [ ] Full suite green (no regressions to the 976-test baseline plus
      ticket 001's additions).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/mcp/ -v`
  (ticket 001's tests) plus the full headless suite.
- **New tests to write**: one success-path test per tool against a fake
  `Daemon` (the established CLI-test pattern —
  `tests/aprilcam/cli/test_camera_cli.py`'s `_FakeDaemon`), plus one
  failure-mode test per tool where the issue names one: `NotCalibrated`
  on `get_frame(deskewed=True)`, `UnknownCamera` on a bad camera name,
  an empty-tuple result from `where()` on no match, an ambiguous-query
  result returning every match.
- **Verification command**: `uv run pytest -m "not camera"` (CI), full
  `uv run pytest` plus `uv run mypy` on this development machine.

## Implementation Plan

**Approach**: cameras and frames first (no coordinate-presence
subtleties), then tags (world-vs-pixel Optional discipline per
`types.TagRecord`'s docstring), then `where` last (thinnest wrapper,
depends on nothing this ticket itself builds).

**Files to create**: `src/aprilcam/mcp/tools/perception.py`
(and `src/aprilcam/mcp/tools/__init__.py` if not already present from
ticket 001's package layout).

**Files to modify**: `src/aprilcam/mcp/server.py` (register this
tool group).

**Documentation updates**: none beyond docstrings.
