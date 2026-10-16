---
id: 009
title: MCP tools for the intrinsics session
status: open
use-cases: [SUC-003]
depends-on: ["006"]
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# MCP tools for the intrinsics session

## Description

MCP tools mirroring the four daemon commands so an agent can run the workflow while a human holds the board. Follow `mcp/tools/configuration.py`'s sprint 037 pattern (`connection.call`, `handle_tool_errors`, optional `daemon` arg, and the NONE-sentinel/empty-arg guidance in `.claude/rules/tool-call-empty-args.md`).

## Acceptance Criteria

- [ ] `start_intrinsics(camera, board=None, daemon=None)`, `get_intrinsics_status(camera, daemon=None)`, `finish_intrinsics(camera, install=False, force=False, daemon=None)`, `abort_intrinsics(camera, daemon=None)` registered and listed by the server.
- [ ] Return values are plain JSON-friendly dicts (status includes `banner`, `next_hint`, coverage grids, rms/residual/correction, errors); numpy-free.
- [ ] Tool docstrings tell an agent how to use them: poll status, read `next_hint`, relay it to the human, finish only at `good_enough`, always abort when giving up.
- [ ] `src/aprilcam/mcp/DESIGN.md` tool table and `get_guide` content mention the new tools; `tests/test_no_cv2_in_client_modules.py` passes.

## Implementation Plan

Create `src/aprilcam/mcp/tools/intrinsics.py`; register in `mcp/tools/__init__.py`/server; update `mcp/DESIGN.md` and the agent guide text (`src/aprilcam/AGENT_GUIDE.md`) as needed.

## Testing

New `tests/aprilcam/mcp/test_intrinsics_tools.py` with a fake daemon: each tool forwards arguments and serialises results; error mapping; tool list contains the four names. Run `uv run pytest tests/aprilcam/mcp tests/test_no_cv2_in_client_modules.py`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
