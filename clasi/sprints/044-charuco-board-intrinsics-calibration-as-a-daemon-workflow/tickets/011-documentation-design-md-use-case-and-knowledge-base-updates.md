---
id: '011'
title: Documentation, DESIGN.md, use-case, and knowledge-base updates
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
depends-on:
- '010'
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Documentation, DESIGN.md, use-case, and knowledge-base updates

## Description

Bring the written design and knowledge base in line with what was built (`design_docs: enabled`; see sprint.md Open Question 1).

## Acceptance Criteria

- [x] New `src/aprilcam/intrinsics/DESIGN.md`; updated `calibration/DESIGN.md` (lens store, carry-forward order, Decision 2 pairing), `daemon/DESIGN.md`, `client/DESIGN.md`, `cli/DESIGN.md`, `mcp/DESIGN.md`, and `docs/design/design.md` state-directory layout (`intrinsics.json`).
- [x] `docs/design/use-cases/cli.md` and `daemon.md` gain the intrinsics use cases (or extend UC-04/UC-14/UC-15) and the traceability tables; `tests/test_usecases_index.py` passes.
- [x] `docs/knowledge/2026-10-15-charuco-intrinsics-calibration-lessons.md` updated: restart caveat marked resolved by the daemon workflow, plus how to run the new command; the prototype README notes it is superseded.
- [x] README/CLI docs describe `camera intrinsics`, `--no-gui`, board spec resolution, and `--with-intrinsics` as the documented no-board fallback.

## Implementation Plan

Docs only; no source behaviour changes. If the `docs/design` overlay process is requested by the stakeholder, apply these edits through it instead.

## Testing

Run `uv run pytest tests/test_usecases_index.py tests/test_deploy_assets.py` and grep the docs for stale 'restart the daemon' instructions in the intrinsics context.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
