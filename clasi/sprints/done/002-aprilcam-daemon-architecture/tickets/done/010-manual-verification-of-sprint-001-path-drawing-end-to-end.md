---
id: '010'
title: Manual verification of sprint 001 path drawing end-to-end
status: done
use-cases:
- SUC-009
depends-on:
- 009
github-issue: ''
issue:
- aprilcam-daemon-architecture.md
- agent-drawn-paths-on-the-aprilcam-live-view.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Manual verification of sprint 001 path drawing end-to-end

## Description

This ticket closes out the deferred T005 from sprint 001. The sprint-001 code
for agent-drawn paths is complete (paths.py, 4 MCP tools, draw_paths in
display.py). The daemon architecture introduced in this sprint eliminates the
camera-ownership conflict that prevented final manual verification.

This ticket is a manual test execution: the programmer follows the verification
sequence below with real camera hardware and confirms all steps pass.

No code changes are expected. If a bug is found, it should be fixed in the
appropriate prior ticket (reopen that ticket) before marking this one done.

## Acceptance Criteria

The following manual test sequence must all pass (from the daemon-architecture
issue, items 1-10, plus the sprint-001 path-drawing verification):

**Daemon and auto-spawn:**
- [ ] With no daemon running, `aprilcam view --camera <cam>` auto-spawns the
  daemon and displays the camera feed with tag overlays.
- [ ] `pgrep -fl aprilcamd` shows exactly one daemon after multiple clients
  are started.
- [ ] Two simultaneous `aprilcam view` windows both show the same feed.

**Path drawing (sprint 001 feature):**
- [ ] `open_camera(0)` → `cam_0`; `create_playfield("cam_0")` → `pf_cam_0`;
  `calibrate_playfield("pf_cam_0")` succeeds.
- [ ] `create_path(pf_cam_0, [...filled_square at (10,10) 3cm red, circle at
  (50,50) 4cm blue...])` → `path_000`; both viewer windows show the path.
- [ ] Colors appear correct (red is red, not blue — RGB-to-BGR check).
- [ ] Adding a second path while viewers are running: path appears within
  approximately one frame (~33ms).
- [ ] `delete_path("path_000")` — first path vanishes from both viewers within
  one frame; second path remains.
- [ ] Stop and restart one viewer — second path reappears immediately from
  `paths.json` without any MCP interaction.
- [ ] `clear_paths(pf_cam_0)` — all paths vanish from both viewers within one
  frame; `list_paths` returns `[]`.

**Size scaling:**
- [ ] A 10cm `filled_circle` looks roughly twice as wide as a 5cm one on the
  same screen.

**Error cases (quick check):**
- [ ] `delete_path("path_999")` → `{"error": "Unknown path_id 'path_999'"}`.
- [ ] `create_path("pf_nope", "[]")` → `{"error": ...}`.

**Calibration while viewer active:**
- [ ] `aprilcam calibrate` runs without error while an `aprilcam view` window
  is displaying the same camera.

**MCP server restart:**
- [ ] Stopping and restarting the MCP server — daemon keeps publishing;
  viewers keep running.

**Config override:**
- [ ] Set `APRILCAM_DATA_DIR=/tmp/test-aprilcam` in env, kill daemon, run a
  client — confirms new daemon uses the overridden path.

## Implementation Plan

### Approach

This is manual test execution, not code implementation. The programmer:
1. Runs `uv run pytest` to confirm all unit tests pass.
2. Follows the verification checklist above with a connected camera.
3. Records pass/fail for each item.
4. If any item fails: identifies the bug, reopens the relevant ticket, fixes
   it there, then re-runs this verification.
5. Marks this ticket done when all items pass.

### Files to Create / Modify

None (this is a verification ticket).

### Testing Plan

This ticket IS the testing. Prerequisite: `uv run pytest` exits 0.

### Documentation Updates

None.
