---
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 007 Use Cases

## SUC-001: MCP server uses daemon-authoritative paths for all file operations

**Actor**: AI agent (via MCP protocol)

**Preconditions**:
- The daemon is running, started from some working directory A.
- The MCP server (Claude Code session) is started from a different directory B.

**Main Flow**:
1. Agent calls `open_camera` — MCP server receives `camera_dir` from daemon response and stores it in `_cam_info`.
2. Agent calls `create_path` — MCP server resolves `paths.json` using `_cam_info["camera_dir"]`; file is written to the daemon's data directory.
3. Agent calls `create_playfield` — calibration info is loaded from `_cam_info["camera_dir"]`, not from a locally computed path.
4. Agent calls `calibrate_playfield` — calibration is saved to `_cam_info["camera_dir"]`; daemon reloads it and finds it.
5. Agent restarts MCP session; calls `open_camera` again — `camera_dir` is re-fetched from daemon and stored fresh.

**Postconditions**:
- `paths.json` resides in the daemon's data directory regardless of MCP server CWD.
- Calibration files are saved where the daemon expects them.
- Paths appear in live view after `create_path`.
- Calibration persists across MCP server restarts.

**Acceptance Criteria**:
- [ ] `open_camera` returns `camera_dir` from the daemon (absolute path).
- [ ] `_cam_info` stores `camera_dir` for each open camera.
- [ ] `_get_paths_file()` derives `paths_file` from `_cam_info["camera_dir"]`; no disk read, no `Config.load()`.
- [ ] `create_playfield` uses `_cam_info["camera_dir"]`; no `Config.load()`.
- [ ] `calibrate_playfield` saves to `_cam_info["camera_dir"]`; no `Config.load()`.
- [ ] `open_camera` no longer calls `Config.load()` to compute `paths_file`.
- [ ] Proto regeneration succeeds; `aprilcam_pb2_grpc.py` uses package-relative import.
