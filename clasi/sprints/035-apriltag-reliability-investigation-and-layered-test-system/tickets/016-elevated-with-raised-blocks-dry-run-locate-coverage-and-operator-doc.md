---
id: '016'
title: Elevated with-raised-blocks dry-run locate coverage and operator doc
status: in-progress
use-cases:
- UC-29
depends-on:
- '015'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Elevated with-raised-blocks dry-run locate coverage and operator doc

## Description

The elevated ("with raised blocks") acceptance coverage, gated entirely
behind `APRILCAM_TEST_ELEVATED=1` (unset by default — this coverage does
not run in a normal acceptance pass). Runs
`camera locate <cam> --dry-run --reference TAG:SLUG:HEIGHT_CM ...`
against real, physically raised block references and asserts the solve
quality gates (RMS ≤ 1.0 cm, condition ≤ 447) — zero writes, per
`--dry-run`'s own contract, additionally covered by ticket 014's
no-write guard as a second, independent check.

Also writes `docs/test-plans/staged-elevated-locate.md`: which blocks,
which tags, their heights, the exact commands, the quality gates, and —
stated plainly — that a real, non-dry `camera locate` is a deliberate,
separate stakeholder action, never something a test performs.

## Acceptance Criteria

- [ ] This coverage does not run at all unless
      `APRILCAM_TEST_ELEVATED=1` is set — verified by a normal
      (unset-env) acceptance run collecting/skipping these tests, not
      just documented.
- [ ] `camera locate <cam> --dry-run --reference TAG:SLUG:HEIGHT_CM ...`
      runs against real elevated references and reports a full solve
      (nadir, height, RMS residual) with RMS ≤ 1.0 cm and condition
      ≤ 447.
- [ ] Zero writes occur — proven by ticket 014's no-write guard covering
      this test too (it is not exempted from the guard), not solely by
      `--dry-run`'s own contract.
- [ ] No test in this module ever calls a non-dry `locate` or
      `calibrate` — enforced by the same node-id allowlist mechanism
      from ticket 015, extended to cover this module.
- [ ] `docs/test-plans/staged-elevated-locate.md` documents: the exact
      blocks/tags/heights used, the exact commands run, the RMS/condition
      quality gates, and states explicitly that a real non-dry
      `camera locate` is a deliberate, separate stakeholder action never
      performed by any test in this suite.
- [ ] Without blocks raised (the default, non-elevated state), coverage
      for this geometry continues to rest on the existing unit/
      real-fixture regressions in
      `tests/aprilcam/calibration/test_geometry.py:485-887` — this
      ticket does not weaken or replace that coverage.

## Implementation Plan

**Approach**: Gate the entire module behind a `pytest.mark.skipif`
(or an equivalent collection-time guard) on `APRILCAM_TEST_ELEVATED`
being unset, so an ordinary acceptance run never attempts these
assertions even accidentally. Write the operator doc first (or
alongside) since the concrete blocks/tags/heights it names are what the
test itself needs to reference — keep the two in sync deliberately, not
by accident.

**Files to create**:
- `tests/acceptance/test_elevated_locate.py`
- `docs/test-plans/staged-elevated-locate.md`

**Testing plan**:
- Requires `APRILCAM_TEST_ELEVATED=1`, real raised blocks physically
  placed per the new operator doc, and a real camera.
- **Verification command**: `APRILCAM_TEST_ELEVATED=1 uv run pytest tests/acceptance/test_elevated_locate.py -m acceptance -v`
  — run deliberately by a human with blocks staged; not part of any
  automated per-ticket loop.

**Documentation updates**:
- `docs/test-plans/staged-elevated-locate.md` (new, as above).
