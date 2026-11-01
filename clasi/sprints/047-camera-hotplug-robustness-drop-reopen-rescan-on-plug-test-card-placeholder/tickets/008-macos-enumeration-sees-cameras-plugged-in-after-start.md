---
id: '008'
title: macOS enumeration sees cameras plugged in after start
status: open
use-cases:
- SUC-006
depends-on:
- '002'
github-issue: ''
issue: cameras-plugged-in-after-daemon-start-are-invisible-until-restart-on-macos.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# macOS enumeration sees cameras plugged in after start

## Description

On darwin, enumeration from worker threads never sees main-run-loop
AVFoundation device notifications, so cameras plugged in after start are
invisible. Implement a darwin PresenceSource (behind the base from 002)
that enumerates on the main thread / pumps the main run loop; decide
between the two based on the daemon's thread layout and record the choice
in the code docstring. Held-camera drop/reopen then works via the common
watcher. Read the issue file for the diagnosis.

## Plan

- Files: `camera/discovery.py` or a darwin presence module, daemon
  startup wiring for the main thread. Hardware verification on gala is
  manual by the stakeholder and is NOT a close blocker; unit tests use
  a fake enumerator.

## Acceptance Criteria

- [ ] A camera plugged in after daemon start appears in `camera list` without restart (manual, gala).
- [ ] Unplug marks held cameras absent via the common watcher (manual).
- [ ] Non-darwin code paths and tests unaffected.

## Testing

- **Existing tests to run**: `uv run pytest tests/camera -q`
- **New tests to write**: darwin presence source with a fake main-thread enumerator.
- **Verification command**: `uv run pytest tests/camera -q`
