---
status: in-progress
sprint: '036'
tickets:
- 036-001
---

# `get_use_case_coverage()` always reports zero — it reads a file this project never populated

## Description

The CLASI MCP tool `get_use_case_coverage()` returns
`{"total_use_cases": 0, ...}` on this project regardless of what the use-case
documents actually contain.

Verified against the tool's own source during sprint 035 ticket 008: it reads a
`clasi/usecases.md` index, which this project has never populated. AprilCam's
real use cases live in `docs/design/use-cases/*.md` (29 use cases, 111 scenario
IDs), which the tool never looks at.

So the tool is not reporting "no coverage" — it is reporting "no use cases,"
and it would say the same thing if every scenario were fully covered.

## Why it matters

Several places treat this tool as the traceability audit:

- Sprint 035's `sprint.md` exit criteria
- Ticket 018's final traceability audit criterion
- The layered-test-system issue's Verification section, item 5

Each of those would pass vacuously against a tool that always returns zero, or
fail confusingly, depending on how the check is written. A traceability gate
that cannot see the traceability data is worse than no gate, because it looks
like one.

## Workaround in use

Ticket 008 substituted a stronger check: every node ID written into a `Tests`
column was individually confirmed to resolve via

```bash
uv run pytest --collect-only -q "<node id>"
```

before being recorded. That catches the failure this gate actually needs to
catch — a `Tests` entry naming a test that does not exist — and it does not
depend on the broken tool. Worth keeping regardless of how the tool is fixed.

## Proposed fix

Decide which is true for this project, then make it so:

1. **Point the tool at `docs/design/use-cases/`** — matches where the use cases
   really live and where `docs/design/process.md` §5 says the test plan lives
   ("scenarios ARE the test plan"). Requires the tool to parse that layout.
2. **Populate `clasi/usecases.md`** as an index that references the per-file
   use cases, so the existing tool has something real to read. Cheaper, but
   introduces a second place that can drift from the first.

Option 1 looks right — a generated index that must be hand-maintained will rot,
which is the same class of problem as the fixture rot this sprint just fixed.

Either way, update the places listed above so they stop relying on a check that
cannot fail.

## Verification

After the fix, `get_use_case_coverage()` must report a non-zero total matching
`docs/design/use-cases/` (29 use cases, 111 scenario IDs at time of writing),
and must show real coverage for the `Tests` columns sprint 035 filled in
(`docs/design/use-cases/daemon.md` UC-04, UC-06, UC-09, UC-10, UC-26).

A regression test should assert the total is non-zero, so this cannot silently
return to reporting zero.

## Related

- Found by sprint 035 ticket 008 (`a7bb77d`)
- `docs/design/process.md` §5
- `clasi/sprints/035-.../sprint.md` exit criteria and ticket 018
