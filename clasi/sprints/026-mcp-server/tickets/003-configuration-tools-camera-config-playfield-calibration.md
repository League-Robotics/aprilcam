---
id: '003'
title: Configuration tools (camera config, playfield, calibration)
status: open
use-cases:
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: mcp-server-the-agent-facing-product.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Configuration tools (camera config, playfield, calibration)

## Description

`mcp/tools/configuration.py`: whole-document configuration and
calibration — `get_camera_config`, `set_camera_config`,
`reset_camera_config`, `get_playfield`, `set_playfield`,
`list_playfields`, `calibrate`, `get_calibration`.

The push tools (`set_camera_config`/`set_playfield`) accept the
document as a **JSON object tool parameter, never a file path** (spec
principle 7 — MCP clients have no filesystem the daemon can reach).
This requires extending `config/loader.py` with dict-based parsing
entry points that share every validation rule the existing file-based
`load_camera_config`/`load_playfield` already enforce, rather than
re-implementing whole-document validation a second time inside `mcp/`
(sprint.md's Architecture Design Rationale). Do this refactor first —
it is this ticket's foundation, not an aside.

Runs strictly after ticket 001 on the shared sprint branch (no
worktree parallel execution opted into this sprint); do not start
before 001 merges. Independent of tickets 002/004/005.

## Acceptance Criteria

- [ ] `config/loader.py` gains dict-based parsing entry points (e.g.
      `ConfigLoader.parse_camera_config(data: dict) -> CameraConfig`,
      `ConfigLoader.parse_playfield(data: dict) -> PlayfieldDefinition`)
      factored out of the existing `_read_json_object` +
      `load_camera_config`/`load_playfield` internals, so the file-based
      and dict-based paths share every validation rule (self-naming
      check, geometry checks, per-section entry validation, duplicate-id
      rejection). `load_camera_config`/`load_playfield` (file-based)
      keep their existing signatures and behaviour unchanged — this is a
      refactor of shared internals, not a contract change.
- [ ] `set_camera_config(document: dict)` / `set_playfield(document: dict)`
      accept a JSON object; a malformed document raises
      `MalformedDocument`, mapped to a tool error naming exactly what
      was wrong, with **no RPC attempted** (client-side validation
      catches it first, per UC-23.S-4).
- [ ] `get_camera_config(camera)` / `get_playfield(name)` /
      `list_playfields()` return the whole document in its
      `to_dict()`-equivalent shape — loadable verbatim, matching the
      round-trip contract `config/loader.py` already documents.
- [ ] `reset_camera_config(camera)` -> re-applies the stored on-disk
      config, discarding any live override.
- [ ] `calibrate(camera)` -> `CalibrationResult` (`succeeded`,
      `matched_ids`, `missing_ids`, `reprojection_error_cm`, `reason`).
      A camera with no playfield link fails with the daemon's
      `NoPlayfieldLinked` error **before any frame is requested** —
      verified against a fake `Daemon` that raises if any frame-related
      method is ever called for that case.
- [ ] `get_calibration(camera)` -> a `Calibration` or `None`, never a
      fabricated default (mirrors `get_tag`'s Optional-not-zero-valued
      discipline).
- [ ] Every tool wrapped with ticket 001's error-mapping decorator.
- [ ] `uv run mypy` clean; zero `typing.Protocol`.
- [ ] Full suite green.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/config/
  tests/aprilcam/mcp/ -v` plus the full headless suite — the
  `config/loader.py` refactor must not change `load_camera_config`/
  `load_playfield`'s existing behaviour, so their current test suite is
  the regression gate.
- **New tests to write**: loader round-trip tests proving
  `parse_camera_config(json.loads(path.read_text()))` and
  `load_camera_config(path)` produce equal documents for the same
  content; one tool test per method against a fake `Daemon`; a
  malformed-document test proving zero RPCs are attempted;
  `calibrate`'s `NoPlayfieldLinked`-before-any-frame-request test.
- **Verification command**: `uv run pytest -m "not camera"` (CI), full
  `uv run pytest` plus `uv run mypy` on this development machine.

## Implementation Plan

**Approach**: `config/loader.py`'s dict-based extraction first (every
other acceptance criterion in this ticket depends on it), then camera
config tools, then playfield tools, then calibration last — matching
UC-15's own configure-then-calibrate ordering.

**Files to modify**: `src/aprilcam/config/loader.py` (extend with
dict-based parsing, refactor internals to share them).

**Files to create**: `src/aprilcam/mcp/tools/configuration.py`.

**Files to modify (cont.)**: `src/aprilcam/mcp/server.py` (register
this tool group).

**Documentation updates**: none beyond docstrings.
