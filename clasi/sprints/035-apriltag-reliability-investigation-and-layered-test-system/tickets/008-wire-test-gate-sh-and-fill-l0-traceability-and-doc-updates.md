---
id: 008
title: Wire test-gate.sh and fill L0 traceability and doc updates
status: in-progress
use-cases:
- UC-10
depends-on:
- '006'
- '007'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire test-gate.sh and fill L0 traceability and doc updates

## Description

Closes out ticket-group 2 (L0): creates `scripts/test-gate.sh` as the
canonical close-sprint wrapper (`-m "not camera and not integration"`,
selecting the existing suite union L0 with zero hardware tests), updates
`docs/test-plans/camera-image-matrix.md` to describe `tests/image_matrix/`
as a package (correcting its "single module" framing, per that doc's own
`Remaining work` note), and fills traceability rows in
`docs/design/use-cases/{daemon,cli}.md` for every use case L0 now covers.

## Acceptance Criteria

- [ ] `scripts/test-gate.sh` exists, runs
      `uv run pytest -m "not camera and not integration"`, and exits
      non-zero on any failure.
- [ ] Running `scripts/test-gate.sh` selects the pre-existing suite union
      `tests/image_matrix/` and **zero** `camera`/`integration`-marked
      tests — verified by inspecting the collected test count/IDs, not
      just "it ran."
- [ ] Renaming `tests/images/` and re-running `scripts/test-gate.sh`
      still exits 0 (the gate-invariance check from the source issue's
      Verification §3).
- [ ] `docs/test-plans/camera-image-matrix.md` is updated: its "Status"
      line and §1/§6 language describe `tests/image_matrix/` as a
      package with the modules landed in tickets 005-007, replacing the
      original single-module (`tests/test_image_matrix.py`) framing.
- [ ] `docs/design/use-cases/{daemon,cli}.md` Tests columns are filled,
      with backticked pytest node IDs, for every use case an L0 test now
      exercises (at minimum UC-09, UC-10, UC-26 for tag queries; UC-04
      for calibration-adjacent world-accuracy; UC-06 for frame-level
      geometry) — cross-checked against `get_use_case_coverage()`.
- [ ] `get_use_case_coverage()` shows non-empty test references for
      every UC this ticket claims, run as a verification step, not just
      asserted in this ticket's description.

## Implementation Plan

**Approach**: This ticket is glue and documentation, not new test logic
— it wires what tickets 003-007 already built into one canonical command
and records which test proves which use case, closing the traceability
gap the sprint's Problem section names.

**Files to create/modify**:
- `scripts/test-gate.sh` (new)
- `docs/test-plans/camera-image-matrix.md` (status/package framing
  update)
- `docs/design/use-cases/daemon.md`, `docs/design/use-cases/cli.md`
  (Tests columns for L0-covered use cases)

**Testing plan**:
- Run `scripts/test-gate.sh` twice: once normally, once with
  `tests/images/` renamed, confirming green both times per the
  gate-invariance requirement.
- **Verification command**: `bash scripts/test-gate.sh`

**Documentation updates**:
- As listed under Files above — this ticket's entire deliverable is
  documentation plus one shell script.
