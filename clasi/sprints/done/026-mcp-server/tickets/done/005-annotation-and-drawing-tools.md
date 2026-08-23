---
id: '005'
title: Annotation and drawing tools
status: done
use-cases:
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: mcp-server-the-agent-facing-product.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Annotation and drawing tools

## Description

`mcp/tools/annotations.py`: the drawing surface — `draw_marker`,
`draw_path`, `draw_polygon`, `draw_circle`, `draw_text`,
`remove_annotation`, `clear_layer`, `list_annotations`. Each `draw_*`
tool builds the matching `types.AnnotationShape` variant from flat
world-coordinate arguments and calls `Daemon.put_annotation`;
`clear_layer` calls `clear_annotations`; `list_annotations` calls
`get_annotations`. World-coordinates only, no cv2, **no rendering** —
authoring is a daemon RPC surface, rendering stays
`client.renderer.Renderer`'s job (spec §9 decision 8); this ticket
never rasterizes anything.

Runs strictly after ticket 001 on the shared sprint branch (no
worktree parallel execution opted into this sprint); do not start
before 001 merges. Independent of tickets 002/003/004.

## Acceptance Criteria

- [x] `draw_marker(playfield, layer, id, x, y, label=None, color=None,
      line_width=None, ttl_seconds=None)` -> builds a `Marker` +
      `Style`, wraps it in an `Annotation`, calls `put_annotation`.
- [x] `draw_path(playfield, layer, id, points, ...)` -> `PathShape`
      from a list of `[x, y]` pairs.
- [x] `draw_polygon(playfield, layer, id, points, ...)` -> `Polygon`.
- [x] `draw_circle(playfield, layer, id, center_x, center_y,
      radius_cm, ...)` -> `Circle`.
- [x] `draw_text(playfield, layer, id, x, y, text, ...)` -> `Text`.
- [x] `remove_annotation(playfield, layer, id)` -> calls
      `Daemon.remove_annotation`; an absent key is a no-op, not an
      error.
- [x] `clear_layer(playfield, layer=None)` -> calls
      `Daemon.clear_annotations`; clearing an already-empty or
      nonexistent layer is a no-op.
- [x] `list_annotations(playfield, layer=None)` -> the current
      `AnnotationSet` (`annotations`, `revision`, `timestamp`) via
      `get_annotations`.
- [x] An unknown playfield on any write is rejected, naming the
      playfields the daemon holds (per `Daemon.put_annotation`'s
      documented contract) — verified against a fake `Daemon` that
      raises `UnknownPlayfield`.
- [x] Every tool wrapped with ticket 001's error-mapping decorator.
- [x] `uv run mypy` clean; zero `typing.Protocol`.
- [x] Full suite green.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/mcp/ -v`
  plus the full headless suite.
- **New tests to write**: one test per tool against a fake `Daemon`;
  a round-trip test (`draw_marker` then `list_annotations` finds
  exactly what was drawn, matching UC-28.S-3's "how a robot reads
  another robot's plan"); the unknown-playfield rejection test;
  `remove_annotation`/`clear_layer` no-op-on-absent tests.
- **Verification command**: `uv run pytest -m "not camera"` (CI), full
  `uv run pytest` plus `uv run mypy` on this development machine.

## Implementation Plan

**Approach**: one small shape-construction helper per
`AnnotationShape` variant first (`Marker`, `PathShape`, `Polygon`,
`Circle`, `Text` — mirrors `client/daemon_client.py`'s own
`_marker_to_proto`/`_path_shape_to_proto`/etc. pattern, but at the
Python-value level, not proto), then the five `draw_*` tools as thin
callers of those helpers plus `put_annotation`, then
`remove_annotation`/`clear_layer`/`list_annotations` last.

**Files to create**: `src/aprilcam/mcp/tools/annotations.py`.

**Files to modify**: `src/aprilcam/mcp/server.py` (register this
tool group).

**Documentation updates**: none beyond docstrings.
