---
id: '007'
title: Idle close, close_camera RPC and camera close CLI
status: done
use-cases:
- SUC-005
depends-on:
- '003'
github-issue: ''
issue: no-way-to-close-a-camera-without-restarting-the-daemon.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Idle close, close_camera RPC and camera close CLI

## Description

Release a device with no stream subscriber and no read for 60 s
(configurable, setting documented); never idle-close while a stream or
tag/detection loop is active. Add a `CloseCamera` RPC and
`aprilcam camera close <camera>` (refused while subscribed unless
`--force`; with force, subscribers are served the card/reopen path). A
CLOSED camera stays listed and reopens transparently on next use with the
full config (ticket 001). Reaper uses an injectable clock.

## Plan

- Files: proto + stubs, `daemon/service.py` (RPC, reaper, thin),
  held-camera close(), CLI camera close command, client library method.

## Acceptance Criteria

- [x] Idle device released after the timeout; timeout configurable.
- [x] No idle close while a subscriber or detection loop exists.
- [x] `camera close N` closes; refused with a clear message when subscribed unless `--force`.
- [x] Next frame request reopens and applies full config.

## Testing

- **Existing tests to run**: `uv run pytest tests/daemon tests/cli -q`
- **New tests to write**: reaper with fake clock, close refusal/force, reopen after close.
- **Verification command**: `uv run pytest tests/daemon tests/cli -q`
