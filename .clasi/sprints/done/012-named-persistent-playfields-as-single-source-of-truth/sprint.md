---
id: '012'
title: Named Persistent Playfields as Single Source of Truth
status: done
branch: sprint/012-named-persistent-playfields-as-single-source-of-truth
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
issues:
- plan-named-persistent-playfields-as-the-single-source-of-truth.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 012: Named Persistent Playfields as Single Source of Truth

## Goals

1. Promote the `data/aprilcam/playfield.json` definition into a named, persistent
   `data/aprilcam/playfields/main-playfield.json` file; introduce a
   `PlayfieldDefinitionRegistry` loaded at MCP server startup.
2. Establish a new per-camera `config.json` (daemon-never-writes) that links a
   camera to its playfield slug, making the geometry relationship explicit and
   machine-readable.
3. Make calibration derive ALL geometry (dimensions, origin, corner ArUco IDs and
   world positions) from the referenced playfield definition — so a
   camera/playfield dimension mismatch is structurally impossible.
4. Auto-rehydrate a `PlayfieldEntry` from disk on `open_camera`, eliminating the
   need for an explicit `create_playfield` call in normal use.
5. Add provenance tracking to `calibration.json` and surface a mismatch/stale
   warning when the stored provenance disagrees with the current config or def.

## Problem

Two disconnected notions of "playfield" currently coexist and disagree. The
canonical `playfield.json` (134.3 × 89.3 cm, center origin) is used only by the
`where` tool. Per-camera `calibration.json` files store their own `playfield`
block with different dimensions (109 × 79.5 cm) and corner world positions
computed from a broken corner origin. Calibration accepts user-supplied
width/height parameters with no link to the definition, so every calibration
session can introduce a different geometry. No runtime state survives server
restart — the agent must call `create_playfield` each session.

## Solution

Introduce a `PlayfieldDefinition` model and `PlayfieldDefinitionRegistry` that
loads all `*.json` files from `data/aprilcam/playfields/` at startup. A new
per-camera `config.json` (sole key: `playfield`) links a camera to its playfield
slug. Calibration resolves the camera's `config.json` → playfield slug → def,
pulling all geometry from the def; it writes provenance fields
(`calibrated_playfield`, `calibrated_camera`) into `calibration.json`. On
`open_camera`, the server reads `config.json` + calibration and reconstructs the
runtime `PlayfieldEntry` without any explicit `create_playfield` call. Both the
MCP `calibrate_playfield` tool and the `aprilcam calibrate` CLI share a single
helper — `calibrate_from_playfield_def` in `calibration.py` — to prevent
divergence. A one-time data migration moves `playfield.json` →
`playfields/main-playfield.json` and writes `config.json` for the three existing
calibrated cameras.

## Success Criteria

- `open_camera` on a camera with a `config.json` and matching `calibration.json`
  auto-creates a `PlayfieldEntry` with correct center-origin world coordinates;
  no `create_playfield` call is needed.
- `calibrate_playfield` (MCP) on a camera without `config.json` → hard error with
  precise instructions (what file to create, available playfields).
- `aprilcam calibrate` (CLI) on a camera without `config.json` → same hard error
  via the same shared code path.
- `pixel_to_world` of the AprilTag A1 pixel ≈ (0, 0) after calibration.
- Mismatch warning fires when the stored `calibrated_playfield` ≠ current
  `config.json` playfield.
- `pytest` passes with new tests in `test_playfield_def.py`, extended
  `test_calibration_geometry_persist.py`, `test_camera_registry.py`, and
  `test_mcp_path_tools.py`.
- Version bumped in `pyproject.toml`.

## Scope

### In Scope

- New `src/aprilcam/core/playfield_def.py` — `PlayfieldDefinition` dataclass and
  `PlayfieldDefinitionRegistry`.
- New per-camera config helpers: `load_camera_config` / `save_camera_config` (new
  small module `src/aprilcam/camera/camera_config.py`).
- `Config` extended with `playfields_dir` property (no env var needed).
- Data migration: `playfield.json` → `playfields/main-playfield.json`;
  `config.json` written for three camera dirs.
- Calibration refactor in `calibration.py`: shared `calibrate_from_playfield_def`
  helper; precondition error; provenance fields in `calibration.json`; mismatch
  detection in `load_calibration_from_camera_dir`.
- `_handle_open_camera` in `mcp_server.py`: auto-rehydrate `PlayfieldEntry` from
  disk after daemon open.
- `calibrate_playfield` MCP tool: delegate to the shared helper, drop
  `width`/`height` as required parameters (derive from def).
- `_handle_create_playfield`: reconcile with the def's corner IDs and world coords
  when a def is linked.
- `playfield_query.py` / `where` tool: load from `PlayfieldDefinitionRegistry`
  (new path), with fallback to old `playfield.json` path for backward compat.
- `mcp_server.py` startup: build `PlayfieldDefinitionRegistry` before first tool call.
- Optional ergonomics: `set_camera_playfield` MCP tool (own ticket, droppable).
- Tests: `tests/test_playfield_def.py` (new); extensions to three existing test
  files.
- Docs: `ROBOT_API_GUIDE.md` and any AGENT_GUIDE mention of calibration workflow.
- Version bump.

### Out of Scope

- Multi-playfield scenarios (a camera viewing multiple playfields).
- Secondary-camera joint calibration via the def (the secondary path reads world
  coords from the primary, not from a def; no change needed this sprint).
- Streamable HTTP transport.
- Any UI or display changes.
- Changes to the daemon (`camera_pipeline.py`, `grpc_server.py`) beyond confirming
  the daemon boundary (daemon never writes `config.json`).

## Test Strategy

- **Unit (no camera hardware):** def load + registry scan; `config.json`
  round-trip; `calibrate_from_playfield_def` precondition raises the guidance
  error when no `config.json` / no def; provenance mismatch detection; startup
  rehydrate builds a `PlayfieldEntry` from fixture data with no camera call.
- **Integration (pytest):** extend `test_calibration_geometry_persist.py`,
  `test_camera_registry.py`, `test_mcp_path_tools.py`; add
  `tests/test_playfield_def.py`.
- **Manual end-to-end:** documented in Verification section of the linked issue.

## Architecture Notes

- The def's center-origin coordinate system (A1 = 0,0) replaces the old
  corner-origin system. All existing calibrations are invalidated and must be
  re-run. The stakeholder has accepted this consequence.
- Corner ArUco IDs come from the def (diagonal-cardinals 1/3/5/7 in
  `main-playfield.json`) — no more hardcoded IDs 0-3.
- The daemon boundary is preserved: the daemon only reads `calibration.json`
  (homography), never writes or reads `config.json` or any playfield def.
- Shared calibration helper enforced: both MCP and CLI call
  `calibrate_from_playfield_def` — the only place that resolves config → def →
  geometry → homography.

## GitHub Issues

(None — all context in the linked CLASI issue.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Config plumbing: playfields_dir + per-camera config.json helpers | — |
| 002 | PlayfieldDefinition model, registry, and where-tool repoint | 001 |
| 003 | Data migration: playfield.json → playfields/main-playfield.json + 3 config.json files | 001, 002 |
| 004 | Calibration refactor: shared helper, precondition error, provenance fields, mismatch detection | 001, 002, 003 |
| 005 | open_camera auto-rehydrate PlayfieldEntry; wire calibrate_playfield and CLI to shared helper | 001, 002, 003, 004 |
| 006 | Optional: set_camera_playfield MCP tool | 001, 002, 005 |
| 007 | Tests, ROBOT_API_GUIDE docs, and version bump | 001, 002, 003, 004, 005 |

Tickets execute serially in the order listed. Ticket 006 is optional and may be
dropped without affecting the other tickets.
