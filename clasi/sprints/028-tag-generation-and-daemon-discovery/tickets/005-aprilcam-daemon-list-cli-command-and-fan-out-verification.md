---
id: '005'
title: aprilcam daemon list CLI command and fan-out verification
status: done
use-cases:
- SUC-004
- SUC-005
- SUC-006
- SUC-007
depends-on:
- '004'
github-issue: ''
issue: tag-generation-and-daemon-discovery.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# aprilcam daemon list CLI command and fan-out verification

**Depends on**: 004 (`Discovery.find_all()` must be real before
listing more than one daemon means anything)
**Serves**: SUC-004, SUC-005, SUC-006 (the new `daemon list` command
itself), SUC-007 (verification only — see "Fan-out verification" below)

## Description

Two parts:

1. **`aprilcam daemon list`** — a new subcommand in `src/aprilcam/cli/
   daemon.py`, alongside the existing `start`/`stop`/`restart`/
   `status`. It lists every daemon `Discovery.find_all()` reports
   (local and remote), with camera count and linked playfields for each
   reachable one. Built entirely from `Discovery.find_all()` and the
   existing `Daemon.status()` RPC — **no new RPC method, no `.proto`
   change** (sprint architecture: "`daemon list`'s camera-count/
   playfield summary is built entirely from the existing `Daemon.
   status()` RPC").
2. **Fan-out verification** — this sprint is also the first time
   `find_all()` can genuinely return more than one daemon (ticket 004),
   which means every pre-existing `--all`-capable command
   (`camera list --all`, `camera probe --all`, `camera image --all`,
   `tags mount list --all`, ...) can, for the first time, actually
   exercise its multi-daemon path for real rather than only ever
   iterating a one-element tuple. Add a test proving this end to end
   using `cli/_common.py`'s existing, **unmodified** `fan_out()` helper
   against a genuinely-discovered second daemon, one of the two
   reachable and one not (SUC-007). Do not modify `fan_out()` itself or
   any existing `--all` command — this is verification, not new
   production code, for that half of the ticket.

**Reuse `fan_out()` for `daemon list` itself, do not reinvent it.**
`fan_out()` (`src/aprilcam/cli/_common.py:169-217`) already implements
most of what SUC-005/SUC-007 need: prints `"(no daemons found)"` and
returns `EXIT_OK` on an empty `find_all()` result (SUC-005); aborts the
whole command via `report_error` on `DiscoveryFailed` (SUC-006); labels
each row `f"{info.host} (local)"` for the local entry and bare
`info.host` for remote ones; catches a per-daemon connect/RPC failure
and prints `"{label}: unreachable -- {exc}"` without aborting the rest
(this is exactly SUC-004's "a daemon that advertises but does not
answer RPCs is listed and flagged, not filtered out" — no special-case
code is needed for it, `fan_out()` already does it); returns
`EXIT_ERROR` if any discovered daemon failed to answer, `EXIT_OK`
otherwise (including the empty case). `cmd_list` should be a thin call
into `fan_out(discovery, lambda daemon: daemon.status(), render)` where
`render` formats one daemon's `DaemonStatus` — camera count and linked
playfields — exactly matching `cli/tags.py`'s existing `cmd_list`
(mount list)'s `fan_out(...)` usage as the pattern to follow.

**`daemon list` takes no `-d`/`--all` flags.** Unlike `mount list`
(which defaults to single-target and opts into fan-out via `--all`),
listing daemons is inherently "every daemon" — there is no
single-target meaning for it (that is what `daemon status -d HOST`
already does). Do not add `add_daemon_option`/`add_all_option` to this
subparser.

## Files to touch

- `src/aprilcam/cli/daemon.py` — new `cmd_list` + subparser wiring. Its
  module docstring currently states *"**No `--all` here.**... there is
  no `daemon list` yet..."* — update that section now that `list`
  exists (see Documentation updates below).
- `tests/aprilcam/cli/test_daemon_cli.py` (existing) — add `list`
  coverage, mirroring its existing fake-`Discovery`-driven style.
- A fan-out-verification test exercising an existing `--all` command
  (e.g. `camera list --all`) against a real multi-daemon `find_all()` —
  add to whichever existing test module already covers that command's
  `--all` path (grep for it first, e.g. `tests/aprilcam/cli/
  test_camera_cli.py` or `test_cli_integration.py`) rather than
  creating a new file, unless none of them are structured for a real
  two-daemon fixture, in which case a small new integration test module
  is fine.

## Acceptance Criteria

- [x] `aprilcam daemon list` prints every daemon `find_all()` reports;
      the local one is labeled `(local)` (via `fan_out()`'s existing
      labeling — no new labeling code needed).
- [x] Each **reachable** daemon's row shows camera count and its linked
      playfields (derived from `DaemonStatus.cameras` — count is
      `len(status.cameras)`; linked playfields are the deduplicated,
      non-`None` `camera.playfield` values across that daemon's
      cameras, printed `(none)` if there are none, matching this
      codebase's established `(no X)`/`(none)` convention rather than
      blank output).
- [x] SUC-004: verified with two local daemons on alternate ports (per
      Hardware Constraints) — both appear; the local one is flagged
      local; each reachable one's row shows camera count and linked
      playfields.
- [x] SUC-005: no daemons anywhere → `(no daemons found)`, exit code 0
      (via `fan_out()`, unmodified).
- [x] SUC-006: a broken discovery (`DiscoveryFailed`, simulated via a
      fake `Discovery.find_all` that raises, or the `browse=` DI seam
      from ticket 004 through a real `Discovery` instance) produces a
      message **visibly distinct** from SUC-005's `(no daemons found)`,
      non-zero exit (via `fan_out()`/`report_error`, unmodified).
- [x] A daemon that advertises but does not answer (e.g. TCP bound but
      the gRPC service is not actually up, or the process died between
      advertise and connect) is listed, flagged unreachable, not
      silently dropped (via `fan_out()`'s existing per-entry
      try/except, unmodified).
- [x] `aprilcam daemon list --help` shows no `-d`/`--all` options.
- [x] **Fan-out verification (SUC-007)**: with two daemons discovered
      (one reachable, one not — e.g. stopped mid-session or never bound
      TCP), an existing `--all` command (`camera list --all` is the
      simplest to reuse) exits non-zero (partial failure) while still
      printing the reachable daemon's full results — using the real,
      now-multi-daemon `find_all()` from ticket 004, not a fake. This
      test changes no production code; it is proof that ticket 004's
      change composes correctly with every pre-existing `--all`
      command, not just the new `daemon list` one.
  - [x] No existing single-local-daemon `--all` fan-out test's expected
        behavior changes (confirm by running the existing suite for
        every `--all`-capable command's test file).
- [x] `uv run mypy` clean on `cli/daemon.py`. Zero `typing.Protocol`.
- [x] Full existing suite stays green, including every existing
      `cli/daemon.py` test (`start`/`stop`/`restart`/`status`,
      unmodified).

## Completion Notes

Implemented exactly as planned. `cmd_list` is a five-line thin call into
`fan_out()` plus a `_render_daemon_list_row` closure; no change to
`fan_out()` itself, `camera_cli.cmd_list`, or any other existing
`--all` command's production code.

- **`cmd_list` takes an `args` parameter it never reads.** Kept for
  signature uniformity with every other `cmd_*` function in this file
  (`cli/__init__.py` dispatches `func(args)` uniformly) rather than
  special-casing this one command's call site. Documented in the
  function's own docstring so it doesn't read as an oversight.
- **Playfield rendering**: `sorted({c.playfield for c in status.cameras
  if c.playfield is not None})`, joined with `", "`, or the literal
  `"(none)"` when empty — verified with a dedup+sort unit test
  (duplicate and out-of-order playfield names collapse to one sorted,
  comma-joined line).
- **Module docstring** updated in place: the historical "No `--all`
  here" reasoning for `start`/`stop`/`restart`/`status` is kept
  verbatim (not deleted), with a new paragraph recording that `list`
  is sprint 028's addition and why it structurally cannot take
  `-d`/`--all` (no single-target meaning — that's what `daemon status
  -d HOST` already does).
- **Test additions, three tiers**, mirroring this codebase's existing
  layering:
  1. Six fake-`Discovery`-driven unit tests in `test_daemon_cli.py`
     covering every acceptance criterion at the `cmd_list` level
     directly (reachable local+remote with camera count/playfields,
     `(none)` marker, dedup+sort, unreachable-flagged-not-dropped,
     empty/SUC-005, `DiscoveryFailed`/SUC-006), plus four argparse
     wiring tests (`list` has no `.daemon`/`.all` attribute after
     parsing; `-d` and `--all` are rejected at parse time with
     argparse's own exit code 2; `--help` output contains neither
     `--daemon` nor `--all`).
  2. One real, two-local-daemon-alternate-port `@pytest.mark.
     integration` test in `test_daemon_cli.py`
     (`test_daemon_list_reports_two_real_local_daemons_on_alternate_ports`),
     satisfying SUC-004's own "verified via two local processes on
     alternate ports" acceptance criterion for `daemon list` itself —
     not just for raw `Discovery.find_all()`, which ticket 004 already
     proved. Reuses the exact real-component technique
     `tests/aprilcam/client/test_discovery.py`'s own real two-daemon
     test established (`daemon.__main__.build()`, real TCP bind, a
     thin `MdnsAdvertiser` wrapper baking in a synthetic second
     hostname, real default `Discovery` browse) rather than inventing
     a second fixture mechanism — no shared conftest fixture exists in
     this codebase for this, so, matching every other integration test
     module here, the technique is reproduced locally rather than
     imported across test modules.
  3. One real, `find_all()`-is-genuinely-real fan-out-verification test
     in `test_cli_integration.py`
     (`test_camera_list_all_against_a_real_multi_daemon_find_all_reports_partial_failure`),
     proving SUC-007 against `camera list --all` end to end. This is
     new: `test_cli_integration.py` already had a two-real-daemon
     `--all` fan-out test, but it predates ticket 004 and necessarily
     used a hand-built `_TwoRealDaemonDiscovery` stand-in (`find_all()`
     was still stubbed local-only when that test was written) — it is
     left completely unmodified per this ticket's own acceptance
     criterion ("no existing single-local-daemon fan-out test's
     expected behavior changes"). The new test instead constructs the
     "one daemon unreachable" half by advertising a genuine mDNS record
     for an OS-assigned ephemeral TCP port with nothing ever bound
     behind it (grab-then-release-immediately), so the real browse
     finds a real, resolvable record whose connect must genuinely fail
     — rather than stopping a real daemon process, which risks the
     mDNS "goodbye" packet racing the subsequent browse and
     nondeterministically removing the very advertisement the test
     needs `find_all()` to still report.
- **Honest gap**: same as ticket 004's own honest gap, inherited
  unchanged — every "two daemons" scenario here (both `daemon list`'s
  own SUC-004 test and the SUC-007 fan-out test) uses two real daemon
  *processes* on this one physical dev machine, per sprint.md's
  documented hardware-blocked stand-in. Neither test, and nothing in
  this ticket, proves genuine multicast traversal across a LAN to a
  second physical host — that remains ticket 006's documentation
  responsibility, not this ticket's to close.
- No file outside this ticket's declared scope (`cli/daemon.py`,
  `tests/aprilcam/cli/test_daemon_cli.py`,
  `tests/aprilcam/cli/test_cli_integration.py`) was touched.

**Verification performed**: `uv run pytest -q tests/aprilcam/cli/test_daemon_cli.py -m "not camera and not integration"`
(27 passed); `uv run pytest -q tests/aprilcam/cli/test_daemon_cli.py -m integration`
(1 passed, ~7s); `uv run pytest -q tests/aprilcam/cli/test_cli_integration.py -m integration`
(3 passed, ~26s, includes the two pre-existing integration tests in that
file, unmodified and still green); `uv run pytest -q -m "not camera and not
integration"` (full suite: 1274 passed, up from the 1264 baseline recorded
at ticket 004 — exactly the 10 new unit-level tests added here); `uv run
pytest -q -m integration` (full suite: 8 passed); `uv run mypy` (clean,
78 source files). Manually confirmed `daemon list --help` shows only
`-h`/`--help`, `daemon --help` lists `list` alongside the four existing
subcommands, and a live `daemon list` against an isolated, empty
`XDG_RUNTIME_DIR` prints `(no daemons found)` and exits 0.

## Implementation Plan

**Approach:**
1. Add `cmd_list(args, *, discovery_factory: Callable[[], Discovery] =
   Discovery) -> int` to `cli/daemon.py`, following the module's
   existing style (`cmd_status`'s use of `discovery_factory` is the
   closest existing precedent in this same file). Build a small
   `render(label, status)` closure that prints camera count and linked
   playfields, then call `fan_out(discovery_factory(), lambda daemon:
   daemon.status(), render)` and return its result directly.
2. Add the `p_list` subparser in `add_subparser` — no
   `add_daemon_option`/`add_all_option` calls for it.
3. Update the module docstring's `"No --all here"` paragraph to record
   that `list` is the new, inherently-fan-out command this sprint adds,
   without deleting the historical reasoning for why the other four
   commands stay single-target/no-`--all` (this codebase's "record
   decisions in place" convention).
4. Write `test_daemon_cli.py` coverage for `cmd_list` against a fake
   `Discovery` (mirroring the existing fakes in that file) — no real
   daemon needed for the unit-level tests.
5. Write the fan-out-verification test: two real daemon instances
   (ticket 003's stack, ticket 004's real `Discovery`), one made
   unreachable after being discovered (e.g. its listener stopped), and
   confirm `camera list --all`'s existing command function returns the
   partial-failure exit code with the reachable daemon's data intact.

**Files to create/modify:**
- `src/aprilcam/cli/daemon.py`
- `tests/aprilcam/cli/test_daemon_cli.py`
- The fan-out-verification test (existing `--all`-command test module,
  or a small new one — see Files to touch above)

**Testing plan:**
- `cmd_list` itself: headless, fake-`Discovery`-driven, matching
  `test_daemon_cli.py`'s existing style exactly (no new pattern
  introduced).
- SUC-004's own acceptance criterion ("verified via two local
  processes on alternate ports") needs one real, two-daemon test for
  `daemon list` specifically, reusing ticket 004's real integration
  fixture/setup rather than duplicating it.
- The fan-out-verification test is deliberately about an **existing**
  command, not `daemon list` itself — it is the proof that this
  sprint's change to `find_all()` is a genuine drop-in for every
  caller, per the sprint architecture's "Impact on existing components"
  claim.

**Documentation updates:** `cli/daemon.py`'s module docstring, as
described above.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/
  test_daemon_cli.py` and the full suite, including every existing
  `--all`-capable command's test file (confirm none regress).
- **New tests to write**: `cmd_list` unit tests (empty, single-local,
  local+remote-reachable, local+remote-unreachable, discovery-failed);
  one real two-daemon `daemon list` test; one real two-daemon fan-out-
  verification test against an existing `--all` command.
- **Verification command**: `uv run pytest && uv run mypy`
