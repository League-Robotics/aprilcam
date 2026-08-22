---
id: '003'
title: SIGPIPE/BrokenPipeError handling at the CLI boundary
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: sprint-020-followups-socket-collision-and-cli-polish.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# SIGPIPE/BrokenPipeError handling at the CLI boundary

## Description

`aprilcam camera image <camera> --base64 | head` prints an unhandled
`BrokenPipeError` traceback instead of exiting quietly, the standard
Unix-tool behavior when a downstream consumer closes its read end
early. There is no SIGPIPE/BrokenPipe handling anywhere in the CLI
boundary today.

The fix belongs at exactly one seam: `cli/__init__.py`'s `main()`,
which every subcommand's `func(args)` already funnels through (per
`adapters.md`'s "adapters hold no authoritative state" rule — this
module is pure dispatch). Handling the error here, once, covers every
current and future subcommand's `print()` calls without touching any of
the ~20 individual call sites across `cli/*.py` — the anti-shotgun-surgery
choice recorded in `sprint.md`'s Architecture section.

## Acceptance Criteria

- [x] `aprilcam camera image <camera> --base64 | head -c 80` exits with
      no Python traceback on stderr (verified against a fake/mocked
      daemon in the test suite — this does not require live hardware).
- [x] The fix is implemented once, in `cli/__init__.py`'s `main()`
      (wrapping the `func(args)` dispatch and/or resetting `SIGPIPE` to
      default disposition at process start, whichever combination
      correctly handles both an in-flight write raising
      `BrokenPipeError` and Python's own known stderr-flush-on-exit
      noise for a broken stdout) — not duplicated at individual
      `print()` call sites in `cli/camera.py`, `cli/playfield.py`, etc.
- [x] The exit code for a broken-pipe termination is reasonable (matches
      conventional Unix tool behavior — non-zero, no traceback) and is
      asserted by a test.
- [x] Normal (non-piped, non-truncated) CLI output and exit codes for
      every existing subcommand are unaffected — full regression pass
      stays green.
- [x] `uv run mypy` clean.

## Implementation Plan

**Approach**: the standard Python idiom for a CLI tool that must behave
correctly when piped: catch `BrokenPipeError` around the `func(args)`
dispatch in `main()`, and additionally redirect/close `sys.stdout` and
exit via `os._exit` (or an equivalent low-level exit) rather than a
normal `sys.exit`, since a normal interpreter shutdown will itself try
to flush the now-broken stdout and can print a second, spurious error
during teardown. Reset `SIGPIPE` to `SIG_DFL` at the top of `main()` on
POSIX platforms as a belt-and-suspenders measure (guarded for
Windows, which has no `SIGPIPE`), consistent with how other
Unix-conventional CLI tools handle this.

**Files**:
- `src/aprilcam/cli/__init__.py` — `main()` gains the
  `BrokenPipeError` handling and, on POSIX, the `SIGPIPE` disposition
  reset.

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/
  test_cli_integration.py -v`, plus the full headless suite to confirm
  no regression to any subcommand's normal output/exit-code behavior.
- **New tests to write**: a test that invokes `main()` with stdout
  replaced by a pipe whose read end is closed early (or a mock that
  raises `BrokenPipeError` on write), asserting no traceback is printed
  and the process exits cleanly; a live subprocess test (headless,
  using a fake/mocked daemon backend) running `aprilcam camera image
  ... --base64 | head -c 80` end-to-end and asserting a clean exit,
  matching the exact scenario named in the issue.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none required in `docs/design/` proper — the
`cli/` subsystem `DESIGN.md` does not exist yet; ticket 008 documents
this behavior when it bootstraps that file.
