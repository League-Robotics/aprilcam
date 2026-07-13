---
id: '006'
title: Overlay Text, Rect, and Polygon Element Types
status: done
branch: sprint/006-overlay-text-rect-and-polygon-element-types
use-cases: []
issues: []
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 006: Overlay Text, Rect, and Polygon Element Types

## Goals

Add three new element types to the live overlay system: `text`, `rect`, and `polygon`.
Robot programs will be able to label tags with text, draw bounding boxes, and shade
zones directly on the playfield view.

## Problem

The overlay system (Sprint 005) supports `arc`, `arrow`, `point`, and `polyline`.
It cannot label positions with text, draw rectangles (bounding boxes), or fill
polygonal regions. These are the most common visual annotations robot programs need.

## Solution

- Add a `string text = 5` field to the `OverlayElement` protobuf message and
  regenerate Python bindings.
- Add `text`, `rect`, and `polygon` draw branches to `display.draw_live_overlay()`.
  `text` reuses the existing `_draw_text_with_outline()` static method.
- Propagate the `text` field through `DaemonControl.publish_overlay()`.
- Update the `set_live_overlay` MCP tool docstring and `ROBOT_API_GUIDE.md`.
- Add four unit tests to `tests/test_display_overlay.py`.

## Success Criteria

- `aprilcam_pb2.OverlayElement(text="hi")` constructs without error.
- `uv run pytest tests/test_display_overlay.py -v` passes all four new tests.
- `uv run pytest tests/ --ignore=tests/system -q` passes with no regressions.

## Scope

### In Scope

- `proto/aprilcam.proto` — add `string text = 5` to `OverlayElement`
- Proto regeneration and import fix in `aprilcam_pb2_grpc.py`
- `ui/display.py` — `text`, `rect`, `polygon` draw branches in `draw_live_overlay()`
- `client/control.py` — pass `text` field in `publish_overlay()`
- `server/mcp_server.py` — update `set_live_overlay` docstring
- `src/aprilcam/ROBOT_API_GUIDE.md` — update element types table and example
- `tests/test_display_overlay.py` — four new tests

### Out of Scope

- New element types beyond `text`, `rect`, and `polygon`
- Font selection or multi-line text
- Any changes to the overlay wire-format (TTL, timestamp, camera_id)
- Changes to the `view_cli` render loop (already handles `draw_live_overlay`)

## Test Strategy

Unit tests in `tests/test_display_overlay.py` cover each new element type:
- `test_text_draws`: element with `type="text"` and `text="hello"` produces non-zero pixels
- `test_text_empty_string`: empty string does not raise
- `test_rect_draws`: filled rect produces non-zero pixels
- `test_polygon_draws`: filled polygon produces non-zero pixels

All existing tests must continue to pass (no regressions in overlay or display tests).

## Architecture Notes

- The `text` field must be added to the proto before any code change — regeneration
  is a hard prerequisite for tickets 002 onward.
- `_draw_text_with_outline()` is a static method; the `text` draw branch calls it
  directly with no refactoring required.
- `rect` and `polygon` need no proto change — they fit in the existing `params` float list.

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
| 001 | Add text field to proto and regenerate bindings | — |
| 002 | Add text, rect, polygon draw branches to display.py | 001 |
| 003 | Propagate text field in control.py and update mcp_server docstring | 001 |
| 004 | Update ROBOT_API_GUIDE.md | 003 |
| 005 | Add unit tests for new overlay element types | 002, 003 |

Tickets execute serially in the order listed.
