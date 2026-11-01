---
id: '006'
title: Surface placeholder flag in CLI, MCP tools and client library
status: open
use-cases:
- SUC-004
depends-on:
- '005'
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Surface placeholder flag in CLI, MCP tools and client library

## Description

Make consumers able to tell a test card from a real frame. Client library
exposes `placeholder` on frame/stream results and the absent heartbeat
status. CLI: `camera image` and `playfield image` still write the card
file, print a stderr warning, and exit 3 (documented; stakeholder may
prefer warn-only); `camera view` / `playfield view` overlay or print a
"NO SIGNAL (placeholder)" notice. MCP tools (`get_frame`, tag tools)
include the flag / absent status in their results. Clients stay thin
(no OpenCV).

## Plan

- Files: client library, `cli` camera/playfield commands, MCP tool
  modules, CLI help text.

## Acceptance Criteria

- [ ] Client library exposes the flag on every frame/stream result.
- [ ] `camera image` on a placeholder writes the file, warns, exits 3; real frame exits 0.
- [ ] `camera view` and `playfield image/view` indicate placeholder state.
- [ ] MCP get_frame result includes the flag; tag tools report camera-absent.

## Testing

- **Existing tests to run**: `uv run pytest tests/cli tests/mcp tests/client -q`
- **New tests to write**: CLI exit-code/warning tests with a stub client, MCP result field tests.
- **Verification command**: `uv run pytest tests/cli tests/mcp -q`
