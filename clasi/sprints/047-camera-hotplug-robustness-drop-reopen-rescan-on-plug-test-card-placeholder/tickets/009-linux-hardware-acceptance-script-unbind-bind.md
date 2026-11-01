---
id: '009'
title: Linux hardware acceptance script (unbind/bind)
status: open
use-cases:
- SUC-001
- SUC-002
- SUC-004
depends-on:
- '006'
- '007'
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Linux hardware acceptance script (unbind/bind)

## Description

Hard gate on Linux (buzzkill or donald). Script under `scripts/` (and a
pytest `hardware` marker wrapper) that simulates plug/unplug with
`echo <usb-id> > /sys/bus/usb/drivers/usb/unbind` / `bind` (needs root;
usage documented) and, while a viewer/stream runs, asserts: camera absent
in `camera list` within ~2 s; test card served with placeholder flag;
real frames at the configured size within ~3 s of bind; stored config
(exposure, frame size) applied; daemon not restarted; idle close and
`camera close` reopen path also exercised. Run it on hardware and record
the result in the ticket.

## Plan

- Files: `scripts/hotplug_acceptance.py`, marker registration in pytest
  config, short usage note.

## Acceptance Criteria

- [ ] Script exits 0 on buzzkill (or donald) with all assertions above passing; output pasted into the ticket.
- [ ] Skipped automatically when no hardware / not root / not Linux.
- [ ] Default test suite does not run it.

## Testing

- **Existing tests to run**: `uv run pytest -q -m "not hardware" tests/daemon`
- **New tests to write**: the hardware acceptance script itself (run manually on buzzkill).
- **Verification command**: `ssh buzzkill` then run `scripts/hotplug_acceptance.py --camera <N> --usb-id <id>`
