---
id: '005'
title: Close UC-25's naming-on-reconnect open question
status: done
use-cases:
- SUC-005
depends-on: []
github-issue: ''
issue:
- sprint-020-followups-socket-collision-and-cli-polish.md
- hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Close UC-25's naming-on-reconnect open question

## Description

`docs/design/use-cases/cli.md`'s UC-25 "Open questions" section asks:
"Does probing re-derive names for cameras already in the registry, and
what happens if a name comes out different than last time? A changed
name silently orphans an installed config." This reads as an open
behavioral question, but the code already implements a considered
answer: `camera/registry.py`'s `resolve()` explicitly never renames a
camera's persisted slug on reconnect (see that module's own docstring —
"Reused... conservative reading... never renames"), with regression
coverage in
`tests/aprilcam/camera/test_registry.py::test_reconnect_refreshes_device_name_and_last_seen`.

This is also the *safe* answer to the risk UC-25's own question names:
never renaming is precisely what prevents a name change from orphaning
an installed config, not a source of that risk. Ticket 020-002 made
this decision without explicitly closing the use-case document's open
question — this ticket closes that loop, the same way ticket 025-003
closed UC-12's reference-point question with a "Decisions resolved"
note in the use-case document.

**No production code change.** This is a documentation-closure ticket
plus one additional regression test covering a gap in existing
coverage: `test_reconnect_refreshes_device_name_and_last_seen` varies
`device_name` on reconnect, not the *proposed slug* itself. Add a case
that exercises the actual scenario UC-25's question describes —
identity resolution proposing a *different slug* than what's stored,
under the same `hardware_key`.

## Acceptance Criteria

- [x] `docs/design/use-cases/cli.md`'s UC-25 "Open questions" section
      records the resolution: reconnect never renames a camera's
      persisted slug (decision made by ticket 020-002), the rationale
      (this is what prevents the exact config-orphaning risk the
      question itself names), and a pointer to
      `camera/registry.py`'s `resolve()` docstring — in the same style
      as ticket 025-003's UC-12 "Decisions resolved" closure in
      `docs/design/use-cases/daemon.md`.
- [x] A new regression test in `tests/aprilcam/camera/test_registry.py`
      covers the case existing coverage does not: the same
      `hardware_key` reconnects with `CameraIdentity.slug` (the
      *proposed* slug from identity resolution) different from the
      slug already stored for that `hardware_key` — asserts
      `resolve()` returns the *stored* slug unchanged, not the newly
      proposed one.
- [x] No change to `camera/registry.py`'s actual `resolve()` logic —
      this ticket documents and tests existing behavior, it does not
      alter it.

## Implementation Plan

**Approach**: read `camera/registry.py`'s `resolve()` and its module
docstring closely (already-cited above) to state the resolution
accurately; read ticket `025-003-mount-correction-geometry-and-reference-point-decision.md`
(done, in `clasi/sprints/done/025-complete-the-daemon-query-surface/
tickets/done/`) as the formatting precedent for how a "Decisions
resolved" closure note should read in a use-case document.

**Files**:
- `docs/design/use-cases/cli.md` — UC-25's open-questions section gains
  the resolution note.
- `tests/aprilcam/camera/test_registry.py` — new regression test.

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/camera/test_registry.py -v` (confirm no regression to
  the existing reconnect/rename test suite this ticket extends).
- **New tests to write**: as described in Acceptance Criteria above —
  a reconnect scenario where the identity's proposed slug differs from
  the stored one, asserting the stored slug wins.
- **Verification command**: `uv run pytest -m "not camera"`.

**Documentation updates**: `docs/design/use-cases/cli.md` (the primary
deliverable of this ticket, described above).
