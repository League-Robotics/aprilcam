---
id: '003'
title: 'CLI: annotated view as default, --bare flag, doc touch-ups'
status: open
use-cases:
- SUC-003
depends-on:
- '002'
github-issue: ''
issue: playfield-view-should-draw-tag-ids-hats-and-a-tag-sidebar-like-v1.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI: annotated view as default, --bare flag, doc touch-ups

## Description

Wire ticket 002's `run_playfield_view(..., bare=)` up to `aprilcam
playfield view`'s own argv surface, and close out the sprint's
documentation touch-ups.

**`cli/playfield.py`'s `add_subparser`** (`p_view`, `:646-657`): add a
new flag, alongside the existing `--camera`/`-d`/`--all`:

```python
p_view.add_argument(
    "--bare",
    action="store_true",
    help="show the deskewed stream unmodified -- no tag overlay, no "
    "sidebar (default: the annotated view with the tag overlay and "
    "sidebar)",
)
```

**`cmd_view`** (`:273-337`): pass the flag through to `run_view`:

```python
try:
    run_view(camera, daemon, bare=args.bare)
except ...
```

`run_view`'s injected-callable type hint (`Callable[[str, Daemon],
None]`) widens to accept the new keyword (e.g. `Callable[..., None]`) —
match whatever shape ticket 002 settled on for `window_factory`'s
widened type, for consistency within the file.

No other change to `cmd_view`: camera resolution
(`resolve_playfield_camera`), the up-front `--all` refusal, and the
`NoDisplayAvailable`/`NotCalibrated` message wiring are all unaffected
by this flag — `--bare` only changes what `run_view` draws once a
window is already open, not whether/how the camera or connection are
resolved.

**Module docstring**: `cli/playfield.py`'s own docstring (`cmd_view`'s
section, `:42-55`) currently says the view "shows the deskewed,
calibrated field with detected tags drawn — ... `client/renderer.py`'s
tag boundaries and 'hats'." Update it to name the new default (overlay +
sidebar) and the `--bare` escape hatch explicitly, and fix the "poll-
driven window" phrasing this docstring and `view/viewer.py`'s own module
docstring both flag as stale (`view/DESIGN.md`'s Open Questions section
names this exact drift) — `run_playfield_view` streams, it does not
poll, since ticket 027-007.

**`docs/design/use-cases/cli.md`**: UC-18's "Open questions" paragraph
(`:483-489`) currently asks "which layers are on by default." Update it
to record this sprint's resolution: tag IDs, world coordinates (via the
crosshair), and orientation (via the hat) are on by default in the
annotated view; the sidebar is also on by default; `--bare` is the new
opt-out. Note explicitly that velocity **vectors** (a drawn arrow) are
still out of scope — only the sidebar's velocity *columns* are in scope
this sprint — so a future reader doesn't conflate the two. Add a short
UC-18 scenario line (or extend S-1) naming `--bare` as the undecorated
alternative, mirroring how UC-16.S-3 names `camera view [RAW]`'s role.

## Acceptance Criteria

- [ ] `aprilcam playfield view` (no flags) opens the annotated view
      (overlay + sidebar) by default.
- [ ] `aprilcam playfield view --bare` opens the undecorated view (no
      overlay, no sidebar).
- [ ] `--camera`, `-d`, `--all`-refusal, `NoDisplayAvailable`, and
      `NotCalibrated` message behavior are all unchanged with and
      without `--bare`.
- [ ] `cli/playfield.py`'s module docstring and `view/viewer.py`'s
      "poll-driven" wording are corrected to describe streaming
      accurately (both currently drift from the ticket-027-007 reality,
      per `view/DESIGN.md`'s own Open Questions note).
- [ ] `docs/design/use-cases/cli.md` UC-18's open question about default
      layers is resolved in the document, and `--bare` is named as the
      undecorated alternative.
- [ ] `uv run mypy` clean.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/cli/test_playfield_cli.py -v`
- **Tests to update**: the `run_view` injection tests around `:576-730`
  (camera resolution, `--all` refusal, `NoDisplayAvailable`/
  `NotCalibrated` messages) currently assert `run_view` is called with
  `(camera, connected)` — update the fake `run_view` signatures/
  assertions to also accept and record `bare=` where relevant, without
  changing what each test is actually checking. The parser tests around
  `:1029-1054` gain a case for `--bare`'s presence/default.
- **New tests to write**: `cmd_view` without `--bare` calls `run_view`
  with `bare=False` (or the parser default); `cmd_view --bare` calls it
  with `bare=True`; `--bare` combined with `--all` still hits the
  existing up-front `--all` refusal (unaffected by the new flag, but
  worth one explicit regression case).
- **Verification command**: `uv run pytest
  tests/aprilcam/cli/test_playfield_cli.py -v && uv run mypy`

**Documentation updates**: `cli/playfield.py` module docstring,
`docs/design/use-cases/cli.md` (UC-18), both as described above. This
is the ticket that closes out the sprint's documentation debt — confirm
`src/aprilcam/client/DESIGN.md` and `src/aprilcam/view/DESIGN.md` (from
tickets 001/002) are both actually updated on disk before this ticket is
marked done, since this is the last ticket in the sprint.
