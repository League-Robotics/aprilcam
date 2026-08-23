---
id: '018'
title: Full dress rehearsal and final traceability fill
status: open
use-cases: [UC-10, UC-22, UC-29, UC-09]
depends-on: ['008', '017']
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

- [ ] `scripts/test-gate.sh`, then `scripts/test-hardware.sh`, then
      `scripts/test-acceptance.sh` all run back to back in one session
      and all pass.
- [ ] A sha256 snapshot of the real `config_dir`/`state_dir` is taken
      immediately before `test-gate.sh` starts and compared against a
      second snapshot immediately after `test-acceptance.sh` ends —
      byte-identical, including specifically
      `arducam-ov9782-usb-camera/config.json`'s `camera_position` field —
      recorded as an explicit, reviewable diff-or-empty artifact (e.g. a
      log file or terminal output captured in the ticket's
      implementation notes), not just asserted in prose.
- [ ] `get_use_case_coverage()` shows at least one named test for every
      UC in the sprint's Success Criteria list (daemon 01, 03, 04, 06,
      09, 10, 11, 12, 26, 28; CLI 13, 14, 15, 18, 19, 20, 22, 25, 29) —
      any UC still uncovered after this ticket is named explicitly in
      the ticket's implementation notes as a deliberate, reported gap,
      not silently left empty.
- [ ] `docs/design/use-cases/daemon.md` and `docs/design/use-cases/cli.md`
      Tests columns are updated with backticked pytest node IDs for
      every test landed across this sprint (tickets 001-017), not only
      the ones ticket 008 already filled for L0.
- [ ] The dress rehearsal's pass/fail/skip counts and timing are recorded
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
