---
id: '002'
title: PlayfieldDefinition model, registry, and where-tool repoint
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-005
depends-on:
- '001'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# PlayfieldDefinition model, registry, and where-tool repoint

## Description

Introduces the `PlayfieldDefinition` dataclass and `PlayfieldDefinitionRegistry`
class (new module `src/aprilcam/core/playfield_def.py`). Repoints the `where`
tool and `playfield_query.load_playfield` to load from the new
`data/aprilcam/playfields/` directory, with a backward-compat fallback to the
old `data/aprilcam/playfield.json` for sessions where migration has not yet run.

Also adds the module-level `playfield_def_registry` singleton to `mcp_server.py`
and the startup-load call that populates it.

### PlayfieldDefinition

```python
@dataclass
class PlayfieldDefinition:
    name: str              # == filename stem; canonical reference id
    display_name: str      # human label; defaults to name if absent
    width_cm: float
    height_cm: float
    origin: str            # e.g. "apriltag-center-a1"
    april_tags: list[dict]
    aruco_tags: list[dict]
    rectangles: list[dict]
    dots: list[dict]

    @classmethod
    def load(cls, path: Path) -> "PlayfieldDefinition": ...

    def corner_aruco_ids(self) -> list[int]:
        """ArUco IDs of the four diagonal-cardinal markers (NW/NE/SE/SW)."""

    def corner_world_coords(self) -> list[tuple[float, float]]:
        """World (x,y) in cm for each corner ID, same order as corner_aruco_ids()."""
```

The `corner_aruco_ids()` helper filters `aruco_tags` for entries whose `cardinal`
is one of `northwest`, `northeast`, `southeast`, `southwest` and returns their
`id` values. `corner_world_coords()` returns the paired `(x, y)` values. For
`main-playfield.json` this yields IDs `[1, 3, 5, 7]` and coords
`[(-67, 44.65), (67, 44.65), (67, -44.65), (-67, -44.65)]`.

### PlayfieldDefinitionRegistry

```python
class PlayfieldDefinitionRegistry:
    def load_all(self, playfields_dir: Path) -> None: ...
    def get(self, name: str) -> PlayfieldDefinition: ...   # raises KeyError if not found
    def list(self) -> list[str]: ...
    def first(self) -> PlayfieldDefinition | None: ...     # convenience; None if empty
```

`load_all` scans `*.json` files in `playfields_dir`; skips silently on parse
errors. If `playfields_dir` does not exist, the registry loads empty without error.

### MCP server startup

In `src/aprilcam/server/mcp_server.py`:
- Add module-level instance: `playfield_def_registry = PlayfieldDefinitionRegistry()`
- In `main()` (after `Config.load()`), call:
  `playfield_def_registry.load_all(cfg.playfields_dir)`

### where tool / playfield_query repoint

In `src/aprilcam/core/playfield_query.py`:
- `default_playfield_path(data_dir)` — keep existing implementation (returns
  `data_dir / "playfield.json"`) as the fallback.
- Update the `where` MCP tool handler in `mcp_server.py` to load from the
  registry when available: call `playfield_def_registry.first()` and construct
  the features list from its marker lists. Fall back to `load_playfield(
  default_playfield_path(cfg.data_dir))` when the registry is empty. This
  keeps the `where` tool working during the migration window.

## Acceptance Criteria

- [x] `PlayfieldDefinition.load(path)` correctly parses `main-playfield.json`
      (after migration in ticket 003) and returns correct `name`, `width_cm`
      (134.3), `height_cm` (89.3).
- [x] `corner_aruco_ids()` returns `[1, 3, 5, 7]` for `main-playfield.json`.
- [x] `corner_world_coords()` returns the four diagonal-cardinal positions
      `(-67, 44.65)`, `(67, 44.65)`, `(67, -44.65)`, `(-67, -44.65)` in the
      same order as the IDs.
- [x] `PlayfieldDefinitionRegistry.load_all(dir)` silently tolerates a
      missing directory (registry stays empty).
- [x] `PlayfieldDefinitionRegistry.load_all(dir)` skips malformed JSON files
      without raising.
- [x] `playfield_def_registry` is a module-level singleton in `mcp_server.py`;
      `load_all` is called in `main()` before any tool invocation.
- [x] `where` tool continues to work when the old `playfield.json` path exists
      (fallback confirmed by existing test).
- [x] `where` tool uses the registry when it is populated (confirmed by a new
      test that patches the registry with a def loaded from a fixture file).

## Implementation Plan

### Approach

New module; additive changes to `mcp_server.py` and `playfield_query.py`. No
deletions. Test can run without a live migration (use fixture JSON files).

### Files to create

- `src/aprilcam/core/playfield_def.py`
  - `PlayfieldDefinition` dataclass with `load(path)` classmethod.
  - `PlayfieldDefinitionRegistry` class with `load_all`, `get`, `list`, `first`.
  - No I/O at attribute access; `load_all` is the only scan-and-load entry point.

### Files to modify

- `src/aprilcam/server/mcp_server.py`
  - Import `PlayfieldDefinitionRegistry` from `aprilcam.core.playfield_def`.
  - Add `playfield_def_registry = PlayfieldDefinitionRegistry()` near line 235
    with the other module-level singletons.
  - In `main()`, call `playfield_def_registry.load_all(cfg.playfields_dir)`.
  - In the `where` tool handler: prefer `playfield_def_registry.first()` over
    `load_playfield(default_playfield_path(...))` when registry is non-empty.

- `src/aprilcam/core/playfield_query.py`
  - No changes needed to `load_playfield` or `default_playfield_path` (they
    remain as the backward-compat fallback). The switch is done at the call site
    in `mcp_server.py`.

### Testing plan

Add `tests/test_playfield_def.py` (new file):
- `test_load_main_playfield(tmp_path)` — write a minimal fixture JSON (or copy
  from `data/aprilcam/playfields/main-playfield.json` after migration), load it,
  assert `name`, `width_cm`, `height_cm`.
- `test_corner_ids()` — assert `corner_aruco_ids()` returns `[1, 3, 5, 7]`.
- `test_corner_world_coords()` — assert paired world positions are correct.
- `test_registry_load_all(tmp_path)` — write two fixture JSON files, call
  `load_all`, assert `list()` returns both names.
- `test_registry_missing_dir(tmp_path)` — non-existent dir → empty registry,
  no exception.
- `test_registry_malformed_json(tmp_path)` — one bad file, one good file →
  registry has 1 entry, no exception.

Existing tests: run `uv run pytest` to verify `where` tool and `playfield_query`
tests still pass with the fallback path intact.

### Documentation updates

None for this ticket. ROBOT_API_GUIDE updated in ticket 007.
