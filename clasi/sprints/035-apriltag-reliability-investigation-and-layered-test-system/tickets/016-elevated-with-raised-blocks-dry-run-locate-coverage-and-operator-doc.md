---
id: '016'
title: Elevated with-raised-blocks dry-run locate coverage and operator doc
status: done
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

- [x] This coverage does not run at all unless
      `APRILCAM_TEST_ELEVATED=1` is set — verified by a normal
      (unset-env) acceptance run collecting/skipping these tests, not
      just documented.
      Verified live: `uv run pytest tests/acceptance/test_elevated_locate.py -v`
      with the env var unset collects both tests in the module and
      SKIPs both, with the env-var reason in the skip text. See
      `docs/test-plans/staged-elevated-locate.md` §7 for the exact
      observed output.
- [ ] `camera locate <cam> --dry-run --reference TAG:SLUG:HEIGHT_CM ...`
      runs against real elevated references and reports a full solve
      (nadir, height, RMS residual) with RMS ≤ 1.0 cm and condition
      ≤ 447.
      **Not verifiable today** — no raised blocks are physically staged
      on the field (stakeholder-confirmed 2026-08-23; see doc §2). With
      `APRILCAM_TEST_ELEVATED=1` set, the live test genuinely attempts
      this command against the real daemon and cleanly SKIPs, naming
      "reference tag 15 (AprilTag) was not detected" — the honest,
      currently-only-reachable outcome. The test's quality-gate
      assertions (RMS ≤ `RMS_RESIDUAL_THRESHOLD_CM`, condition ≤
      `CONDITION_NUMBER_THRESHOLD`, both imported from
      `aprilcam.cli.locate`, never re-declared) are written and will
      execute the first time this test is run with blocks staged per
      `docs/test-plans/staged-elevated-locate.md` §3.
- [x] Zero writes occur — proven by ticket 014's no-write guard covering
      this test too (it is not exempted from the guard), not solely by
      `--dry-run`'s own contract.
      Verified live: the new module carries no exemption from
      `conftest.py`'s autouse `_no_write_guard` fixture; a full
      `tests/acceptance/` run (both with the env var unset and with it
      set to `1` against the real daemon, real `locate` subprocess
      included) completed with zero `NoWriteViolation`s.
- [x] No test in this module ever calls a non-dry `locate` or
      `calibrate` — enforced by the same node-id allowlist mechanism
      from ticket 015, extended to cover this module.
      Verified two ways: (1) this module's own
      `test_this_modules_dry_run_locate_call_passes_the_node_id_allowlist`
      reads its own source, asserts zero allowlist violations, then
      asserts the same call shape with `--dry-run` removed IS flagged
      — passing live under `APRILCAM_TEST_ELEVATED=1`; (2) an end-to-end
      collection-time check: a temporary sibling file containing a real
      non-dry `locate` call was added under `tests/acceptance/` and
      confirmed to fail collection with the allowlist's `UsageError`
      (`pytest --collect-only`), then removed. No allowlist code changes
      were needed — ticket 015's directory-wide scan already covers any
      new file added here.
- [x] `docs/test-plans/staged-elevated-locate.md` documents: the exact
      blocks/tags/heights used, the exact commands run, the RMS/condition
      quality gates, and states explicitly that a real non-dry
      `camera locate` is a deliberate, separate stakeholder action never
      performed by any test in this suite.
- [x] Without blocks raised (the default, non-elevated state), coverage
      for this geometry continues to rest on the existing unit/
      real-fixture regressions in
      `tests/aprilcam/calibration/test_geometry.py:485-887` — this
      ticket does not weaken or replace that coverage.
      `tests/aprilcam/calibration/test_geometry.py` was not modified by
      this ticket (`git status` confirms); the operator doc §10 cites
      the specific pinned regressions by name and line.

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
