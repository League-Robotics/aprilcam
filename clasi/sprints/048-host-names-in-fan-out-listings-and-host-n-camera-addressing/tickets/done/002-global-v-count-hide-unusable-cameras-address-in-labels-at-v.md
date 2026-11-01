---
id: '002'
title: Global -v count, hide unusable cameras, address in labels at -v
status: done
use-cases:
- SUC-004
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: global-v-verbosity-and-hide-unusable-cameras.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Global -v count, hide unusable cameras, address in labels at -v

## Description

Add a repeatable root `-v` verbosity count. Use it to (a) show every camera in
`camera list` (default hides `usable=false` rows with a trailing note), (b) add
the address to fan-out labels, and (c) replace `camera calibrate`'s private
`-v/--verbose`. Depends on 001 because both change the `fan_out` label code.

## Implementation Plan

- `cli/_common.py`: `add_verbose_option(parser)` on the root parser
  (`-v/--verbose`, `action="count"`, `default=0`) next to `add_daemon_option`;
  call it from `cli/__init__.py`. Make `-v` also work after the subcommand
  (`camera list -v`) without a subparser default clobbering the root count
  (for example subparser copies with `default=argparse.SUPPRESS`, or a
  post-parse merge that sums both). Pick one approach and test both orders.
- `cli/calibrate.py`: remove its own `--verbose/-v` (line ~203); read
  `verbose >= 1` from the global count.
- `print_cameras(cameras, *, verbose=0)`: when `verbose < 1`, drop rows with
  `usable` false and print `  (N unusable camera(s) hidden; -v to show)` if
  any were hidden (singular for 1). All-hidden prints the note, not
  `(no cameras)`. Applies to `camera list` and `camera list --all`; NOT to
  `camera probe` (it reports discovery state).
- `fan_out()` label: at `verbose >= 1` append the address, e.g.
  `donald (192.168.4.60:5280):`; local: `gala (local):` unchanged (or with
  the socket path; keep it simple and document the choice).
- Count >= 2 sets the root logger to INFO, >= 3 to DEBUG, configured once in
  the CLI entry point (level wiring only, no new log statements).

## Acceptance Criteria

- [x] `aprilcam -v camera list` and `aprilcam camera list -v` are equivalent; `-vv` parses as count 2
- [x] Default `camera list` hides `usable=false` rows and shows the hidden-count note
- [x] `-v` shows all rows and no note
- [x] `camera calibrate -v` still gives the verbose pose report (via the global count); no duplicate flag
- [x] `--all` labels include the address at `-v`, not otherwise
- [x] `-vv` / `-vvv` set INFO / DEBUG on the root logger
- [x] `camera probe` output unchanged

## Testing

- **Existing tests to run**: `tests/aprilcam/cli` (notably test_camera_cli.py, test_calibrate_cli.py, test_main.py, test_common.py)
- **New tests to write**: parser tests for both `-v` positions and counts; `print_cameras` hidden/note/singular/all-hidden cases; `fan_out` label with and without `-v`; logging-level wiring
- **Verification command**: `uv run pytest tests/aprilcam/cli`
