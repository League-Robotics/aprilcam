---
id: '001'
title: Add pillow>=10.0 dependency to pyproject.toml
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: plan-aprilcam-view-positional-argument-tkinter-gui.md
completes_issue: false
---

# Add pillow>=10.0 dependency to pyproject.toml

## Description

`view_cli.py` will use `PIL.Image` and `PIL.ImageTk` (from Pillow) to convert
OpenCV BGR numpy arrays into `ImageTk.PhotoImage` objects for display in the
tkinter canvas. Pillow must be declared as a runtime dependency in
`pyproject.toml` so that `uv sync` and `pipx install` automatically install it.

## Acceptance Criteria

- [x] `pillow>=10.0` appears in `[project] dependencies` in `pyproject.toml`.
- [x] `uv sync` completes without errors after the change.
- [x] `python -c "from PIL import Image, ImageTk"` succeeds in the project
      virtualenv.
- [x] `uv run pytest` passes with no regressions.

## Implementation Plan

### Approach

Add a single line to `pyproject.toml`'s `[project] dependencies` list. The
line should read `"pillow>=10.0"`. No other file changes are required in this
ticket.

### Files to Modify

| File | Change |
|------|--------|
| `pyproject.toml` | Add `"pillow>=10.0"` to `[project] dependencies` |

### Testing Plan

- Run `uv sync` to verify the dependency resolves.
- Run `python -c "from PIL import Image, ImageTk"` inside the venv to confirm
  Pillow is importable.
- Run `uv run pytest` to confirm no regressions in existing test suite.

### Documentation Updates

None required. The dependency is self-explanatory in `pyproject.toml`.
