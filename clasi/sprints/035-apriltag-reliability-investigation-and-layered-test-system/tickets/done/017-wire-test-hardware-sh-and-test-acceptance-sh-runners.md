---
id: '017'
title: Wire test-hardware.sh and test-acceptance.sh runners
status: done
use-cases:
- UC-29
depends-on:
- '011'
- '012'
- '013'
- '016'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire test-hardware.sh and test-acceptance.sh runners

## Description

Closes out ticket-groups 3 and 4 with their canonical runner scripts:
`scripts/test-hardware.sh` (`-m "(camera or integration) and not
acceptance"`, with a preflight checklist echo — daemon/views status,
which cameras should be attached) and `scripts/test-acceptance.sh`
(runs the real acceptance suite, tees output to a timestamped file).
Adds a README/docs runner section describing when to use each of the
three scripts (`test-gate.sh`, `test-hardware.sh`, `test-acceptance.sh`).

## Acceptance Criteria

- [x] `scripts/test-hardware.sh` runs
      `uv run pytest -m "(camera or integration) and not acceptance"`
      and, before running, echoes a preflight checklist (daemon stopped
      or not as appropriate, views closed, which cameras should be
      attached) matching the sprint's documented capture/test
      logistics.
- [x] Running `scripts/test-hardware.sh` collects L1's tests (tickets
      009, 011, 012, 013) and explicitly zero `acceptance`-marked tests
      — verified by inspecting the collected node IDs.
- [x] `scripts/test-acceptance.sh` runs the `acceptance`-marked suite
      and tees its output to a timestamped file under a documented
      location (e.g. `.clasi/log/acceptance-<timestamp>.log` or
      equivalent — pick one consistent with this project's existing log
      conventions).
- [x] Both scripts exit non-zero on any test failure.
- [x] A README or `docs/test-plans/` section (new or appended) explains
      the three-script hierarchy: `test-gate.sh` (always, no hardware),
      `test-hardware.sh` (rig required, no acceptance risk), and
      `test-acceptance.sh` (rig + real daemon, no-write-guarded).

## Implementation Plan

**Approach**: Thin shell wrappers only — no test logic of their own,
matching `scripts/test-gate.sh`'s own scope from ticket 008.

**Files to create/modify**:
- `scripts/test-hardware.sh` (new)
- `scripts/test-acceptance.sh` (new)
- A README or `docs/test-plans/` runner-hierarchy section (new or
  appended to an existing doc — author's judgment on the best home,
  consistent with this project's existing doc layout).

**Testing plan**:
- Run both scripts once against a real rig to confirm they select the
  intended test sets and exit codes behave correctly.
- **Verification command**: `bash scripts/test-hardware.sh && bash scripts/test-acceptance.sh`

**Documentation updates**:
- The runner-hierarchy section described above.
