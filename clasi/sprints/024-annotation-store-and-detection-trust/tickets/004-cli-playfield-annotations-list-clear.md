---
id: '004'
title: 'CLI: playfield annotations (list, --clear)'
status: done
use-cases:
- SUC-004
depends-on:
- '002'
github-issue: ''
issue: annotation-store-for-shared-spatial-state.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI: playfield annotations (list, --clear)

## Description

Add `aprilcam playfield annotations` to `cli/playfield.py`: bare form
lists the current annotation set for a playfield; `--clear [--layer
NAME]` clears one layer or everything. Both spellings are already
named in `cli.md`'s command surface (line 36-37) and its fan-out table
(line 67) — this ticket implements exactly what is already documented,
not a new design.

**Depends on ticket 002** — needs `GrpcDaemon.get_annotations`/
`.clear_annotations` to exist on the client `Daemon` interface.

**List/clear only — no authoring from the CLI.** Matches the issue's
explicit scope and `process.md`'s stated minimum ("at least list/clear
from the shell"). Do not add a `playfield annotations --put`/`--draw`
flag or similar — that is explicitly deferred (sprint.md Architecture,
resolved open question 7), not an oversight to "helpfully" close.

**Playfield resolution follows the established convention.** Like
`playfield image`/`playfield view`, this command names no camera and
resolves its playfield the same way — but note annotations are
**playfield-addressed directly** (not camera-addressed), so unlike
`playfield image`/`view` there is no camera to resolve via
`cli._common.resolve_playfield_camera` at all. The command instead
takes a playfield **name** argument directly (annotations are
addressed by playfield name in the daemon API — `get_annotations(
playfield: str, ...)` — there is no camera-to-playfield indirection to
resolve here, unlike the frame-fetching commands).

Builds:

- **`cli/playfield.py`** — new `annotations` subcommand:
  - `aprilcam playfield annotations <name> [-d HOST] [--all]` — prints
    the current set (one line per annotation: layer, id, shape kind,
    key fields). `--all` fans out (read-only, matches the existing
    table row), each row labelled by daemon.
  - `aprilcam playfield annotations <name> --clear [--layer NAME] [-d
    HOST]` — clears one layer or everything. `--all` is refused here
    (a write, matches the existing table's "any annotation write" row)
    — mirror `cmd_view`'s existing `--all` refusal message shape
    (`cli/playfield.py`'s `cmd_view`) for consistency.
  - Wire into `add_subparser` alongside `config`/`image`/`view`.

## Acceptance Criteria

- [x] `aprilcam playfield annotations main-playfield` prints the
      current set, matching exactly what `get_annotations` returns
      (the CLI invents no separate truth — same discipline as
      `playfield config --dump` printing the daemon's document
      verbatim).
- [x] `aprilcam playfield annotations main-playfield --clear` clears
      every layer on that playfield; `--clear --layer planner` clears
      only that layer, leaving others untouched.
- [x] `aprilcam playfield annotations main-playfield --all` fans out
      across discovered daemons, each row labelled — matches
      `camera list`'s existing fan-out labelling convention.
- [x] `aprilcam playfield annotations main-playfield --clear --all` is
      refused with an error naming the restriction (a write does not
      fan out), not a silent single-target fallback — mirror
      `cmd_view`'s refusal wording/shape.
- [x] An unknown playfield name produces the daemon's
      `UnknownPlayfield` error, surfaced via the existing
      `report_error` path (no CLI-invented error text).
- [x] `uv run mypy` clean.
- [x] Full suite green: `uv run pytest -m "not camera"`.

## Implementation Plan

**Approach**: mirror `cmd_config`'s `--dump` shape (a flag on the noun
that changes the RPC called, not a new subcommand-within-a-subcommand)
for the list/clear split, and mirror `cmd_view`'s `--all` refusal
handling for the write path.

**Files**:
- `src/aprilcam/cli/playfield.py` (extend: new `cmd_annotations`,
  wired in `add_subparser`)

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/
  test_playfield_cli.py tests/aprilcam/cli/test_cli_integration.py -v`,
  plus full headless suite.
- **New tests to write**: unit tests for `cmd_annotations` against a
  fake `Discovery`/`Daemon` (matching `test_playfield_cli.py`'s
  existing pattern for `cmd_config`/`cmd_image`) covering list,
  `--clear`, `--clear --layer`, `--all` fan-out, `--clear --all`
  refusal, and unknown-playfield error surfacing.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none — `cli.md` already documents this
command surface and fan-out row; no update needed unless the actual
implementation's argument shape diverges from what is already written
there (it should not).
