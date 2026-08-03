---
status: pending
---

# MCP server — the agent-facing product

## Description

**This is what the project is for.** `.claude/rules/project-overview.md`
opens with the problem statement: AI agents working with robotics
playfields need to understand the physical layout without doing their
own vision processing, and there is no programmatic interface for it.
The package is named "AprilCam MCP Server". Five sprints have built a
daemon that answers every one of those questions — and **no `mcp/`
subsystem exists in the v2 tree at all**. `aprilcam mcp` is not a
command.

Everything it needs is now built and proven on real hardware.

**What it is** (spec §4.7): a FastMCP stdio server that is a **pure
daemon client** — no OpenCV, no camera access, no authoritative state
of its own. An adapter in the strict sense of `adapters.md`: it
translates the daemon's contract into MCP tool calls and nothing more.

**Tool surface**, all over the existing client library:

- **Perception** — list/probe cameras, `get_tags`, `get_tag`, tag
  history, `get_frame` (raw and deskewed, base64 or temp-file per the
  caller's choice), `where` (feature lookup).
- **Configuration** — get/set playfield definition, get/set/reset
  camera config, list playfields and cameras with their links,
  calibration trigger.
- **Mobile tags** — register/unregister mount parameters (UC-12), which
  an agent needs so reported positions are the robot's.
- **Drawing** — `draw_marker`, `draw_path`, `draw_polygon`,
  `draw_circle`, `draw_text`, `remove_annotation`, `clear_layer`,
  `list_annotations` (spec §4.7, restored by §9 decision 8). Thin
  wrappers over the annotation RPCs; world coordinates only; no cv2.
- **Self-description** — the packaged guide (UC-27), which is how an
  agent orients itself before its first real call.

**Late binding** (UC-22): the server starts **unbound** and resolves a
daemon per call, so it survives daemon restarts rather than dying with
them. An agent session outlives any one daemon process.

**The vision boundary is structural here**: `tests/test_no_cv2_in_client_modules.py`
already enforces no cv2 under `client/` and `view/` — extend it to
`mcp/`. Consumers needing pixel work call `get_frame` and process the
image themselves.

## Cause

The daemon and CLI were built first, deliberately, because the MCP
server is an adapter over them and building it against an incomplete
daemon would mean building the tool surface twice. That reasoning has
held; the daemon is now complete enough that the adapter is a thin
translation layer rather than a design exercise.

## Proposed fix

New `src/aprilcam/mcp/` subsystem plus the `aprilcam mcp` CLI entry
point. Mine `archive/src/aprilcam/server/` for v1's tool surface — it
had a working MCP server and the tool *shapes* are earned knowledge —
but per `CLAUDE.md`'s archive rule, take the ideas, not the structure,
and do not carry forward the frame-processing registry or any local-cv2
path (both explicitly removed in v2, spec §6).

Image return format is per-request: base64 inline, or a temp file whose
path is returned. Agents cannot see a GUI and must never be handed a
file path they did not ask for.

## Verification

- A real agent session against a real daemon: orient via the guide,
  list cameras, calibrate, read tags in world coordinates, fetch a
  deskewed frame, draw a marker, and read it back — all through MCP
  tool calls with no shell access.
- The server starts with **no daemon running** and recovers when one
  appears (UC-22's late-binding property).
- No `cv2` in `sys.modules` after importing the MCP server.

## Related

- `.claude/rules/project-overview.md` — the problem statement this
  closes
- `docs/design/specification.md` §4.7, §9 decision 8;
  `docs/design/adapters.md` §4.2
- `docs/design/use-cases/cli.md` UC-22
- Sprint 026 in `docs/design/development-plan.md`'s sequence
- Depends on `complete-the-daemon-query-surface.md` (sprint 025) —
  `where`, mounts, and guides are tools this server exposes
