---
id: '006'
title: 'CLI: camera tags command'
status: open
use-cases: [SUC-004]
depends-on: ['005']
github-issue: ''
issue: tag-detection-and-tag-queries.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI: camera tags command

## Description

Adds `aprilcam camera tags <camera>` — this sprint's shipped command
spelling (sprint.md Architecture Decision 3). The literal `cli.md`
spelling, `aprilcam playfield tags` (client-side-resolved camera via
`CameraState.playfield` links), is **not** buildable this sprint —
playfields do not exist, so that link is always `None` and the
resolution would fail on every call. This ticket also updates
`cli.md` to record that decision, not just implement around it
silently.

## Acceptance Criteria

- [ ] `aprilcam camera tags <camera>` (`-d HOST` for remote) calls
      `get_tags` and prints every detected tag: family, id, and either
      a world position (calibrated — not reachable this sprint, but
      the code path exists for when it is) or, this sprint always, an
      explicit uncalibrated indicator plus pixel position. **Output
      never prints `0.0, 0.0` or any world-looking number for an
      uncalibrated record** — it prints the flag and pixel coordinates
      instead (mirrors UC-20.S-3's failure-mode requirement literally).
- [ ] Nothing detected prints an empty result (e.g. "no tags
      detected"), not an error or a stack trace.
- [ ] `--all` fans out, one row per daemon, each labelled — matching
      `camera list`/`camera probe`'s existing fan-out shape in
      `cli/camera.py` (reuse the `fan_out` helper in `cli/_common.py`
      rather than reimplementing it).
- [ ] An unknown or absent camera surfaces the typed error from
      tickets 002/005 (`UnknownCamera`/`CameraNotPresent`) via the
      existing `report_error` helper — no new error-handling pattern
      invented here.
- [ ] `docs/design/use-cases/cli.md` is updated:
  - The command-surface block: `aprilcam playfield tags` gains an
    inline note that it is deferred; `aprilcam camera tags <camera>`
    is added as what this sprint ships.
  - UC-20's main success path is amended to name `camera tags
    <camera>` as this sprint's shipped form, with a forward pointer to
    the deferred playfield-addressed spelling once playfields exist.
  - The fan-out table's `playfield tags` row becomes `camera tags`,
    joining `camera image`/`camera list`/`camera probe`'s existing
    read-only fan-out-eligible group.
- [ ] `uv run mypy` stays clean; `uv run pytest` stays green.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/ -v`,
  full suite, `uv run mypy`.
- **New tests to write**: a new `tests/aprilcam/cli/test_tags_cli.py`
  (or an addition to `test_camera_cli.py`, matching whichever module
  actually hosts the new command — see Implementation Plan) covering:
  the uncalibrated-flag-not-zeros output format; empty-result
  formatting; `--all` fan-out with per-daemon labels (following
  `test_camera_cli.py`'s existing `cmd_list`/`cmd_probe` test
  patterns and its `discovery_factory` injection convention); the
  unknown-camera and camera-not-present error paths.
- **Verification command**:
  `uv run pytest tests/aprilcam/cli/ -v && uv run mypy`

## Implementation Plan

### Approach

1. Decide module placement: either a new `cli/tags.py` (own
   subparser, own module — matches `cli/daemon.py`'s pattern of one
   module per top-level noun) or an addition inside `cli/camera.py`
   (since `tags` is a `camera`-addressed subcommand, following that
   module's existing `list`/`probe`/`image` subcommand grouping).
   Given `cli/camera.py`'s module docstring already frames it as
   "`aprilcam camera list|probe|image`," adding a fourth subcommand
   (`tags`) to the same module is the more consistent choice unless
   it grows unwieldy — prefer extending `cli/camera.py` over a new
   file, matching the existing one-module-per-noun shape.
2. Follow `cmd_image`'s existing shape closely: thin argv-to-client
   shell, `discovery_factory` injectable for tests, `report_error` on
   `AprilCamError`.
3. Output formatting: for each `TagRecord`, print family/id always;
   if `record.calibrated`, print world position/heading/speed; else
   print an explicit marker (e.g. `"UNCALIBRATED"`) followed by pixel
   position — never fall through to printing `None`-derived zeros.
4. Wire `--all` through the existing `fan_out` helper exactly as
   `cmd_list`/`cmd_probe` already do.
5. Add the `tags` subparser to `add_subparser` (or wherever the
   command tree is assembled — check `cli/__init__.py`'s dispatcher
   registration alongside `cli/camera.py`'s own `add_subparser`).
6. Edit `docs/design/use-cases/cli.md` per the acceptance criteria
   above — read the current command-surface block, UC-20, and the
   fan-out table sections in full before editing so the edit is
   surgical, not a rewrite.

### Files to create/modify

- `src/aprilcam/cli/camera.py` (modify — add `tags` subcommand; or a
  new `src/aprilcam/cli/tags.py` if that proves cleaner, wired into
  `src/aprilcam/cli/__init__.py`)
- `tests/aprilcam/cli/test_camera_cli.py` (modify) or
  `tests/aprilcam/cli/test_tags_cli.py` (new), matching the module
  placement chosen above
- `docs/design/use-cases/cli.md` (modify)

### Documentation updates

`docs/design/use-cases/cli.md` as detailed in Acceptance Criteria —
this ticket's documentation update **is** part of its acceptance
criteria, not a follow-on.
