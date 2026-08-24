---
id: '001'
title: Generate clasi/usecases.md index from docs/design/use-cases/
status: in-progress
use-cases:
- SUC-005
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

- [x] A generator (a small committed script, e.g.
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
- [x] `clasi/usecases.md` is committed with real content (not a
      placeholder) reflecting the current `docs/design/use-cases/`
      set.
- [x] A regression test (e.g. `tests/test_usecases_index.py`)
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
      **NOT satisfied — see "AC4 verification" note below.** Confirmed
      29 `### UC-NN` headings across `daemon.md` (16) + `cli.md` (13)
      at time of writing; `clasi/usecases.md` now indexes all 29
      correctly. But calling `get_use_case_coverage()` by hand
      (2026-08-24) returns `{"total_use_cases": 0, ...}` regardless,
      because the *installed* tool never reads `clasi/usecases.md` at
      all — see the note for the root cause, which is more specific
      than (and postdates) what this ticket's Description and the
      sourcing issue assumed.
- [x] No change is made to the CLASI package's own
      `get_use_case_coverage()` implementation — it is out of scope
      (lives in a different repository).

### AC4 verification note (2026-08-24)

Read the installed tool's source directly
(`clasi/tools/process_tools.py` and `clasi/project.py` in the
installed `clasi` package, currently at
`/Volumes/Cache/User-Eric/.local/pipx/venvs/clasi/lib/python3.14/site-packages/clasi/`)
rather than trusting the issue's paraphrase. Finding:

- `get_use_case_coverage()` resolves its index file as
  `project.clasi_dir / "usecases.md"` and its sprint-scan root as
  `project.clasi_dir / "sprints"`.
- `Project.clasi_dir` is documented in `project.py` itself as "`.clasi/`
  directory — fixed hidden state anchor; not configurable" — it always
  returns `<root>/.clasi`, ignoring the `paths:` overrides in
  `.clasi/config.yaml`.
- This project's own `.clasi/config.yaml` already redirects
  `issues`/`sprints`/`reflections` to the **visible** `clasi/`
  directory (`clasi/issues`, `clasi/sprints`, `clasi/reflections`) via
  the *separate*, correctly-configurable `Project.sprints_dir` /
  `issues_dir` / `reflections_dir` properties (each calling
  `_resolve_dir(key)`, which honors the override).
- `get_use_case_coverage()` never calls those configurable properties —
  it manually derives `sprints_dir` from the hardcoded `clasi_dir`
  instead. So on this project it actually reads/scans
  `.clasi/usecases.md` and `.clasi/sprints/**/usecases.md` (both
  nonexistent), never `clasi/usecases.md` or `clasi/sprints/**` where
  the real artifacts (and now the real index) live.
- Empirically confirmed: calling `get_use_case_coverage()` after
  `clasi/usecases.md` was generated and committed with all 29 entries
  still returned `total_use_cases: 0`.

This is a real, separate bug from the one the sourcing issue described
(issue text: "it reads a `clasi/usecases.md` index" — true when that
issue was written during sprint 035 ticket 008, per the issue's own
"Related" section, but the installed package has since gained the
visible/hidden path-config split for `sprints`/`issues`/`reflections`
without `get_use_case_coverage()` being updated to use it). Fixing this
requires changing the installed CLASI package (either point it at
`project.sprints_dir`/a new configurable `usecases` key, or the
sprint's already-recorded "point it at `docs/design/use-cases/`
directly" follow-up) — out of scope here per this ticket's own AC5.
Recommend the team-lead fold this more precise root cause into the
CLASI-package follow-up issue already noted in sprint 036's plan
(Solution item 5), so whoever picks it up doesn't have to
re-discover it.

## Testing

- **Existing tests to run**: `uv run pytest tests/ -k usecase -q`
  (should currently collect nothing — confirms no naming collision
  before adding the new test).
- **New tests to write**: `tests/test_usecases_index.py` — parses
  `docs/design/use-cases/*.md`, derives the expected UC id/title set,
  and asserts it matches `clasi/usecases.md`.
- **Verification command**: `uv run pytest tests/test_usecases_index.py -q`
