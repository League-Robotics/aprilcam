---
id: '012'
title: 'Documentation: calibration and config DESIGN.md, use cases and knowledge base'
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
- SUC-007
depends-on:
- '001'
- '002'
- '003'
- '004'
- '005'
- '006'
- '007'
- 008
- 009
- '010'
- '011'
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Documentation: calibration and config DESIGN.md, use cases and knowledge base

## Description

Bring the design set and knowledge base in line with the code (the project
has design docs enabled).

**Update:**
- `src/aprilcam/calibration/DESIGN.md`: purpose/orientation (new
  `pose.py`, `gates.py`), the pose-first solve, gates and thresholds,
  degradation ladder, fallback rules, derived homography convention and camera
  position, injected target observer, removal of the estimator section, and
  the sign-convention list. Refresh "Last reviewed" and the file-size claims.
- `src/aprilcam/config/DESIGN.md`: definition schema (`size_mm`,
  `policy`, `playfield.marker_size_mm`, `calibration_target`), defaults and
  validation; state that marker orientation is detected, not declared.
- `src/aprilcam/intrinsics/DESIGN.md`: `target.py` and the new
  `intrinsics -> config` edge.
- `docs/design/use-cases/daemon.md` UC-04: the "all fiducials or fail" rule
  becomes per-field policy (required/helpful), with the degradation scenarios;
  `docs/design/use-cases/cli.md` UC-15: new output and the removed flag;
  mirror in the `*-interface-trace.md` files; name the mounted-tag/parallax
  use case if one exists. Keep `tests/test_usecases_index.py` green.
- `docs/design/design.md` / `overview.md` only where they describe
  calibration inputs, the estimator, or the parallax input.
- `docs/knowledge/`: a new entry (e.g.
  `2026-10-NN-pose-based-field-calibration.md`) with the sign conventions,
  gate thresholds as tuned, the IPPE ambiguity behaviour near fronto-parallel
  cameras, and how to author a sized definition; append a pointer to
  `2026-09-04-secondary-playfield-resurvey-and-lens-model.md` noting that the
  extra markers' original motivation is superseded.
- Playfield JSON: `main-playfield.json` already carries
  `playfield.marker_size_mm: 46` (do not re-add). If the stakeholder has
  supplied the secondary-field marker size (Open Question 2), add
  `marker_size_mm` to the `playfield` block of
  `config/aprilcam/playfields/secondary-playfield.json`; otherwise leave it
  and document that the secondary field stays on the centre-point path.
- Knowledge entry also records: orientation detection by best-of-4 rotation
  residual, the 2026-10-16 main-field facts (46 mm; orientations 1-3 upright,
  4 +90, 8 -90, 5/6/7/9/10 upside down), and the 9/10 swap as the worked
  example for the leave-one-out gate (30 cm rms before, 0.23 cm rms and
  0.75 cm worst leave-one-out after).
- Record the deferred tag-free table-edge option (issue's "Explored and
  deferred") in the knowledge base so it is not rediscovered.
- Terminology: user-facing text says "lens model" and "field calibration".

## Acceptance Criteria

- [x] Every item above done; no doc still describes `--with-intrinsics` as
      current behaviour.
- [x] `uv run pytest tests/test_usecases_index.py` passes.
- [x] `validate_design` (CLASI) reports no new findings, if available.
- [x] Knowledge-base entry added and linked from the calibration DESIGN.md.

## Implementation Plan

Docs only; read the final code of tickets 001-011 first so the prose
matches what shipped (thresholds, field names, wording).

## Testing

- **Existing tests to run**: `uv run pytest tests/test_usecases_index.py`
- **New tests to write**: none.
- **Verification command**: `uv run pytest tests/test_usecases_index.py`

## Documentation Updates

This ticket is the documentation update.
