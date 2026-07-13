---
id: '001'
title: Agent-Drawn Paths on the Live View
status: done
branch: sprint/001-agent-drawn-paths-on-the-live-view
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
issue: .clasi/issues/agent-drawn-paths-on-the-aprilcam-live-view.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 001: Agent-Drawn Paths on the Live View

## Goals

Enable AI agents to draw labeled, color-coded waypoint paths onto the
AprilCam live playfield display in real time. Agents submit paths in
world coordinates (cm); the server renders lines and symbols scaled
to the physical playfield. Paths are persistent across live-view
restarts.

## Problem

Agents using the AprilCam MCP server can read the playfield but cannot
annotate it. There is no way for an agent to plan a route and have the
MCP server render that route on the live deskewed view — blocking agent
workflows that require visual confirmation of planned motion or coverage
paths.

## Solution

Introduce four new MCP tools (`create_path`, `delete_path`,
`list_paths`, `clear_paths`) backed by an in-process `PathRegistry`.
Paths are forwarded to the live-view child process via a new OS pipe
(line-delimited JSON commands: add / remove / clear). The child
maintains a local dict and calls `PlayfieldDisplay.draw_paths()` each
frame. Waypoint symbols (8 types) and connecting lines scale with the
physical playfield via per-waypoint `size_cm` mapped through the
existing inverse-homography pipeline.

## Success Criteria

- `create_path` accepts a JSON waypoint list and returns a `path_id`.
- Paths render immediately on a running live view and reappear after a
  live-view restart (initial-state push).
- All 8 symbols render correctly; colors are correctly RGB→BGR at the
  drawing boundary.
- `delete_path`, `list_paths`, and `clear_paths` behave as specified.
- Verification sequence in the issue file passes without errors.

## Scope

### In Scope

- New `src/aprilcam/server/paths.py` module: `Symbol`, `RGB`,
  `Waypoint`, `Path`, `PathRegistry`.
- Four new MCP tools in `mcp_server.py`.
- Third OS pipe (command pipe) in `liveview.py` with
  `set_initial_paths()` and `send_command()` on `LiveViewProcess`.
- `PlayfieldDisplay.draw_paths()` in `display.py` with all 8 symbols
  and per-waypoint `size_cm` scaling.
- `_live_view_for_playfield()` helper in `mcp_server.py`.
- Version bump to `0.20260514.1` in `pyproject.toml`.
- Manual end-to-end verification per the issue file.

### Out of Scope

- Per-waypoint mutation (paths are submitted whole; no patch endpoint).
- Paths on raw cameras (playfield_id required).
- Streamable HTTP transport.
- Automated integration tests requiring live camera hardware.
- Any change to the existing tag detection, ring buffer, or frame
  capture paths.

## Test Strategy

Unit tests cover `PathRegistry` (create, delete, list, clear, ID
monotonicity) and `create_path` input validation without live hardware.
The drawing method and IPC pipe are verified manually using the
step-by-step sequence in the issue file's Verification section.

## Architecture Notes

The design is specified in detail in the issue file. Key constraints:

- Child process never imports the `Path` dataclass; it consumes plain
  dicts from the pipe.
- RGB→BGR conversion happens only at the OpenCV draw call boundary
  inside `draw_paths`.
- `path_registry` lives in the parent process and outlives any child;
  live-view restart reseeds from the registry.
- Homography is snapshotted at child startup; post-startup calibration
  does not affect the running view (pre-existing limitation).

## GitHub Issues

(none)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On | Group |
|---|-------|------------|-------|
| 001 | Paths data model and registry | — | 1 |
| 002 | MCP path tools and playfield lookup helper | 001 | 2 |
| 003 | Live-view command pipe IPC | 001, 002 | 3 |
| 004 | draw_paths rendering in PlayfieldDisplay | 003 | 4 |
| 005 | End-to-end verification and version bump | 004 | 5 |

**Execution groups** — groups execute in order; tickets within a group
may run in parallel.

- **Group 1**: T001 (foundation — no dependencies)
- **Group 2**: T002 (depends on T001)
- **Group 3**: T003 (depends on T001 and T002)
- **Group 4**: T004 (depends on T003)
- **Group 5**: T005 (depends on T004 — verification gate)
