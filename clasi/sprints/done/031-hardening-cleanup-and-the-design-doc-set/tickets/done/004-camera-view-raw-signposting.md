---
id: '004'
title: camera view RAW signposting
status: done
use-cases:
- SUC-004
depends-on: []
github-issue: ''
issue: hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# camera view RAW signposting

## Description

UC-16's "raw means raw" is a genuine, deliberate diagnostic guarantee —
`camera view` shows exactly what the camera sees, no deskew or
homography applied, which is how an operator tells a camera problem
from a geometry problem apart from `playfield view`. The stakeholder
hit this in practice, expecting deskew from `camera view` and not
getting it. The behavior is correct; only the signposting is missing.

Today, `view/viewer.py`'s `run_raw_view` sets the window title to
`f"aprilcam camera view -- {camera}"` — no mention of raw-ness. `cli/
camera.py`'s `cmd_view` prints no startup line at all before the window
opens. Fix both: the window title states RAW explicitly, and the CLI
prints a startup line naming the view raw and pointing at `playfield
view` as the deskewed alternative.

**This ticket changes signposting only.** `run_raw_view`'s actual image
content and behavior must remain byte-for-byte unchanged — per the
umbrella issue's explicit "keep the behaviours" instruction, and
because UC-16's raw-means-raw guarantee is the reason this command
exists at all.

## Acceptance Criteria

- [x] `view/viewer.py`'s `run_raw_view` sets the window title to
      include an explicit RAW marker (e.g. `"aprilcam camera view [RAW]
      -- {camera}"` or equivalent — exact wording at implementer's
      discretion, but "RAW" must appear).
- [x] `cli/camera.py`'s `cmd_view` prints a startup line, before the
      window opens, stating the view is raw (no deskew/homography) and
      naming `playfield view` as the deskewed alternative.
- [x] `run_raw_view`'s frame content, subscription (`stream_images(camera,
      deskewed=False)`), and all other behavior are unchanged — a
      diff-level review confirms no logic changes beyond the title
      string and the new CLI print statement.
- [x] Existing tests for `cmd_view`/`run_raw_view` (headless-display
      failure path, UC-16.S-3) continue to pass unmodified in behavior.
- [x] `uv run mypy` clean.

## Implementation Plan

**Approach**: two small, independent edits — a string change in
`view/viewer.py` and one new `print()` (or logger call, matching this
module's existing convention) in `cli/camera.py`'s `cmd_view`, placed
before the `discovery_factory().connect(...)` call so the signposting
appears even if the connection subsequently fails (an operator who
sees the raw-ness warning and then hits a connection error still saw
the warning).

**Files**:
- `src/aprilcam/view/viewer.py` — `run_raw_view`'s window-title string.
- `src/aprilcam/cli/camera.py` — `cmd_view`'s startup output.

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/view/
  test_viewer.py tests/aprilcam/cli/test_camera_cli.py -v`.
- **New tests to write**: assert `run_raw_view`'s constructed window
  title contains a RAW marker (via the existing `window_factory`
  injection seam — no real display needed); assert `cmd_view`'s stdout
  output (captured via `capsys` or the existing CLI test harness
  pattern) includes the raw-ness line and names `playfield view` before
  attempting any daemon connection.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none required in `docs/design/` proper — the
`view/`/`cli/` subsystem `DESIGN.md` files do not exist yet; ticket 008
documents this behavior when it bootstraps them.
