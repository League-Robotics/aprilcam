---
id: '002'
title: Delete resolve_host_letter_number and collapse view's code resolution to one
  stage
status: open
use-cases:
- SUC-001
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: aprilcam-view-cannot-parse-host-qualified-camera-codes.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Delete resolve_host_letter_number and collapse view's code resolution to one stage

## Description

This is the core fix. Deletes the letter+digit (`B6`-style) camera-code
parser (`cli/_daemon.py:resolve_host_letter_number`) and the resolution
block in `cli/view_cli.py` that calls it, rather than patching its bugs in
place. `resolve_camera_code` (letter+letter, already correct — see ticket
001 and `architecture-update.md`) becomes the sole camera-code resolver for
`view`, matching what `tags_cli.py` already does.

This directly fixes the reported bug: `resolve_host_letter_number` never
validated the camera exists on the resolved host, never translated the
store's `num` to the daemon's `enum`, and had a local-kind shortcut that
returned `(number, None)` without reconnecting — the exact mechanism of
"no camera numbered 6 on the daemon" when host B's camera 6 was intended.
It also ran *before* the correct `resolve_camera_code` resolution in
`view_cli.py` (line ~427 vs ~444), shadowing it for any argument shaped
like `[A-Za-z]\d+`. Deleting it removes the shadowing and the bug in one
step.

See `architecture-update.md` §2-3 and "Design Rationale: delete
`resolve_host_letter_number` rather than fix its bugs in place" for the
full justification of deletion over in-place repair.

## Acceptance Criteria

- [ ] `cli/_daemon.py:resolve_host_letter_number` (currently lines
      ~192-251) is deleted entirely.
- [ ] `cli/view_cli.py`'s letter+digit resolution block (currently lines
      ~423-441 — the `from aprilcam.cli._daemon import
      resolve_host_letter_number as _rhln` import and its reconnect
      branch) is deleted.
- [ ] `view`'s camera-argument resolution is now two stages: (1)
      `resolve_camera_code` (letter+letter code), (2) the existing bare
      int/name fallback (lines ~463-491, unchanged). The three-stage
      resolution (letter+digit → letter+letter → fallback) no longer
      exists.
- [ ] `aprilcam view <code>` succeeds for every valid letter+letter code
      (local single-letter and remote two-letter), reconnecting to the
      correct remote daemon when the code addresses a non-local host —
      verified against a populated host store (test double or fixture, not
      live hardware).
- [ ] A remote host code always triggers a reconnect via
      `connect_from_args` before `open_camera` is called — no code path
      remains where a genuine remote code silently queries the local
      daemon.
- [ ] `cli/tags_cli.py` is confirmed unchanged and still correct: it
      already calls `resolve_camera_code` exclusively (line ~93). Verify
      this by inspection/test — no code edit expected here, but note in the
      ticket's completion if any drift is found.
- [ ] `grep -rn resolve_host_letter_number src/ tests/` returns no matches
      in `src/` (test file cleanup of `TestResolveHostLetterNumber` is
      ticket 004's responsibility, not this ticket's — do not delete those
      tests here if it would leave `view_cli`/`_daemon` tests failing to
      import; coordinate so this ticket doesn't break the tree given 004
      hasn't run yet — see Testing below).

## Implementation Plan

**Approach**: Delete the dead/buggy code path; do not attempt to preserve
or fix its behavior. Deletion order matters for import safety: since ticket
004 (not yet executed when this ticket runs) still contains
`TestResolveHostLetterNumber` referencing the soon-to-be-deleted function,
this ticket must either (a) also remove that test class as part of this
ticket's own scope to keep the tree green after this commit, or (b) leave a
minimal stub. Given tickets execute serially and each must leave the tree
in a working, test-passing state, **this ticket removes
`TestResolveHostLetterNumber` from `tests/test_host_codes.py` as part of
its own change** (ticket 004 then adds the *new* round-trip tests; it does
not need to re-remove anything).

**Files to create/modify**:
- `src/aprilcam/cli/_daemon.py` — delete `resolve_host_letter_number`
  function in full.
- `src/aprilcam/cli/view_cli.py` — delete the `<host-letter><number>` code
  resolution block (the `_rhln`/`resolve_host_letter_number` import and its
  `if _hl is not None:` branch); confirm the subsequent `resolve_camera_code`
  block (`_resolve_code`) and the bare-int/name fallback still read
  correctly with the deleted block removed (the `camera_arg` variable flows
  straight from `args.camera` into `_resolve_code` now).
- `tests/test_host_codes.py` — remove `TestResolveHostLetterNumber`
  (currently lines ~441-473), including `test_local_code_no_reconnect`
  (which documents the bug this ticket fixes).
- `src/aprilcam/cli/tags_cli.py` — no code change expected; read and
  confirm during implementation.

**Testing plan**:
- Add/extend a `view_cli` resolution test (fixture-based: fake `DaemonControl`
  + populated host store) asserting a remote letter+letter code reconnects
  and opens the right camera enum.
- Run `uv run pytest tests/test_host_codes.py tests/ -k "view or daemon or
  host_code"` to confirm the deletion doesn't break other tests.
- Run the full suite (`uv run pytest`) — must be green before ticket 002 is
  considered done, since ticket 003/004 build on this.

**Documentation updates**: None — `docs/wiki/daemon.md` already documents
the letter+letter scheme as canonical (verified during sprint planning); no
change needed there.
