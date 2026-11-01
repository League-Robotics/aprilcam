---
id: '002'
title: Presence watch covers held cameras (Linux sysfs source, drop and reopen)
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Presence watch covers held cameras (Linux sysfs source, drop and reopen)

## Description

Enumeration, not read failures, is the unplug signal (spike: `read()`
fails fast, `isOpened()` stays true, `camera list` stayed present
indefinitely). Add a `PresenceSource` behavioural base (raises
NotImplementedError) and a Linux implementation polling
`/sys/class/video4linux` every 0.5 s (no new dependency; chosen over
udev/netlink, see sprint Design Rationale). Extend `watch()` so it diffs
presence for held cameras too: key gone -> `drop()` the held camera and
mark it absent; key back (any node) -> `reopen()` with full config. The
watcher emits events to the service; keep DaemonService additions thin.

## Plan

- Files: `camera/interfaces.py` (PresenceSource), `camera/discovery.py`
  (watch, Linux source), `daemon/service.py` + `daemon/__main__.py` (wire
  events to held cameras).

## Acceptance Criteria

- [x] Held camera whose key disappears is dropped and reported absent within ~2 s with no reads.
- [x] Key reappearing on a different node triggers reopen with full config and the camera returns to PRESENT.
- [x] PresenceSource is a real base class (no Protocol); a fake source is used in tests.
- [x] Polling cost is negligible (listing only, no device opens).

## Testing

- **Existing tests to run**: `uv run pytest tests/camera tests/daemon -q -k "discovery or watch or service"`
- **New tests to write**: fake PresenceSource driving drop/reopen of a held camera; non-held cameras unchanged.
- **Verification command**: `uv run pytest tests/camera tests/daemon -q`
