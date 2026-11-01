---
id: '003'
title: 'Honest list and probe: device cache is not a subscriber table'
status: open
use-cases:
- SUC-001
- SUC-003
depends-on:
- '002'
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Honest list and probe: device cache is not a subscriber table

## Description

`DaemonService._cameras` is a device cache but probe treats it as "the
live-subscriber table" (`camera/discovery.py:238-256`, wired at
`daemon/__main__.py:114-121`) and skips every cached slug. Define "in
use" as a live stream subscriber or an in-flight read. Probe never skips
a camera merely because it is cached; for a camera actively streaming it
avoids re-opening the device but still reports true presence from
enumeration. `list_cameras` presence for held cameras comes from the
PresenceSource, not `held.state`.

## Plan

- Files: `daemon/service.py` (in-use predicate, list_cameras),
  `camera/discovery.py` (probe skip rule), `daemon/__main__.py` wiring.
- Subscriber/in-flight tracking should be a small explicit counter owned
  by the held camera or stream registry, not inferred from the cache.

## Acceptance Criteria

- [ ] A cached idle camera is probed normally.
- [ ] A streaming camera is not reopened by probe but reported with true presence.
- [ ] `camera list` reflects current enumeration for held cameras within ~2 s.
- [ ] Probe during a live stream does not disturb frames.

## Testing

- **Existing tests to run**: `uv run pytest tests/camera tests/daemon -q`
- **New tests to write**: probe with cached idle vs subscribed camera; list after fake unplug/plug.
- **Verification command**: `uv run pytest tests/camera tests/daemon -q`
