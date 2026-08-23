---
id: '002'
title: Orphaned daemon process detection and reporting
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Orphaned daemon process detection and reporting

## Description

Observed 2026-08-03: `daemon.lock` held the live pid while an orphaned
`aprilcam.daemon` process from hours earlier was still alive — not
holding the lock, not serving. Nothing in `daemon status`/`daemon stop`
currently detects or reports this state; both only ever look at the
lock file's holder (`daemon/lifecycle.py`'s `_read_holder`,
`client/lifecycle.py`'s `_lock_holder` probe). The flock mechanism
itself is already correct — the OS releases a held flock when its
process dies, so a genuinely-dead process is never mistaken for the
lock holder. The gap is a process that is *alive* but is not (or is no
longer) the lock holder: one that released the lock during a slow
shutdown but did not fully exit, or one that never completed
`start()`'s lock acquisition. That process is invisible today.

Add a best-effort process-table scan for other `python -m
aprilcam.daemon`-shaped processes not matching the current lock
holder's pid, surfaced through `daemon status` (both the server-side
`daemon/lifecycle.py` and the client-side `client/lifecycle.py`, which
is what the CLI's `daemon status` actually calls). Per this sprint's
Design Rationale (see `sprint.md`): **report, never auto-kill**. The
scan is name-matching, not a proof of identity, so acting on a false
positive (killing an unrelated process) is a worse failure than an
operator having to clean up a stale process by hand.

## Acceptance Criteria

- [x] `client/lifecycle.py`'s `status()` — the method `aprilcam daemon
      status` actually calls — additionally reports any process
      matching the daemon's spawn command (`python -m aprilcam.daemon`)
      that is running but is *not* the current lock holder's pid,
      distinctly labeled as a candidate orphan.
- [x] With only the real, lock-holding daemon present, `daemon status`
      output is unchanged from today — no false positives against the
      daemon's own process.
- [x] With no daemon running at all, output is unchanged from today (an
      empty process-table scan is not itself reported as anything).
- [x] The scan is best-effort: if process-table enumeration is
      unavailable or fails on some platform, `status()` degrades to
      today's lock-only behavior rather than raising.
- [x] No code path introduced by this ticket signals or kills any
      process — `stop()`'s existing behavior (signal only the pid the
      lock names) is unchanged.
- [x] `daemon status`'s orphan report is explicitly local-only: it does
      not participate in `-d remote`/`--all` fan-out, since a
      process-table scan has no meaning against a remote host (per the
      architecture self-review's noted clarification). The CLI help
      text / output makes this scope clear rather than silently
      omitting remote coverage.
- [x] `uv run mypy` clean.

## Implementation Plan

**Approach**: add a small, dependency-light process-table probe (e.g.
scanning `/proc` on Linux or shelling out to `ps` — match this
project's existing platform-handling conventions in
`camera/backends/uvc_util.py`/`camera/backends/libcamera.py` for how
subprocess/platform-specific probes are structured and made
test-injectable) as an overridable seam, mirroring `client/lifecycle.py`'s
existing constructor pattern (`spawn`/`lock_holder`/`socket_reachable`/
`kill` are already injectable callables — add the process-scan the same
way so it is unit-testable without touching the real process table).
Wire it into `status()` only; `start()`/`stop()` are unchanged by this
ticket.

**Files**:
- `src/aprilcam/client/lifecycle.py` — `status()` gains the orphan
  report; constructor gains an injectable `list_daemon_processes`-style
  seam (default: real implementation).
- `src/aprilcam/daemon/lifecycle.py` — server-side `status()` (used
  when the daemon reports its own view) may also surface this, if the
  ticket implementer finds a clean way to share the probe; the
  client-side surface is the one the acceptance criteria above require.
- `src/aprilcam/daemon/interfaces.py` — `DaemonStatus` (or a small
  addition to it) gains a field to carry the orphan report, if the
  existing dataclass shape doesn't already have room without breaking
  its frozen/kw_only contract (see `CLAUDE.md`'s interfaces rule:
  extend the real dataclass, do not add a `@property`).
- `src/aprilcam/cli/daemon.py` — `cmd_status` (or equivalent) prints the
  orphan report distinctly when present.

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/client/test_lifecycle.py
  tests/aprilcam/daemon/test_lifecycle_integration.py
  tests/aprilcam/cli/test_daemon_cli.py -v`.
- **New tests to write**: injected process-list seam returning a
  fabricated "other daemon-shaped process" distinct from the lock
  holder's pid — assert `status()` reports it distinctly; injected seam
  returning only the lock holder's own pid — assert no orphan reported
  (regression against false positives); injected seam raising/failing —
  assert `status()` still returns the lock-only view rather than
  raising; assert no code path in this ticket calls the injectable
  `kill` seam under any orphan-detected scenario.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none required in `docs/design/` proper — see
ticket 001's note; ticket 008 documents this once it bootstraps the
`daemon/`/`client/` `DESIGN.md` files, after this ticket lands.
