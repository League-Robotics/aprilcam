---
id: '015'
title: Acceptance inventory against the real daemon
status: open
use-cases: [UC-29]
depends-on: ['014']
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Acceptance inventory against the real daemon

## Description

The read-only/reversible acceptance inventory itself, running under
ticket 014's no-write guard against the REAL daemon and real field
state: daemon status, camera list, raw + deskewed frames from cameras 3
and 5, `get_tags` ⊇ the confirmed field state, `where` on a surveyed
tag, an MCP stdio round trip to the REAL daemon (guide, tags,
`draw_path`/`draw_marker` on a throwaway layer, list, clear, prove
gone), and a mount_z register→moves-per-formula→unregister→restored
cycle. `calibrate` and any non-dry `locate` call are structurally
excluded via a pytest node-id allowlist — not merely avoided by
convention — so a future test added to this file cannot accidentally
introduce a write-risking call without the allowlist itself changing.

## Acceptance Criteria

- [ ] Every test in this ticket's module runs against the REAL daemon
      (no isolation env vars) and passes ticket 014's no-write guard —
      the real `config_dir`/`state_dir` are byte-identical before and
      after the full module runs.
- [ ] Daemon status, camera list, and raw+deskewed `get_frame` for
      cameras 3 and 5 all succeed and return plausible data (non-empty
      camera list, non-zero-size frames).
- [ ] `get_tags` returns a superset of the confirmed field state's
      expected visible tags (from `tests/images/field-state.json`'s
      current confirmed entry).
- [ ] `where` on at least one surveyed tag returns its known surveyed
      position within tolerance.
- [ ] An MCP stdio round trip against the REAL daemon: `get_guide`,
      `get_tags`, `draw_marker`/`draw_path` on a throwaway annotation
      layer, `list_annotations` confirms it present, `clear_layer`
      confirms it gone.
- [ ] A mount_z register→moves-per-formula→unregister→restored cycle
      runs against the real daemon and real camera geometry, using the
      same displacement-constant helper as tickets 002/011 (no
      redeclaration).
- [ ] A pytest node-id allowlist (or equivalent collection-time guard)
      structurally excludes `calibrate` and any non-dry `locate` call
      from this module — verified by a test that a disallowed node
      cannot be collected/added without also touching the allowlist
      itself (i.e., the exclusion is enforced, not just documented).
- [ ] Every test in this module carries `acceptance`+`camera`+
      `integration` markers.

## Implementation Plan

**Approach**: Build the inventory as one module of focused, individually
skippable-per-camera test functions (mirroring L1's per-camera skip
discipline) rather than one monolithic test, so a single unavailable
camera doesn't block the rest of the inventory. The node-id allowlist
can be implemented as a `pytest_collection_modifyitems` hook in
`tests/acceptance/conftest.py` that fails collection outright if any
node under `tests/acceptance/` calls a disallowed daemon method name
(detected via a lightweight source/AST check or an explicit
per-test-function decorator registry — pick whichever ticket 014's
conftest structure makes cleaner, and document the choice).

**Files to create**:
- `tests/acceptance/test_inventory.py`

**Testing plan**:
- Requires the real daemon running and real cameras 3, 5 attached (each
  camera-dependent assertion skips individually if its camera is
  unavailable, per the established convention).
- **Verification command**: `uv run pytest tests/acceptance/test_inventory.py -m acceptance -v`
  — run deliberately, by a human, against the real rig; not part of any
  automated per-ticket CI loop for this sprint.

**Documentation updates**:
- None yet — ticket 017 covers the runner script and docs.
