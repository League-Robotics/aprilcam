---
id: '010'
title: 'Docs: DESIGN updates, UC-25 coverage, knowledge entry'
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
depends-on:
- 008
- 009
github-issue: ''
issue:
- camera-hotplug-rock-solid-drop-reopen-and-test-card.md
- no-way-to-close-a-camera-without-restarting-the-daemon.md
- cameras-plugged-in-after-daemon-start-are-invisible-until-restart-on-macos.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Docs: DESIGN updates, UC-25 coverage, knowledge entry

## Description

Bring docs in line with what shipped. Update `camera/DESIGN.md`
(lifecycle states, presence source, reopen config re-apply) and
`daemon/DESIGN.md` (remove "No OS hotplug event source exists"; device
cache vs subscribers; test card; idle close; CloseCamera). Update UC-25
S-3/S-5/S-7 coverage in the use-case docs. Add `docs/knowledge/` entry:
unbind/bind hotplug simulation, V4L2 read fails fast / isOpened stays
true, reopen-at-640x480 pitfall. Document the `camera image` exit-code
choice and the idle-timeout setting in CLI/config docs.

## Acceptance Criteria

- [x] DESIGN docs no longer claim there is no hotplug event source and describe the new lifecycle.
- [x] UC-25 S-3/S-5/S-7 reference this sprint's behaviour.
- [x] Knowledge entry added and linked from where setup is documented.
- [x] CLI/config docs mention exit code 3, `camera close`, idle timeout.

## Testing

- **Existing tests to run**: none required (docs only); `uv run pytest -q -k docs` if a docs check exists.
- **New tests to write**: none.
- **Verification command**: `grep -rn "No OS hotplug" docs src`  (expect no hits)
