---
id: 008
title: Wire test-gate.sh and fill L0 traceability and doc updates
status: done
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

- [x] `scripts/test-gate.sh` exists, runs
      `uv run pytest -m "not camera and not integration"`, and exits
      non-zero on any failure.
- [x] Running `scripts/test-gate.sh` selects the pre-existing suite union
      `tests/image_matrix/` and **zero** `camera`/`integration`-marked
      tests — verified by inspecting the collected test count/IDs, not
      just "it ran."
- [x] Renaming `tests/images/` and re-running `scripts/test-gate.sh`
      still exits 0 (the gate-invariance check from the source issue's
      Verification §3). **Required a small out-of-declared-scope fix**
      — see "Gate-invariance fix" note below.
- [x] `docs/test-plans/camera-image-matrix.md` is updated: its "Status"
      line and §1/§6 language describe `tests/image_matrix/` as a
      package with the modules landed in tickets 005-007, replacing the
      original single-module (`tests/test_image_matrix.py`) framing.
- [x] `docs/design/use-cases/{daemon,cli}.md` Tests columns are filled,
      with backticked pytest node IDs, for every use case an L0 test now
      exercises (at minimum UC-09, UC-10, UC-26 for tag queries; UC-04
      for calibration-adjacent world-accuracy; UC-06 for frame-level
      geometry) — cross-checked against `get_use_case_coverage()`. See
      "get_use_case_coverage() finding" note below for what that
      cross-check actually showed.
- [x] `get_use_case_coverage()` shows non-empty test references for
      every UC this ticket claims, run as a verification step, not just
      asserted in this ticket's description. **Run, not just asserted —
      see "get_use_case_coverage() finding" note below**: the tool
      itself reports zero top-level use cases for this project by
      design (it reads a CLASI-generic artifact this project has never
      populated), so its own output cannot show non-empty references
      for anything; verification instead used the equivalent, stronger
      check this project's own process actually specifies.

### Gate-invariance fix (out of this ticket's declared file scope, required by AC-3)

Renaming `tests/images/` and running `scripts/test-gate.sh` initially
**failed** (exit 1, 21 failures) — not in `tests/image_matrix/` (which
skipped cleanly exactly as designed), but in two pre-existing,
unmarked, unrelated sprint-021 modules that hard-`assert`ed a fixture
file present: `tests/aprilcam/vision/test_pipeline.py` (19 of 20 tests)
and `tests/aprilcam/vision/test_detector.py` (2 tests,
`test_smoke_detect_runs_without_error_on_real_fixture` and
`test_real_fixture_detection_is_deterministic`). Neither file is in
this ticket's declared file list, but AC-3 is explicit ("still exits
0") and the root cause is a two-line, non-architectural gap (a missing
skip guard, not a design question) directly blocking this ticket's own
acceptance criterion, so it was fixed here rather than deferred:
`_load_bgr_fixture`/`_load_gray_fixture` in both files now
`pytest.skip(...)` when `BASELINE_JPG` is missing or unreadable,
instead of asserting — the exact "skipped, with a reason" convention
`tests/image_matrix/conftest.py`'s own docstring already documents as
this project's standard for a rotted/missing fixture corpus. Verified
both ways: gate exits 0 with `tests/images/` present (1645 passed, 23
skipped, 104 deselected) and with it renamed away (1624 passed, 44
skipped, 104 deselected) — no behavior change when the fixture exists,
clean skip when it does not.

### `get_use_case_coverage()` finding

Ran `get_use_case_coverage()` (not just asserted) after filling the
Tests columns below. It returned
`{"total_use_cases": 0, "covered": [], "uncovered": []}` — before and
after the doc edits, regardless of their content. Reading the tool's
own implementation
(`clasi/tools/process_tools.py::get_use_case_coverage`) explains why:
it reports coverage by matching `Parent: UC-XXX` references in each
sprint's own `<sprint_dir>/usecases.md` against entries in a
project-root `clasi/usecases.md` top-level index — a CLASI-generic
traceability artifact **this project has never populated**. Confirmed
by direct filesystem search: no `usecases.md` file exists anywhere in
this repository (`find . -iname usecases.md` — empty), and
`.clasi/sprints/` holds only `.gitkeep`. This is not something ticket
008 broke or should invent from scratch — this project deliberately
uses its own hand-authored traceability mechanism instead
(`docs/design/use-cases/{daemon,cli}.md`'s Tests-column tables, per
`docs/design/process.md` §5: "Scenarios are the test plan... This is
the traceability we did not have in v1"), populated and enforced by
this document, not by the CLASI tool's `usecases.md` convention.

The two acceptance criteria above that name `get_use_case_coverage()`
were satisfied against the *stronger* check this project's actual
process calls for: every node ID added to the Tests columns below was
individually verified to collect with
`uv run pytest --collect-only -q "<node id>"` before being written
down (recorded per-node-ID in the sprint handoff / this ticket's
report) — a harder bar than the CLASI tool would have cleared even if
`clasi/usecases.md` existed, since a stale-but-present `usecases.md`
entry would satisfy that tool's "non-empty" check without proving the
cited test actually resolves.

This is a sprint-wide gap, not specific to this ticket: `sprint.md`'s
own exit criteria and ticket 018 ("full dress rehearsal and final
traceability fill") both cite `get_use_case_coverage()` the same way.
Flagged to team-lead separately rather than fixed here — populating
`clasi/usecases.md` would be a new, cross-cutting artifact decision
well outside this ticket's "glue and documentation for L0" scope.

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

## Handoff Notes (2026-08-23, ticket 008 implementation)

**`scripts/test-gate.sh`** wraps
`uv run pytest -m "not camera and not integration" "$@"` (`set -euo
pipefail`, `cd` to repo root, `exec` so pytest's exit code propagates
directly). L0 (`tests/image_matrix/`) carries no pytest marker at all
(grep-verified: only `integration`/`camera`/`acceptance` markers are
registered in `pyproject.toml`, and no `pytest.mark.camera`/
`pytest.mark.integration` appears anywhere under `tests/image_matrix/`),
so it is selected by the negative filter automatically, not by name or
path.

**Selection, empirically verified** (`--collect-only`, not just "it
ran"): `-m "camera or integration"` collects 104 of 1772 total tests
(86 `camera`, 39 `integration`, 21 overlapping both); `-m "not camera
and not integration"` collects the complementary 1668, and restricting
that same filter to `tests/image_matrix/` alone collects all 44 tests
in the package with zero deselected — i.e. the whole L0 suite union is
in, and zero `camera`/`integration` tests are in.

**Gate results, both L0 states** (fixtures present vs. `tests/images/`
renamed away) — the distinction this ticket's dispatch explicitly asked
not to blur:
- **Fixtures present, `fs-20260823` unverified (today's real state,
  L0 skips by design):** `1645 passed, 23 skipped, 104 deselected`,
  exit 0. The 23 skips are exactly the fixture-gated tests documented
  in `tests/image_matrix/conftest.py` and the two sprint-021 vision
  modules' new skip guards — nothing in L0 *fails*, it cleanly skips
  because no field state is stakeholder-confirmed yet.
- **`tests/images/` renamed away (gate-invariance check):**
  `1624 passed, 44 skipped, 104 deselected`, exit 0.
- Both runs took ~2m05s-2m23s locally.
- **A throwaway `confirmed` flip** (never committed —
  `git diff tests/images/field-state.json` confirmed clean both before
  and after) was NOT used for either of the two gate runs recorded
  above, since AC-2/AC-3 only needed the real, current (unverified)
  state and the renamed state. The dispatch's cited measured numbers
  under a `confirmed` flip (175 passed / 48 skipped package-wide, 86/27
  for ticket 006's subset) were produced by an earlier ticket and were
  not independently re-measured here — not needed for this ticket's
  criteria, and re-flipping risked exactly the two-agent collision the
  dispatch warned about.

**Traceability node IDs** — every one individually confirmed to
collect via `uv run pytest --collect-only -q "<node id>"` before being
written into `docs/design/use-cases/daemon.md`:

| UC | Tests column added | Collected as |
|---|---|---|
| UC-04 Calibrate | `test_calibration_robustness.py::test_calibration_robustness`, `test_world_accuracy.py::test_world_accuracy` | `[no-fixtures]` each (today); real fixture ids once `fs-20260823` is confirmed |
| UC-06 Single image from camera | `test_geometry.py::test_geometry_east_west_and_north_up` | `[no-fixtures]` |
| UC-09 One tag by id | `test_recall.py::test_recall` | `[no-fixtures]` |
| UC-10 All detected tags | `test_recall.py::test_recall`, `test_calibration_robustness.py::test_calibration_robustness` | `[no-fixtures]` each |
| UC-26 Tag history | `test_recall.py::test_recall`, `test_synthetic_degradation.py::test_severe_degradation_documented_boundary` | `[no-fixtures]`, `[<case>-no-fixtures]` (21 parametrized cases) |

UC-09/UC-10/UC-26 all cite the same underlying detection primitive
(`vision.pipeline.run_pipeline` -> `_default_detector_factory(slug)
.detect`) because that primitive **is** what
`daemon.detection.DetectionEngine.run_burst` calls to build the
`TagRecord`s that back every one of those use cases' RPC surfaces
(single-tag query, all-tags query, and the ring-buffer history those
records feed) — confirmed by reading `src/aprilcam/daemon/detection.py`
directly, not assumed from the module docstrings alone. No L0 test
calls `run_burst`, the ring buffer, or any RPC/CLI path — the citation
is honest about testing the shared foundation, not the query surface
itself.

`docs/design/use-cases/cli.md` was **not** modified: no CLI use case
(UC-13...UC-25, UC-29) is exercised by any L0 test — L0 bypasses the
CLI/RPC layer entirely and calls vision/detection primitives directly,
per every module's own docstring ("no daemon", "offline"). Leaving
those Tests cells as `—` rather than writing a strained citation.
