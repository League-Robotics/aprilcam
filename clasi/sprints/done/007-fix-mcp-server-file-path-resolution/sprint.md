---
id: '007'
title: Fix MCP server file path resolution
status: done
branch: sprint/007-fix-mcp-server-file-path-resolution
use-cases:
- SUC-001
issues:
- fix-mcp-server-must-not-compute-file-paths-independently-from-daemon.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 007: Fix MCP server file path resolution

## Goals

Eliminate all independent `Config.load()` calls in the MCP server that compute data file paths. Instead, have the daemon return its authoritative per-camera data directory at `open_camera` time, and have the MCP server cache and reuse that path everywhere.

## Problem

The MCP server calls `Config.load()` in four places to compute file paths independently. `Config.load()` defaults `data_dir` to `./data/aprilcam/` — relative to CWD. When the MCP server is started from a different working directory than the daemon, these paths diverge silently:

- `paths.json` is written to the wrong location — paths never appear in live view
- Calibration data is saved/loaded from the wrong directory — playfield calibration breaks
- The `_get_paths_file()` fallback reads `info.json` from the wrong directory

The daemon is the authority on where its files live. The MCP server must not compute data paths independently.

## Solution

Add a `camera_dir` field to `OpenCameraResponse` in the proto. The daemon returns the absolute path to its per-camera data directory. The MCP server stores it in `_cam_info` at `open_camera` time and uses it in all four problem sites instead of calling `Config.load()`.

## Success Criteria

1. Start daemon from directory A, MCP server (Claude Code session) from directory B.
2. `open_camera` → `create_playfield` → `create_path` — path appears in live view.
3. `calibrate_playfield` → restart MCP server → `create_path` — calibration persists.
4. `paths.json` is located in the daemon's data dir (`data/aprilcam/cameras/<cam>/`), not CWD.
5. No `Config.load()` calls remain in `mcp_server.py` for path computation.

## Scope

### In Scope

- Add `camera_dir` field to `OpenCameraResponse` proto message
- Daemon `OpenCamera()` RPC populates `camera_dir` in both return paths
- Client `open_camera()` returns `(cam_name, camera_dir)` tuple
- MCP server: four sites updated to use `_cam_info["camera_dir"]`
- Regenerate protobuf bindings; fix bare import in `aprilcam_pb2_grpc.py`

### Out of Scope

- Any other proto message changes
- Changes to detection, overlay, or playfield logic
- New MCP tools or CLI commands

## Test Strategy

Manual verification per the success criteria above. The fix is a plumbing change — the observable behavior (paths land in the correct directory) is the test. Existing tests continue to pass.

## Architecture Notes

The fix is additive (new proto field, no renumbering). All existing callers that do not read `camera_dir` are unaffected. The `_cam_info` dict already exists in `mcp_server.py`; adding `"camera_dir"` is a backward-compatible extension of that structure.

## GitHub Issues

None.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Fix MCP server file path resolution via daemon-returned camera_dir | — |

Tickets execute serially in the order listed.
