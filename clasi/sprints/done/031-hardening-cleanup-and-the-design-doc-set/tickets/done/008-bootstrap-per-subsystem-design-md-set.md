---
id: 008
title: Bootstrap per-subsystem DESIGN.md set
status: done
use-cases:
- SUC-008
depends-on:
- '001'
- '002'
- '006'
- '010'
github-issue: ''
issue:
- bootstrap-per-subsystem-design-docs-for-v2-tree.md
- hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Bootstrap per-subsystem DESIGN.md set

## Description

`clasi design validate` fails: every subsystem directory under
`src/aprilcam/` (12 subsystems: `calibration/`, `camera/`, `cli/`,
`client/`, `config/`, `daemon/`, `mcp/`, `playfield/`, `proto/`,
`tags/`, `view/`, `vision/`) is missing its co-located `DESIGN.md`, and
so is the source root itself (`src/aprilcam/DESIGN.md`).
`docs/design/design.md`'s subsystem table already links to these files
(e.g. `[DESIGN.md](../../src/aprilcam/daemon/DESIGN.md)`) — the links
are dead. `.clasi/config.yaml` has `design_docs: enabled`, so this is a
standing failure, not a preference.

Run the `bootstrap-design` skill against `src/aprilcam` (the sole
declared source root): produce `src/aprilcam/DESIGN.md` (root overview)
plus one co-located `DESIGN.md` per subsystem, seeded from the already
written design set (`docs/design/design.md`, `specification.md`, the
interface skeletons, the two interface-sufficiency traces) and from the
actual code. Six subsystems have real implementations to document from
code: `camera/`, `client/`, `cli/`, `config/`, `daemon/`, `proto/`. Six
are still interface-only: `calibration/`, `mcp/`, `playfield/`, `tags/`,
`view/`, `vision/` — document the contract and say so plainly, rather
than inventing undelivered behavior.

**Sequenced last among the code-changing tickets** (depends on 001,
002, 006, 010) so the bootstrapped docs describe this sprint's own
final state — the new `protocol_version` field, orphan-detection
surface, libcamera backend wiring, and annotation limits — rather than
a snapshot that goes stale the moment those tickets land.

**Correction found during execution (verified against actual code, not
copied from the planning-time snapshot above):** none of the six
subsystems this ticket's Description names as "still interface-only"
were actually interface-only by the time this ticket ran.
`calibration/calibration.py` (~1470 lines), `playfield/playfield.py` +
`query.py`, `tags/generator.py`, `vision/{detector,pipeline,motion,
aruco_compat}.py`, `mcp/server.py` + `mcp/tools/*`, and
`view/viewer.py` (~580 lines) are all real, substantial
implementations — every one of the 12 subsystems has a real
implementation today. The dependency-and-sequencing rationale above
(document this sprint's own final state, not a stale mid-sprint
snapshot) is exactly why: enough sprints landed real code in these six
subsystems between this sprint's planning and this ticket's execution
that the "still interface-only" premise itself went stale. Each
subsystem's `DESIGN.md` documents its actual current implementation
from code read during this ticket, per the acceptance criterion below
(corrected to match).

## Acceptance Criteria

- [x] `clasi design validate` reports `ok: true`, zero messages.
- [x] `src/aprilcam/DESIGN.md` (root overview: what the root is, its
      subsystem list with one line each, any conventions every
      subsystem doc may assume) exists and is non-empty.
- [x] All 12 subsystem `DESIGN.md` files exist, are non-empty, and are
      derived via `clasi.design.paths.design_doc_path_for`/
      `clasi.design.store.write_design_doc` (never a hand-constructed
      path) per the `bootstrap-design` skill's load-bearing rule.
- [x] The six subsystems with real implementations (`camera/`,
      `client/`, `cli/`, `config/`, `daemon/`, `proto/`) are documented
      from actual code read during this ticket — including this
      sprint's own additions from tickets 001, 002, 006, 010.
- [x] Corrected: all six subsystems named in the planning-time
      "interface-only" list (`calibration/`, `mcp/`, `playfield/`,
      `tags/`, `view/`, `vision/`) turned out to already have real,
      substantial implementations by execution time — verified against
      actual code, not copied from the stale snapshot (see the
      Description's "Correction found during execution" note above).
      All 12 subsystems are therefore documented from actual code, and
      none is described as interface-only, since that would now be
      false.
- [x] `docs/design/design.md` is rewritten (via `write_system_doc`) so
      its subsystem table's links all resolve to real files — no dead
      links remain.

## Implementation Plan

**Approach**: follow the `bootstrap-design` skill's process exactly
(call `get_skill_definition("bootstrap-design")` for the full text if
not already loaded): identify roots and subsystems (mechanical — the
12 subsystems are already enumerated above, confirmed via
`validate_design`'s current failure output), read each subsystem's
actual code, start each doc from `clasi.design.store.subsystem_template()`,
derive every path via `clasi.design.paths`/write via
`clasi.design.store` (never hand-constructed), then validate/fix/
re-validate until `clasi design validate` passes cleanly. Do not
consider this ticket done while validation fails.

**Files**:
- `src/aprilcam/DESIGN.md` (new, root overview).
- `src/aprilcam/{calibration,camera,cli,client,config,daemon,mcp,
  playfield,proto,tags,view,vision}/DESIGN.md` (12 new files).
- `docs/design/design.md` (rewritten wholesale by
  `write_system_doc` — this is a full-overwrite operation per that
  skill's documented semantics, not a merge; any hand-edits made to the
  current file between sprint planning and this ticket's execution
  should be captured first, per `sprint.md`'s Migration Concerns).

**Testing plan**:
- **Existing tests to run**: none of this sprint's other tickets'
  tests are affected (this ticket produces documentation, not code).
- **New tests to write**: none — `clasi design validate` itself is the
  verification mechanism, not a pytest-authored test.
- **Verification command**: `clasi design validate` (or the
  `validate_design` MCP tool) reporting `ok: true`.

**Documentation updates**: this ticket's entire deliverable is
documentation — see Files above. No other subsystem's behavior changes.
