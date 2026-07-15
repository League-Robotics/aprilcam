---
id: '005'
title: systemd unit --force and PrivateTmp documentation
status: open
use-cases:
- SUC-002
depends-on:
- '004'
github-issue: ''
issue: enforce-one-aprilcamd-daemon-per-machine.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# systemd unit --force and PrivateTmp documentation

## Description

Deployment-only ticket, closing out the sprint. Updates
`deploy/aprilcamd.service` so that a systemd-managed daemon always wins
over any other `aprilcamd` instance on the machine (boot, restart, or
`systemctl restart aprilcamd` should never be refused by the new
refuse-by-default guard from ticket 004), and documents the operational
constraint that makes the machine-global lock work at all under systemd.

Two small, independent changes to the unit file:

1. Add `--force` to `ExecStart` (currently `-m aprilcam.daemon --tcp
   --tcp-port 5280 --unix`, confirmed at `deploy/aprilcamd.service:30-33`
   — no `--force` today). Systemd's instance should always take over,
   consistent with "systemd wins" being the intended deployment invariant.
2. Add a comment documenting that **`PrivateTmp` must never be enabled**
   for this unit. `PrivateTmp=true` would give the systemd-managed daemon
   its own private `/tmp` namespace, meaning its
   `/tmp/aprilcamd.lock` would not be the same inode any other process on
   the host sees — silently defeating the entire machine-global lock
   mechanism this sprint builds. The unit file has no `PrivateTmp`
   directive today (confirmed), so this is a preventive comment against a
   plausible future "harden the service" edit, not a fix to an existing
   bug.

See `architecture-update.md` "What Changed" (`deploy/aprilcamd.service`
bullet) and the issue's "Risks / notes" section ("systemd `PrivateTmp` must
remain off").

## Acceptance Criteria

- [ ] `deploy/aprilcamd.service`'s `ExecStart` includes `--force` as an
      argument to `python -m aprilcam.daemon` (alongside the existing
      `--tcp --tcp-port 5280 --unix`).
- [ ] A comment is present in the `[Service]` section explicitly stating
      that `PrivateTmp` must stay off (unset/`false`) and explaining why
      (private `/tmp` would give the daemon its own
      `/tmp/aprilcamd.lock`, defeating the shared machine-global lock from
      ticket 002).
- [ ] No `PrivateTmp=true` (or any `PrivateTmp` directive at all) is
      introduced by this ticket — the absence is the correct state; the
      comment documents *why* it must stay absent.
- [ ] The rest of the unit file (directories, environment variables,
      `Restart=on-failure`, `RestartSec=5`, `[Install]` section) is
      unchanged — this ticket does not touch anything beyond `ExecStart`
      and the new comment.
- [ ] Manual verification note added to the ticket (or cross-referenced
      from the issue's existing "Verification (end-to-end)" section, which
      already specifies the manual steps): confirm on a real host (or
      documented as deferred to live Pi bring-up if hardware isn't
      available in this session) that `systemctl restart aprilcamd` when
      another `aprilcamd` is already running results in exactly one
      daemon process afterward.
- [ ] No test suite changes required (deployment file, not code) — but run
      `uv run pytest` to confirm this ticket introduces no regressions
      (expected: zero test changes, zero failures).

## Implementation Plan

### Approach

Two-line edit to a static config file plus a documentation comment. No code
paths are touched. This ticket is intentionally the last in the sequence so
that `--force` (ticket 004) already exists and is proven working before the
deployment artifact references it.

### Files to create/modify

- `deploy/aprilcamd.service`:
  - Change `ExecStart` (lines 30-33) from:
    ```
    ExecStart=/home/eric/.local/share/pipx/venvs/aprilcam/bin/python \
        -m aprilcam.daemon \
        --tcp --tcp-port 5280 \
        --unix
    ```
    to:
    ```
    ExecStart=/home/eric/.local/share/pipx/venvs/aprilcam/bin/python \
        -m aprilcam.daemon \
        --tcp --tcp-port 5280 \
        --unix --force
    ```
  - Add a comment above or within the `[Service]` section, near the
    existing directory-management comments (lines 11-16), e.g.:
    ```
    # IMPORTANT: do not set PrivateTmp=true on this unit. The one-daemon-
    # per-machine guard (sprint 018) relies on a shared, machine-global
    # lock file at /tmp/aprilcamd.lock (or $APRILCAM_DAEMON_LOCK). A
    # private /tmp namespace would give this daemon its own lock file,
    # silently defeating the guard and allowing a second daemon to run
    # undetected.
    ```
- No other files. If a deployment runbook exists (`deploy/README.md` per
  arch-014, if present in this repo — check with a quick `ls deploy/`), add
  a one-line cross-reference there too; if no such file exists yet, skip
  (not required by this sprint's scope).

### Testing plan

- No automated test applies to a systemd unit file in this repo's test
  suite (no hardware, no systemd in CI). Verification is manual, per the
  issue's existing end-to-end verification steps (start a daemon, attempt
  a second start, confirm refusal; `--force` takeover; single
  `pgrep`/`lsof` afterward) — reuse those steps against the updated unit
  file on a real Pi or dev host when available, rather than inventing new
  ones.
- Run `uv run pytest` (full suite) purely as a regression check that this
  ticket's file-only change didn't accidentally touch anything else.

### Documentation updates

- The comment added directly in `deploy/aprilcamd.service` *is* the
  primary documentation deliverable for this ticket.
- If `deploy/README.md` exists, add a short note under its deployment
  steps referencing the `--force`-always-on systemd behavior and the
  `PrivateTmp` constraint, so an operator reading the runbook doesn't need
  to open the unit file to learn this.
