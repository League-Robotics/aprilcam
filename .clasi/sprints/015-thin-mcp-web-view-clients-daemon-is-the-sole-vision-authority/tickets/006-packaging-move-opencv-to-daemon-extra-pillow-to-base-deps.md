---
id: '006'
title: 'Packaging: move opencv to daemon extra, Pillow to base deps'
status: done
use-cases:
- SUC-006
depends-on:
- '003'
- '005'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Packaging: move opencv to daemon extra, Pillow to base deps

## Description

Update `pyproject.toml` to enforce the new dependency boundary:
- `opencv-contrib-python` moves to the `[daemon]` extra only.
- `pillow>=10.0` moves to the base `[project.dependencies]`.
- `DAEMON_COMMANDS` in `cli/__init__.py` shrinks to only those commands that
  genuinely require daemon-side extras.

This ticket depends on tickets 003 and 005 completing successfully — the code
must actually be opencv-free before the packaging boundary is enforced, otherwise
`mcp`, `web`, and `view` would fail at import time in a base install.

## Acceptance Criteria

- [x] `pyproject.toml`: `opencv-contrib-python` appears **only** under
  `[project.optional-dependencies.daemon]` — not in base `[project.dependencies]`
  and not in the `imaging` extra (or `imaging` extra is removed/emptied).
- [x] `pyproject.toml`: `pillow>=10.0` (or `Pillow>=10.0`) appears in
  `[project.dependencies]` (base deps).
- [x] `cli/__init__.py`: `DAEMON_COMMANDS` is `frozenset({"daemon", "taggen",
  "calibrate"})` — `mcp`, `web`, `view`, `cameras`, `tags` are removed.
- [x] `uv run pytest` green.
- [x] Verify the dependency change does not break the `uv` lockfile:
  run `uv lock --check` or `uv sync` and confirm no errors.

## Implementation Plan

### `pyproject.toml` changes

Current state (from inspection):
```toml
[project.optional-dependencies]
imaging = ["opencv-contrib-python>=4.8"]
daemon = [
    # ... other daemon deps ...
]
```
And Pillow is already in the `imaging` extra: `"pillow>=10.0"`.

Changes to make:
1. Remove `opencv-contrib-python>=4.8` from the `imaging` extra.
2. Add `opencv-contrib-python>=4.8` to the `daemon` extra (if not already there).
3. Move `pillow>=10.0` from `imaging` to `[project.dependencies]`.
4. If the `imaging` extra is now empty, remove it (or leave it empty with a comment).

Example final state:
```toml
[project.dependencies]
# ... existing base deps ...
"pillow>=10.0",

[project.optional-dependencies]
daemon = [
    "opencv-contrib-python>=4.8",
    # ... other daemon deps ...
]
```

### `cli/__init__.py` changes

Find the `DAEMON_COMMANDS` frozenset (~line 58):
```python
DAEMON_COMMANDS = frozenset(
    {"daemon", "mcp", "web", "taggen", "calibrate", "cameras", "tags", "view"}
)
```

Change to:
```python
DAEMON_COMMANDS = frozenset({"daemon", "taggen", "calibrate"})
```

The hint message (shown when a DAEMON_COMMAND is run without daemon extras) will
now only appear for `daemon`, `taggen`, and `calibrate` — the three commands that
actually import opencv or daemon-only dependencies.

### Verify no broken imports

After the changes, run:
```bash
uv sync --no-dev   # installs base deps only (no extras)
python -c "import aprilcam.server.mcp_server; print('mcp ok')"
python -c "import aprilcam.server.web_server; print('web ok')"
python -c "import aprilcam.cli.view_cli; print('view ok')"
```

If any of the above fail (ImportError for cv2), a previous ticket (003, 004, or 005)
left a cv2 import — fix it before closing this ticket.

### `uv lock` check

After editing `pyproject.toml`:
```bash
uv lock
uv run pytest
```

### Files to modify

- `pyproject.toml` — opencv to daemon extra, Pillow to base
- `src/aprilcam/cli/__init__.py` — `DAEMON_COMMANDS` reduced
