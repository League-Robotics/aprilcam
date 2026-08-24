---
status: pending
---

# CLASI's `get_use_case_coverage()` ignores `paths:` config and scans an obsolete per-sprint layout — needs a CLASI fix, then a re-verify here

## What happened

Sprint 036 ticket 001 generated `clasi/usecases.md` (29 use cases, from
`docs/design/use-cases/*.md`) with a regression test guarding it — the
project-local half of the fix for
`get-use-case-coverage-reports-zero-reads-the-wrong-file.md`. Calling
`get_use_case_coverage()` afterward **still returned `total_use_cases: 0`**.

## Root cause (read from the installed CLASI source, 2026-08-24)

`clasi/tools/process_tools.py` `get_use_case_coverage()` (installed at
`/Volumes/Cache/User-Eric/.local/pipx/venvs/clasi/.../clasi/`, version
0.20260821.6):

1. Resolves the index as `project.clasi_dir / "usecases.md"` and the sprint
   scan root as `project.clasi_dir / "sprints"`. `Project.clasi_dir` is
   documented in `project.py` as a fixed `<root>/.clasi` — "not
   configurable" — so it never honours this project's `.clasi/config.yaml`
   `paths:` block, which routes `sprints`/`issues`/`reflections` to the
   visible `clasi/` directory. The properly-configurable `Project.sprints_dir`
   etc. exist but this tool does not call them. Net effect: it reads
   `.clasi/usecases.md` (absent) and scans `.clasi/sprints/` (empty).
2. Even with the path fixed, the coverage pass (`~line 486-493`) looks for a
   per-sprint `usecases.md` in each sprint directory and extracts
   `Parent: UC-XXX` refs from it. The single-doc sprint model folded use
   cases into `sprint.md`'s `## Use Cases` section, so that file no longer
   exists for any recent sprint — coverage would read 0 % regardless.

So this is two CLASI-side defects, not a missing project file. A local
workaround (also writing `.clasi/usecases.md`) was deliberately **not** done:
it would make the gate report 29 use cases at 0 % coverage — a confident
wrong answer in place of an obviously-broken one.

## What this project needs

This issue is the aprilcam-side tracker; the fix lives in the CLASI
repository (file it there — the `report` skill creates a GitHub issue on the
clasi repo):

- `get_use_case_coverage()` should resolve through `Project.sprints_dir` and
  a `paths:`-aware use-case index location (or, better, read
  `docs/design/use-cases/*.md` directly — option 1 in the original issue).
- The coverage scan should parse `## Use Cases` / `Parent: UC-XXX` from
  `sprint.md` under the single-doc model, and ideally also the `Tests`
  columns in `docs/design/use-cases/*.md`.

Once a CLASI release fixes this:

- [ ] Upgrade `clasi` and re-run `get_use_case_coverage()` by hand — expect
  `total_use_cases: 29` (or the live count) and real coverage for the
  `Tests` columns sprint 035 filled in (`docs/design/use-cases/daemon.md`
  UC-04, UC-06, UC-09, UC-10, UC-26).
- [ ] Decide whether `clasi/usecases.md` + `scripts/generate_usecases_index.py`
  + `tests/test_usecases_index.py` (sprint 036 ticket 001) are still needed
  or can be retired if the tool reads the design docs directly.

## Related

- `get-use-case-coverage-reports-zero-reads-the-wrong-file.md` (sprint 036,
  ticket 001, commit `52d9e94`) — the project-local half
- Sprint 036 ticket 001's "AC4 verification note" — the full investigation
- `docs/design/process.md` §5 ("scenarios ARE the test plan")
