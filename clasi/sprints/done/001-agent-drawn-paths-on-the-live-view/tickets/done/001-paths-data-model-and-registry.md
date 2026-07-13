---
id: '001'
title: Paths data model and registry
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-006
depends-on: []
github-issue: ''
issue: agent-drawn-paths-on-the-aprilcam-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# T001: Paths data model and registry

## Description

Create `src/aprilcam/server/paths.py` containing the complete path data model
and registry. This is pure Python — no OpenCV, no IPC, no MCP dependency.
It is the foundation all other tickets build on.

The symbol set has exactly 8 values including `"none"` (draws no marker at a
waypoint but lines still connect through it). The registry mirrors the
`FrameRegistry` pattern in `src/aprilcam/server/frame.py` (thread-safe,
monotonic `path_NNN` IDs, dict-backed).

## Acceptance Criteria

- [x] `Symbol` is a `Literal` type alias with exactly these 8 values:
      `"square"`, `"filled_square"`, `"circle"`, `"filled_circle"`,
      `"triangle"`, `"filled_triangle"`, `"x"`, `"none"`.
- [x] `RGB = Tuple[int, int, int]` type alias exists.
- [x] `Waypoint` is a frozen dataclass with fields `x: float`, `y: float`,
      `size_cm: float`, `symbol: Symbol`, `symbol_color: RGB`,
      `line_color: RGB`.
- [x] `Path` is a dataclass with `path_id: str`, `playfield_id: str`,
      `waypoints: List[Waypoint]`, and a `to_dict()` method returning a plain
      dict serializable by `json.dumps` (each waypoint as a dict with
      `symbol_color` and `line_color` as lists).
- [x] `PathRegistry` provides: `create(playfield_id, waypoints) -> Path`,
      `delete(path_id) -> Optional[Path]`, `get(path_id) -> Optional[Path]`,
      `list_for(playfield_id) -> List[Path]`,
      `clear_for(playfield_id) -> List[str]` (returns deleted path_ids).
- [x] IDs are monotonically increasing: `path_000`, `path_001`, ... (zero-padded
      to 3 digits, growing beyond 3 digits when counter exceeds 999).
- [x] All `PathRegistry` operations are protected by `threading.Lock`.
- [x] `to_dict()` output round-trips cleanly through `json.dumps` / `json.loads`.
- [x] Unit tests pass (see Testing Plan).

## Implementation Plan

### Approach

Model the file on `src/aprilcam/server/frame.py`. The `PathRegistry` has no
capacity limit and no eviction — paths accumulate until explicitly deleted.
Use a plain `dict[str, Path]` keyed by `path_id` plus a monotonic counter
protected by a single lock.

### Files to Create

**`src/aprilcam/server/paths.py`**

Structure outline (not prescriptive — implementer may adjust style):

```
from __future__ import annotations
import json, threading
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

Symbol = Literal[
    "square", "filled_square",
    "circle", "filled_circle",
    "triangle", "filled_triangle",
    "x",
    "none",
]
RGB = Tuple[int, int, int]

VALID_SYMBOLS = {"square", "filled_square", "circle", "filled_circle",
                 "triangle", "filled_triangle", "x", "none"}

@dataclass(frozen=True)
class Waypoint:
    x: float
    y: float
    size_cm: float
    symbol: Symbol
    symbol_color: RGB
    line_color: RGB

@dataclass
class Path:
    path_id: str
    playfield_id: str
    waypoints: List[Waypoint]

    def to_dict(self) -> dict: ...   # serialize to plain dict / JSON-safe

class PathRegistry:
    def __init__(self) -> None: ...
    def create(self, playfield_id: str, waypoints: List[Waypoint]) -> Path: ...
    def delete(self, path_id: str) -> Optional[Path]: ...
    def get(self, path_id: str) -> Optional[Path]: ...
    def list_for(self, playfield_id: str) -> List[Path]: ...
    def clear_for(self, playfield_id: str) -> List[str]: ...
```

Export `VALID_SYMBOLS` as a set constant for use by the validation logic in T002.

**`tests/test_paths.py`** — unit test module (new file).

### Files to Modify

None.

### Testing Plan

Write `tests/test_paths.py` covering:
- `test_create_returns_path_with_monotonic_id` — first create yields `path_000`.
- `test_ids_are_monotonic` — three creates yield `path_000`, `path_001`, `path_002`.
- `test_delete_known_id` — create then delete; return value is the path; `get` returns None afterward.
- `test_delete_unknown_id_returns_none` — delete a nonexistent id returns None.
- `test_list_for_filters_by_playfield` — create paths for two different playfield ids; `list_for` on each returns only that playfield's paths.
- `test_clear_for_removes_all_for_playfield` — create two paths for one playfield, clear; list is empty; returned ids match.
- `test_to_dict_round_trip` — `json.dumps(path.to_dict())` followed by `json.loads` reproduces all field values including nested waypoints.

Verification command: `uv run pytest tests/test_paths.py -v`

### Documentation Updates

Add a module-level docstring to `paths.py` describing its purpose and the
8-value symbol set. No other documentation changes needed.
