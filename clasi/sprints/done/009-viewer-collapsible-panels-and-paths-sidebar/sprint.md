---
id: 009
title: Viewer collapsible panels and paths sidebar
status: done
branch: sprint/009-viewer-collapsible-panels-and-paths-sidebar
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
issues: []
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 009: Viewer collapsible panels and paths sidebar

## Goals

1. Replace the four fixed `LabelFrame` sections in the `aprilcam view` right panel
   with a reusable `CollapsibleFrame` widget that lets users collapse sections they
   don't need.
2. Wire the Objects section collapse state directly to object detection — open means
   detection on, closed means detection off — and remove the redundant toggle button.
3. Add an optional `name` field to the `Path` data model and the `create_path` MCP
   tool so paths have human-readable display names.
4. Add a new collapsible Paths sidebar section that shows each path with a symbol
   preview, line color swatch, display name, and a delete button; collapsing the
   section hides all paths from the video display.

## Problem

The right-side info panel in `aprilcam view` has no way to collapse sections, forcing
users to see all four fixed sections regardless of what they need. The Objects section
has a separate toggle button that is redundant with the section itself. Paths created
by agents are visible in the video but have no UI representation in the panel and
cannot be managed (renamed or deleted) from the viewer. The `Path` model has no
`name` field, so agents cannot label paths for display.

## Solution

Introduce a `CollapsibleFrame(tk.Frame)` helper class with a header row (triangle
toggle + title) and a content sub-frame that hides/shows via `grid_remove()` /
`grid()`. Replace all four existing `LabelFrame` sections with `CollapsibleFrame`
instances. Remove the standalone Objects toggle button; drive `_detect_objects`
directly from the Objects `CollapsibleFrame` expand/collapse callback.

Add `name: str = ""` to the `Path` dataclass and propagate it through `to_dict()`,
`from_dict()`-style deserialization in `_load_paths()`, and the `create_path` MCP
tool. No protocol changes are needed.

Add a Paths collapsible section below Objects. It polls `_load_paths()` each frame
and renders one row per path: 16×16 symbol canvas, 20×4 line-color swatch, name
label (falls back to `path_id`), and a delete button. Collapsing the section sets
`_show_paths = False` in the render loop, hiding all paths from the video frame.

## Success Criteria

- All four existing sections (Camera Status, Mobile Tags, Stationary Tags, Objects)
  start expanded with ▼ triangles and collapse to header-only on click.
- Collapsing Objects stops detection; expanding it resumes (lazy ColorClassifier init
  preserved).
- `create_path` accepts a `name` param; stored path shows the name in the Paths panel.
- Paths with no name show `path_id` as the fallback label.
- Collapsing the Paths section removes all paths from the video display.
- Clicking the delete button removes the path from `paths.json` and from the panel.
- All existing tests pass.

## Scope

### In Scope

- `CollapsibleFrame` widget class in `view_cli.py`
- Replacement of all four `LabelFrame` sections with `CollapsibleFrame`
- Objects collapse/expand wiring to `_detect_objects`
- Removal of the standalone `btn_objects` toggle button
- `name: str = ""` field on `Path` dataclass (`paths.py`)
- `to_dict()` and `_load_paths()` deserialization updated for `name`
- `name` parameter on `create_path` MCP tool (`mcp_server.py`)
- Paths section in the right panel with symbol preview, swatch, label, delete button
- `_show_paths` bool controlling `draw_paths()` in the render loop

### Out of Scope

- Editing path names from the viewer
- Reordering paths
- Per-waypoint visibility controls
- Any changes to the daemon or detection pipeline
- Streamable HTTP transport

## Test Strategy

Manual verification per the issue's verification checklist:
1. Launch `aprilcam view 4`, confirm all sections start expanded.
2. Toggle each section collapse/expand.
3. Collapse/expand Objects; verify detection state changes.
4. Collapse/expand Paths; verify video show/hide behavior.
5. Call `create_path` with and without `name`; verify panel display.
6. Click delete button; verify path removed from panel and `paths.json`.

Automated: `uv run pytest tests/ -q` must pass after changes.

## Architecture Notes

- `CollapsibleFrame` uses `tk.Frame` as its base; header uses `grid` internally;
  content frame toggled via `grid_remove()` / `grid()`. Fits the existing `pack`
  layout of the right panel.
- `_show_paths` is a mutable container `[True]` (same pattern as `_detect_objects`
  threading.Event) accessible from both the `CollapsibleFrame` callback and the
  `_process_frame_and_tags` closure.
- `Path.name` is UI metadata only; it is not included in the ZMQ stream protocol.
  `to_dict()` writes it; `_load_paths()` reads it with `default=""`.
- `PathRegistry.create()` gains a `name` parameter; `delete_path` MCP tool is
  unchanged (already exists).

## GitHub Issues

(None — tracked via CLASI issue file.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Path model name field and create_path name param | — |
| 002 | CollapsibleFrame widget, all UI sections, Objects wiring, Paths section | 001 |

Tickets execute serially in the order listed.
