---
id: '002'
title: Wire aprilcam tags generate CLI command
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: tag-generation-and-daemon-discovery.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire aprilcam tags generate CLI command

**Depends on**: 001 (`TagGenerator` must exist before the CLI can call it)
**Serves**: SUC-001, SUC-002, SUC-003 (UC-21)

## Description

Add a `generate` subcommand to `src/aprilcam/cli/tags.py`, as a sibling
to the existing `mount` subcommand (that module's own docstring already
anticipates this: *"Sprint 028's later `aprilcam tags generate`
(UC-21) is expected to add its own subcommand to *this same file*
rather than conflict with it"*). This is a thin argv-to-`TagGenerator`-
call shell, matching `adapters.md`'s "adapters hold no authoritative
state, adapters do no vision" rule already applied throughout
`cli/tags.py`'s `mount` commands — no image rendering happens in this
file, only argument parsing, id-range parsing, and error/result
formatting.

Unlike `mount register`/`mount clear`/`mount list`, `tags generate` is
**purely offline** — it never touches `Discovery`, never connects to a
daemon, and takes no `-d`/`--daemon` or `--all` flag at all (those
flags exist to target a daemon; there is no daemon in this command's
path, so omit `add_daemon_option`/`add_all_option` entirely rather than
adding them and having them do nothing).

## Files to touch

- `src/aprilcam/cli/tags.py` — add `cmd_generate` and its subparser,
  alongside the existing `cmd_register`/`cmd_clear`/`cmd_list` and
  `p_mount`. `tags.py`'s `add_subparser` currently only wires the
  `mount` sub-noun under `tags`; add a `generate` sub-noun as a
  sibling, both hanging off the same top-level `tags` subparsers
  action (`sub = parser.add_subparsers(dest="tags_command", ...)`).
- `tests/aprilcam/cli/test_tags_cli.py` (existing file — confirm its
  current scope with a quick read before adding tests; it currently
  covers `mount register`/`clear`/`list` only) — add `generate`
  coverage.

## Acceptance Criteria

- [x] `aprilcam tags generate --family aruco --ids 1-10 --size-cm 8
      --out ./playfield-tags` parses `--ids` into `(1, 2, ..., 10)`
      (reuse or mirror the archive's `parse_ids` idea —
      `archive/src/aprilcam/tags/taggen.py`'s comma/range spec parsing
      — implemented fresh here, not imported from `archive/`) and calls
      `TagGenerator().generate(TagFamily.ARUCO, ids, size_cm=8.0,
      out_dir=Path("./playfield-tags"), dpi=300)` (SUC-001).
- [x] `--family` is restricted to `apriltag`/`aruco` via argparse's own
      `choices=`, matching `mount register`'s existing family-argument
      pattern in this same file (`TagFamily(args.family)` then always
      succeeds once argparse has accepted the value — no separate
      translation table).
- [x] `--dpi` defaults to `300` (matches `TagGenerator.generate`'s own
      default) and is overridable.
- [x] SUC-002: an out-of-range id (e.g. `--ids 1-3,55` for `aruco`)
      surfaces `InvalidTagId` via the existing `report_error` helper
      (`cli._common.report_error`) — printed to stderr, non-zero exit —
      and the CLI performs no partial cleanup logic of its own (the
      "nothing written for any id in the batch" guarantee is
      `TagGenerator`'s, not this file's; this file only needs to not
      break that guarantee by, e.g., calling `generate` once per id in
      a loop — call it once with the whole id tuple).
- [x] SUC-003: an unwritable `--out` surfaces `UnwritableOutputPath` via
      `report_error`, same pattern.
- [x] On success, prints a confirmation naming how many images were
      written and where (mirroring this file's existing confirmation
      tone, e.g. `cmd_register`'s `print(f"registered ...")` line) —
      not silent success.
- [x] `generate` has **no** `-d`/`--daemon` or `--all` option (it is
      purely offline; verify by checking the subparser's argument list
      in a test, or simply by not calling `add_daemon_option`/
      `add_all_option` for it).
- [x] `aprilcam tags generate --help` and `aprilcam tags --help` both
      show `generate` alongside `mount`, and the existing `mount`
      subcommands are unchanged (no regression to `cmd_register`/
      `cmd_clear`/`cmd_list` or their tests).
- [x] `uv run mypy` clean on `src/aprilcam/cli/tags.py`. Zero
      `typing.Protocol`.
- [x] Full existing suite stays green, including the pre-existing
      `tests/aprilcam/cli/test_tags_cli.py` mount tests (unmodified).

## Implementation Plan

**Approach:**
1. Read the current `tests/aprilcam/cli/test_tags_cli.py` to confirm
   its existing fixture/style (likely mirrors `test_daemon_cli.py`'s
   direct-function-call-against-fakes style, but `generate` needs no
   `discovery_factory` fake at all since it never touches a daemon —
   confirm this by reading `TagGenerator`'s constructor from ticket
   001's completed work first).
2. Add an id-range parser (module-level function or inline in
   `cmd_generate` — implementer's call; the archive's `parse_ids`
   regex approach in `archive/src/aprilcam/tags/taggen.py:19-33` is a
   reasonable starting idea, written fresh) that turns `"1-3,55"` into
   a sorted tuple of unique ints.
3. Add `cmd_generate(args) -> int`: parse ids, build `TagFamily`,
   instantiate `TagGenerator()` directly (no factory/DI parameter
   needed — this is a pure local computation, unlike every other
   command in this file, which all take a `discovery_factory` seam for
   daemon connection; `generate` has nothing to inject a fake for
   besides `TagGenerator` itself, so add a `tag_generator_factory`
   keyword defaulting to `TagGenerator` only if it meaningfully
   simplifies testing — otherwise constructing `TagGenerator()`
   directly in the test and calling `cmd_generate` against a `tmp_path`
   `--out` is simpler and matches SUC-001/002/003's own "purely
   offline" framing).
4. Add the `p_generate` subparser in `add_subparser`, with `--family`,
   `--ids`, `--size-cm`, `--out`, `--dpi` arguments.
5. Add tests for the id-range parser, the three failure contracts, and
   the success path.

**Files to create/modify:**
- `src/aprilcam/cli/tags.py`
- `tests/aprilcam/cli/test_tags_cli.py`

**Testing plan:**
- Headless, no daemon fixture needed — this is the one CLI command in
  the whole codebase that genuinely needs zero `Discovery`/daemon
  machinery in its tests.
- Cover: id-range parsing (single ids, ranges, mixed, e.g. `"1-3,7"`),
  the three UC-21 failure contracts routed through `report_error`, and
  the success-path confirmation message.

**Documentation updates:** none required beyond the CLI's own
`--help` text (argparse `help=` strings), which is part of the
subparser wiring itself.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/
  test_tags_cli.py` and the full suite (`uv run pytest`) to confirm
  `mount`'s existing tests are untouched.
- **New tests to write**: id-range parsing, SUC-001 success (calls
  `TagGenerator.generate` with the right arguments — assert via a
  fake/spy `TagGenerator`, not a real OpenCV render, to keep this
  file's tests fast and focused on argv wiring — ticket 001's own
  suite already covers real rendering), SUC-002/SUC-003 failure
  routing through `report_error`.
- **Verification command**: `uv run pytest && uv run mypy`

## Completion Notes

Implemented exactly as planned, no scope changes and nothing partial
or blocked.

- `src/aprilcam/cli/tags.py`: added `_parse_ids` (module-level,
  written fresh -- not imported from `archive/`) and `cmd_generate`,
  plus a `p_generate` subparser wired as a sibling of `p_mount` under
  the same `tags` subparsers action (`sub.add_parser("generate", ...)`
  directly, since `generate` is a leaf command with no further
  sub-subcommands, unlike `mount`). `cmd_generate` takes a
  `tag_generator_factory` keyword (defaulting to the concrete
  `TagGenerator`), mirroring `cmd_register`/`cmd_clear`/`cmd_list`'s
  `discovery_factory` seam, so tests can inject a fake/spy generator
  instead of exercising real OpenCV rendering. `generate` gets no
  `-d`/`--daemon` or `--all` -- `add_daemon_option`/`add_all_option`
  are simply never called for its subparser. `--family`/`--ids`/
  `--size-cm`/`--out` are `required=True` (no sensible default);
  `--dpi` defaults to `300` and is overridable. Updated the stale
  `tags` top-level help text ("and, later, generation)" -> "and marker
  generation (UC-12, UC-21)") and the module/`add_subparser`
  docstrings to describe `generate`'s offline design instead of
  anticipating it.
- `tests/aprilcam/cli/test_tags_cli.py`: added a `_FakeTagGenerator`
  spy and a `_generate_args` helper (matching the file's existing
  `_FakeDaemon`/`_FakeDiscovery`/`_args` pattern), plus new tests for
  `_parse_ids` (singles, inclusive ranges, mixed, dedup+sort,
  whitespace, malformed-spec `ValueError`), `cmd_generate`'s
  argv-to-`TagGenerator.generate` wiring (single call for the whole
  batch, not a per-id loop), the SUC-001 success/confirmation path,
  SUC-002/SUC-003 failure routing through `report_error`, the `--dpi`
  default/override, and argparse wiring (required flags, unknown
  family rejected, no `-d`/`--all` accepted, both `--help` screens
  show `generate` and its flags without `--daemon`/`--all`). All
  pre-existing `mount` tests are untouched and still pass; this file
  went from 22 tests to 44.
- Manually smoke-tested the real (non-faked) `TagGenerator` end to
  end via `uv run aprilcam tags generate ...`: a valid `--ids 1-3`
  batch wrote 3 PNGs and printed a confirmation naming the count and
  output directory; an out-of-range id (`--ids 1-3,999` for `aruco`,
  whose dictionary tops out at 49) printed
  `error: aruco id 999 outside valid range 0..49` to stderr, exited 1,
  and created no output directory at all (SUC-002's whole-batch
  guarantee, verified live against real OpenCV, not just via the
  fake).
- `uv run mypy` is clean (77 source files, no errors). Zero
  `typing.Protocol` anywhere in this ticket's changes.
- Full suite: `uv run pytest -q -m "not camera and not integration"`
  -> 1233 passed, 49 deselected (1212 passing baseline noted in the
  ticket brief + 22 net new tests in this file, minus one that was
  reclassified -- no regressions, `mount`'s tests unmodified and
  green). The pre-existing camera/integration SIGABRT noted in the
  ticket brief was not investigated -- out of scope per instruction --
  and was not hit, since those markers were excluded from this run.
