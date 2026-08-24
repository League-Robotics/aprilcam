---
id: 018
title: Full dress rehearsal and final traceability fill
status: done
use-cases:
- UC-10
- UC-22
- UC-29
- UC-09
depends-on:
- 008
- '017'
- 019
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Full dress rehearsal and final traceability fill

## Description

The sprint's closing ticket: runs `scripts/test-gate.sh` →
`scripts/test-hardware.sh` → `scripts/test-acceptance.sh` back to back,
in one session, proving the whole three-layer stack works together —
not just that each layer passes in isolation. Proves, via ticket 014's
no-write guard's sha256 sweep spanning the *entire* rehearsal (not just
the acceptance script's own internal window), that the real
`~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json`
`camera_position` — and every other real config/state file — is
byte-identical before the gate run starts and after the acceptance run
ends. Also performs the final traceability fill: every remaining named
use case from the sprint's Success Criteria (daemon 01, 03, 04, 06, 09,
10, 11, 12, 26, 28; CLI 13, 14, 15, 18, 19, 20, 22, 25, 29) gets at least
one named test in `docs/design/use-cases/{daemon,cli}.md`'s Tests
columns.

## Acceptance Criteria

- [x] `scripts/test-gate.sh`, then `scripts/test-hardware.sh`, then
      `scripts/test-acceptance.sh` all run back to back in one session
      and all pass.
- [x] A sha256 snapshot of the real `config_dir`/`state_dir` is taken
      immediately before `test-gate.sh` starts and compared against a
      second snapshot immediately after `test-acceptance.sh` ends —
      byte-identical, including specifically
      `arducam-ov9782-usb-camera/config.json`'s `camera_position` field —
      recorded as an explicit, reviewable diff-or-empty artifact (e.g. a
      log file or terminal output captured in the ticket's
      implementation notes), not just asserted in prose.
- [x] `get_use_case_coverage()` shows at least one named test for every
      UC in the sprint's Success Criteria list (daemon 01, 03, 04, 06,
      09, 10, 11, 12, 26, 28; CLI 13, 14, 15, 18, 19, 20, 22, 25, 29) —
      any UC still uncovered after this ticket is named explicitly in
      the ticket's implementation notes as a deliberate, reported gap,
      not silently left empty. **Note:** `get_use_case_coverage()`
      itself was NOT used — it is broken (confirmed by ticket 008: it
      always returns `{"total_use_cases": 0}` because it reads an
      unpopulated `clasi/usecases.md` index, never
      `docs/design/use-cases/*.md`). Ticket 008's stronger method was
      used instead: every node ID written into a Tests column was
      confirmed to resolve via
      `uv run pytest --collect-only -q "<node id>"`. All 10 daemon UCs
      and all 9 CLI UCs in the list now have at least one named,
      collect-verified test.
- [x] `docs/design/use-cases/daemon.md` and `docs/design/use-cases/cli.md`
      Tests columns are updated with backticked pytest node IDs for
      every test landed across this sprint (tickets 001-017), not only
      the ones ticket 008 already filled for L0.
- [x] The dress rehearsal's pass/fail/skip counts and timing are recorded
      (e.g. in the sprint's own implementation notes or a short summary
      file) so a future sprint has a baseline to compare against.

## Implementation Plan

**Approach**: This ticket runs last, after every other ticket in the
sprint is done, since it depends on the complete set of tests existing
and passing individually first. The sha256 sweep should reuse ticket
014's no-write guard machinery directly (construct it once, snapshot
before `test-gate.sh`, let `test-acceptance.sh`'s own internal guard run
as normal, then take the final snapshot after everything completes) —
not a second, independently-written hashing implementation.

**Files to modify**:
- `docs/design/use-cases/daemon.md`, `docs/design/use-cases/cli.md` —
  final Tests-column fill.
- No new test code — this ticket verifies the existing suite, it does
  not add new tests.

**Testing plan**:
- The dress rehearsal itself IS the testing plan for this ticket.
- **Verification command**: `bash scripts/test-gate.sh && bash scripts/test-hardware.sh && bash scripts/test-acceptance.sh`

**Documentation updates**:
- `docs/design/use-cases/{daemon,cli}.md` Tests columns, as above.
- A short dress-rehearsal summary (pass/fail/skip counts, timing,
  no-write-guard result) recorded for the sprint-close report.

## Completion Notes

**Method for the sha256 sweep.** Reused ticket 014's `hash_tree()`
directly from `tests/acceptance/conftest.py` (no second hashing
implementation) via a small standalone script
(`snapshot_tree.py`, run with `uv run python`) that constructs the real
`aprilcam.config.directories.Directories()` (no env override — confirmed
`APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` unset for the whole session)
and writes a JSON snapshot of every file's sha256 under both
`config_dir` and `state_dir`. Snapshot taken immediately before
`test-gate.sh` started, second snapshot taken immediately after
`test-acceptance.sh` finished — spanning the entire three-script
rehearsal, not just `test-acceptance.sh`'s own internal window.

**sha256 proof — full tree, byte-identical.**

```
config_dir: /Users/eric/.config/aprilcam (source=fallback)
state_dir:  /Users/eric/.local/share/aprilcam (source=fallback)
files hashed before: 30, after: 30
diff count: 0

camera_position config.json sha256 before: 15dd06a2ea5aec1352c5bb8ff018207b3c5828ac31ca2d78f5a9f325518520be
camera_position config.json sha256 after:  15dd06a2ea5aec1352c5bb8ff018207b3c5828ac31ca2d78f5a9f325518520be
BYTE-IDENTICAL: True
FULL TREE BYTE-IDENTICAL (no diffs): True
```

`arducam-ov9782-usb-camera/config.json`'s `camera_position` was read
directly both times and matched the ticket-specified sacred value
exactly: `height_cm: 128.22990496814816, nadir_x_cm:
3.0570142285481654, nadir_y_cm: -2.798700632324644`. Zero of the 30
files under either real root changed — not just the camera config, the
whole tree. Full artifact:
`.clasi/log/dress-rehearsal-nowrite-diff.txt` (also
`dress-rehearsal-snapshot-before.json` / `-after.json` alongside it).

**Dress rehearsal — measured, this session, back to back, real
terminal-equivalent agent shell (per the layer's own caveats below).**

| Script | Result | Counts | Time | Exit |
|---|---|---|---|---|
| `scripts/test-gate.sh` | PASS | 1645 passed, 23 skipped, 101 deselected | 117–119s | 0 |
| `scripts/test-hardware.sh` | PASS | 27 passed, 54 skipped, 1687 deselected, 1 xfailed | 236s (3:56) | 0 |
| `scripts/test-acceptance.sh` | PASS | 17 passed, 2 skipped, 1750 deselected | 22.7s | 0 |

Ran twice for `test-gate.sh` (once timed, once to independently confirm
exit code) — both runs identical: 1645/23/101, exit 0. The
`test-gate.sh` deselected count (101) differs slightly from the
handoff note's stated baseline (104) — collection totals drift by a
few items run-to-run as other concurrent tickets land marker changes;
not investigated further since the pass/skip counts that matter (1645
passed, 23 skipped, 0 failed) matched exactly.

`test-hardware.sh` beat its stated baseline (27 passed/54 skipped vs.
the handoff note's 24 passed/60 skipped) — this session's shell had
working camera enumeration+open beyond what the note's "no TCC grant"
caveat predicted for a pure agent shell. `test-acceptance.sh` also beat
its baseline (17 passed/2 skipped vs. 14/5) — includes the 7
`test_no_write_guard.py` self-tests (always run, unit-tests the guard
itself against a fake tree) plus the read-only `test_inventory.py`
suite against the real daemon (10 tests, all passed, including
`test_where_surveyed_apriltag` and `test_mount_z_register_moves_per_formula_then_unregister_restores`).
Only 2 skips, both `test_elevated_locate.py` (opt-in,
`APRILCAM_TEST_ELEVATED=1` not set, correctly not attempted).

**What this sprint has proven vs. what remains unproven — stated
plainly, per the ticket's own instruction.**

1. **L0 has never actually run.** `tests/images/field-state.json`'s
   `fs-20260823` entry is still `status: "unverified"` (confirmed —
   `git diff tests/images/field-state.json` is empty, untouched this
   session). Every L0 test under `tests/image_matrix/` therefore still
   skips in `test-gate.sh` (visible above as `s` marks, not `.`).
   Confirming the field state to flip it to `confirmed` is a
   stakeholder action this ticket does **not** take. (A prior ticket's
   throwaway flip measured 175 passed/48 skipped under `confirmed` —
   not re-verified this session, per the hard rule against flipping
   it.)
2. **Mutating hardware tests still never execute in an agent session.**
   `test-hardware.sh`'s 54 skips are dominated by tests that calibrate,
   push config, or need a private daemon this shell cannot open a
   camera for — see the script's own "misleading symptom" preflight
   note. What *does* execute and pass: read-only queries against the
   operator's real, already-running daemon (`test_public_api_system.py`
   and friends), camera-attached-but-not-open enumeration checks, and
   the full acceptance layer's read-only inventory.

**Per-layer executing-vs-skipping, quantified.**

| Layer | Executing (passed+xfailed) | Skipping | Deselected (out of scope for this layer) |
|---|---|---|---|
| L0 gate (`test-gate.sh`) | 1645 | 23 (L0 image_matrix skips, field-state unverified) | 101 |
| L1 hardware (`test-hardware.sh`) | 28 (27 passed + 1 xfailed) | 54 | 1687 |
| L2 acceptance (`test-acceptance.sh`) | 17 | 2 (both opt-in elevated coverage) | 1750 |

**Traceability — node IDs added.** 21 new backticked pytest node IDs
added across `docs/design/use-cases/daemon.md` (UC-01, 03, 04, 06, 09,
10, 11, 12, 26, 28 — 04/06/09/10/26 got additions alongside ticket
008's existing L0 entries; 01/03/11/12/28 were previously `—` and are
now filled) and `docs/design/use-cases/cli.md` (UC-13, 14, 15, 18, 19,
20, 22, 25, 29 — all 9 were previously `—`, all now filled). Every node
ID (56 total across both files, including ticket 008's pre-existing
ones) was confirmed to resolve via a single batched
`uv run pytest --collect-only -q <node id> ...` — 62 tests collected
(parametrized L0 tests expand to multiple ids per base node), zero
collection errors. Both traceability preambles were updated to name the
now-covered UC list and explicitly name the UCs still left `—`
(daemon: UC-02, 05, 07, 08, 27; CLI: UC-16, 17, 21, 23, 24) as
deliberate, reported gaps outside sprint 035's Success Criteria list —
not silently empty.

**Deliberate scope note.** `get_use_case_coverage()` was not called at
all in this ticket's verification — per the ticket's own instruction
and ticket 008's prior finding, it is confirmed broken
(`{"total_use_cases": 0}`, reads an unpopulated `clasi/usecases.md`
index). This is a pre-existing, already-filed issue, not something
this ticket attempts to fix.
