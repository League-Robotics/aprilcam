---
id: '001'
title: Camera lifecycle states and full-config re-apply on reopen
status: open
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Camera lifecycle states and full-config re-apply on reopen

## Description

Spike (buzzkill, 2026-11-01): after a replug the camera self-healed but
reopened at 640x480 with no stored config applied (config pins 1280x800 +
manual exposure), silently breaking playfield warps. Introduce explicit
lifecycle states on the held camera (PRESENT, ABSENT, CLOSED) with
`drop()` (close device, mark ABSENT) and `reopen()` (re-resolve index via
index_resolver, then re-apply the FULL stored config: frame size first,
then gated controls in stored order) as the single reopen path used by
self-heal too. The control watchdog and check_stall must skip non-PRESENT
cameras (no "hardware controls drifted" / "re-assert failed" spam while
absent). Value types are frozen kw_only dataclasses; no typing.Protocol.

## Plan

- Files: `src/aprilcam/camera/camera.py` (HeldCamera lifecycle, reopen,
  watchdog guard), `camera/interfaces.py` (state enum/value type), backends
  only if config apply needs a hook.
- Look at v1 archive only for ideas on control ordering quirks.

## Acceptance Criteria

- [ ] Lifecycle state is queryable; transitions PRESENT -> ABSENT -> PRESENT and PRESENT -> CLOSED -> PRESENT work.
- [ ] Every reopen (self-heal, explicit, post-absence) applies frame size first, then stored controls in order; frame size equals the configured size after reopen.
- [ ] Watchdog and stall checks do nothing for ABSENT/CLOSED cameras.
- [ ] Existing 5-failed-read self-heal now goes through the same reopen path.

## Testing

- **Existing tests to run**: `uv run pytest tests/camera -q`
- **New tests to write**: fake-backend tests for state transitions, config apply order and size on reopen, watchdog skip while absent.
- **Verification command**: `uv run pytest tests/camera -q`
