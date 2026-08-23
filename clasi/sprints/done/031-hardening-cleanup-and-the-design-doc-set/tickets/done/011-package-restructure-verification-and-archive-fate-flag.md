---
id: '011'
title: Package-restructure verification and archive/ fate flag
status: done
use-cases:
- SUC-011
depends-on:
- '001'
- '002'
- '006'
- '010'
github-issue: ''
issue: hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Package-restructure verification and archive/ fate flag

## Description

`docs/design/development-plan.md`'s Stage 12 named a package restructure
deliberately deferred to last — `server/`→`mcp/`, `core/` dissolves,
`view/` extracted, `ui/` deleted, legacy modules removed — "when the code
that moves is final." Confirmed during sprint planning: `ls
src/aprilcam/` already shows only the clean v2 module names (`calibration/`,
`camera/`, `cli/`, `client/`, `config/`, `daemon/`, `mcp/`, `playfield/`,
`proto/`, `tags/`, `view/`, `vision/`) — no `core/`, `server/`, or `ui/`
directory exists. The restructure Stage 12 described already happened
incrementally across sprints 026-030's direct-to-final-name delivery,
rather than as one big-bang move. What remains is verification, not a
move: confirm nothing imports a module the Stage 1 demolition already
condemned.

**This ticket does not delete or move `archive/`.** The umbrella issue
asks whether the 5.7 MB v1 tree is "worth carrying forever" — but
`CLAUDE.md` currently treats `archive/` as a live ideas source for the
whole v2 rebuild ("Before designing any of it from scratch, go read how
v1 did it... Hard-won specifics are buried in that code"). A sprint
ticket does not have standing to override that project-level
instruction by deleting the tree it governs. Flag the decision to the
stakeholder in this ticket's completion notes; do not resolve it here.

**Sequenced after tickets 001, 002, 006, and 010** so this verification
checks the sprint's own final state, not a mid-sprint snapshot.

## Acceptance Criteria

- [x] A test (mirroring this codebase's existing no-cv2-import-style
      pattern — see `docs/design/development-plan.md` Stage 10/11 for
      the precedent) asserts no module under `src/aprilcam/` imports
      anything from `archive/`.
- [x] The sweep's actual result is recorded in this ticket's completion
      notes: clean (nothing found), or naming exactly what it found if
      anything did turn up.
- [x] If the sweep finds a genuine dead import (something under
      `src/aprilcam/` or `tests/` still referencing a condemned v1-era
      module name), it is fixed as part of this ticket — a real defect,
      not deferred.
- [x] `archive/`'s fate is explicitly flagged to the stakeholder in this
      ticket's completion notes as a decision this sprint did not make
      (per Description above) — not silently dropped, not resolved
      unilaterally either way.
- [x] `uv run mypy` clean.

## Implementation Plan

**Approach**: write a headless test that walks `src/aprilcam/`'s Python
modules (or uses an import-graph tool already available in this
project's toolchain) and asserts none of them import from `archive.*`
or any known-condemned v1-era module name (cross-reference
`docs/design/development-plan.md` Stage 1's "Delete" list for the
specific names condemned: legacy `protocol.py`/`client.py` at the old
paths, `homocal`, the old web server, the old `ui/` tree, dead
compositing paths). This mirrors the existing "no-cv2-import" test
pattern already used elsewhere in this codebase (client/MCP modules
asserting `cv2` is absent from `sys.modules` after import) — same
shape, different target module set.

**Files**:
- A new test file (e.g. `tests/aprilcam/test_no_archive_imports.py`, or
  wherever this project's existing import-discipline tests live —
  check for a precedent file first) for the sweep itself.
- Any file the sweep finds with a genuine dead import (unlikely, given
  the planning-time research found none, but the ticket should still
  fix one if found rather than report-and-defer).

**Testing plan**:
- **Existing tests to run**: the full headless suite, to confirm the
  new import-sweep test itself passes and nothing else regresses.
- **New tests to write**: the import-graph sweep described above.
- **Verification command**: `uv run pytest -m "not camera"`.

**Documentation updates**: none required in `docs/design/` proper. This
ticket's completion notes are where the `archive/`'s-fate flag to the
stakeholder belongs (per Acceptance Criteria) — not a `docs/design/`
file, since no decision was made to document.

## Completion Notes

**Sweep result: clean.** Two static AST-based sweeps in the new
`tests/test_no_archive_imports.py` (mirroring
`tests/test_no_cv2_in_client_modules.py`'s established AST-scan
pattern), each run across every `.py` file under both `src/aprilcam/`
and `tests/`:

1. **No module imports `archive` or anything under it.** Every
   reference to `archive/` actually present in `src/aprilcam/`/`tests/`
   — dozens, concentrated in `calibration/`, `camera/`, `daemon/`,
   `client/`, `vision/` — is prose in a docstring or comment citing
   where the v2 code was converted from (e.g. "Converted from
   `archive/src/aprilcam/camera/registry.py`"), never a live `import`/
   `from ... import` statement.
2. **No module imports a condemned v1-era dotted name.** Checked
   specifically: `aprilcam.core`, `aprilcam.server`, `aprilcam.ui`
   (Stage 1's named-subsystem deletions / Stage 12's `server`→`mcp`
   rename and `ui` deletion), and `aprilcam.daemon.protocol`,
   `aprilcam.daemon.client`, top-level `aprilcam.stream` (the old
   module paths superseded by `proto/` + `client/daemon_client.py` +
   `daemon/stream_transport.py`/`framing.py`). Also hand-verified
   (targeted grep, not just the AST scan) that no dead multi-camera
   compositing symbol (`create_composite`, `get_composite_frame`,
   `get_composite_tags`, a `camera/composite.py` module) or old
   web-server module name (`aprilcam.web`, `web_server`) survives
   anywhere in `src/`/`tests/` — every remaining "composit-" hit in the
   tree is legitimate use (image compositing in tag rendering,
   geometric/class composition), not the removed compositing feature.

No genuine dead import turned up, so acceptance criterion 3 ("if the
sweep finds a genuine dead import... fixed as part of this ticket")
has nothing to act on. This confirms the ticket Description's own
framing: sprints 026-030's incremental direct-to-final-name delivery
already completed Stage 12's restructure; this ticket's contribution
is the verification test that makes that claim mechanically checked
going forward, not a code move.

**`archive/`'s fate — flagged to the stakeholder, not resolved here.**
The umbrella issue asks whether the 5.7 MB v1 tree is "worth carrying
forever." This ticket does not answer that question, per its own
Description and `sprint.md`'s Scope → Out of Scope. `CLAUDE.md`
currently instructs every session to treat `archive/` as a live ideas
source for the *entire* v2 rebuild ("go read how v1 did it... hard-won
specifics are buried in that code"), and this sweep's own results back
that framing up with fresh evidence: every one of the dozens of
`archive/` references found across `src/aprilcam/` in this pass is a
docstring citing exactly which v1 file a given piece of v2 code was
converted from (identity, registry, calibration, homography, geometry,
deskew, streaming, mDNS, framing, viewer, camera backends, and more) —
`archive/` is still actively cited as provenance throughout the
current codebase, not a dead historical curiosity. A sprint ticket has
no standing to override that project-level instruction by deleting,
moving, or otherwise disposing of the tree it governs.

**Decision needed from the stakeholder**: keep `archive/` indefinitely
as permanent provenance/ideas documentation, or decide it has now
served its purpose (the v2 rebuild is substantially complete as of
this sprint) and can be pruned, relocated outside the working tree, or
deleted. This ticket recommends the question stay explicitly open
until a session where the stakeholder weighs in directly — not
defaulted either way by inaction.

**`uv run mypy`: clean** (`Success: no issues found in 79 source
files`) — unaffected by this ticket; the new test file lives under
`tests/`, and mypy's `files` setting in `pyproject.toml` scopes
checking to `src/aprilcam` only.
