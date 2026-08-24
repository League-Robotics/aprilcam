---
id: '001'
title: Generate clasi/usecases.md index from docs/design/use-cases/
status: open
use-cases: [SUC-005]
depends-on: []
github-issue: ''
issue: get-use-case-coverage-reports-zero-reads-the-wrong-file.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Generate clasi/usecases.md index from docs/design/use-cases/

## Description

`get_use_case_coverage()` (a CLASI MCP tool, implemented outside this
repo) reads `clasi/usecases.md` and parses lines matching
`##\s+(UC-\d+):\s*(.+)` to build its `total_use_cases` count. This
project has never populated that file — the real use cases live in
`docs/design/use-cases/*.md` (`daemon.md`, `cli.md`; `README.md` and
the two `*-interface-trace.md` files are not sources of UC entries),
each with headings like `### UC-01 — Start the daemon and configure
it`. As a result the tool always reports zero, and several sprint exit
criteria that treat it as a traceability audit pass vacuously.

Sprint 036's plan (see sprint.md's Solution, item 5) records fixing the
tool's own implementation (pointing it at `docs/design/use-cases/`
directly) as a **recommended follow-up issue for the CLASI package
itself** — out of scope here, since that code lives in a different
repository. This ticket's scope is narrower and entirely within this
repo: generate a `clasi/usecases.md` index in the exact format the
existing tool already parses, sourced from `docs/design/use-cases/`,
plus a regression test so the index can't silently rot back to
matching nothing.

At time of writing, `daemon.md` + `cli.md` together contain 29 real
`### UC-NN` headings (`README.md`'s own `### UC-nn — <goal...>` line is
a template, not a real use case — exclude `README.md` and the two
`*-interface-trace.md` files entirely) — matching the issue's own
"29 use cases" figure. Confirm the exact current count when
implementing, since sprint work between now and then may add or
renumber use cases.

## Acceptance Criteria

- [ ] A generator (a small committed script, e.g.
      `scripts/generate_usecases_index.py`, or a test-side fixture
      function — implementer's choice) reads every `### UC-NN — Title`
      heading from `docs/design/use-cases/*.md` (excluding
      `README.md` and `*-interface-trace.md`) and writes
      `clasi/usecases.md` with one `## UC-NN: Title` line per use
      case, in ascending numeric order, matching
      `get_use_case_coverage()`'s parsing regex
      (`##\s+(UC-\d+):\s*(.+)`) exactly (two-digit vs. bare numeric
      IDs: use whatever `UC-NN` the source heading actually uses,
      e.g. `UC-01`, so the regex's `\d+` still matches).
- [ ] `clasi/usecases.md` is committed with real content (not a
      placeholder) reflecting the current `docs/design/use-cases/`
      set.
- [ ] A regression test (e.g. `tests/test_usecases_index.py`)
      independently re-derives the same UC id/title list from
      `docs/design/use-cases/*.md` and asserts it matches
      `clasi/usecases.md`'s committed content exactly (or, at minimum,
      that the UC-id set and count match) — so a future use-case
      addition/removal without regenerating the index fails this test
      rather than silently drifting.
- [ ] Calling `get_use_case_coverage()` by hand (documented in the
      ticket's own verification, not necessarily automated) reports
      `total_use_cases` equal to the real count (29 at time of
      writing, per the note above — confirm the live count).
- [ ] No change is made to the CLASI package's own
      `get_use_case_coverage()` implementation — it is out of scope
      (lives in a different repository).

## Testing

- **Existing tests to run**: `uv run pytest tests/ -k usecase -q`
  (should currently collect nothing — confirms no naming collision
  before adding the new test).
- **New tests to write**: `tests/test_usecases_index.py` — parses
  `docs/design/use-cases/*.md`, derives the expected UC id/title set,
  and asserts it matches `clasi/usecases.md`.
- **Verification command**: `uv run pytest tests/test_usecases_index.py -q`
