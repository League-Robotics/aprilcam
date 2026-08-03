---
id: '004'
title: Mobile-tag tools (register/unregister/list)
status: open
use-cases:
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: mcp-server-the-agent-facing-product.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Mobile-tag tools (register/unregister/list)

## Description

`mcp/tools/mobile_tags.py`: the smallest tool group in the sprint —
`register_tag`, `unregister_tag`, `list_tag_parameters`. Flat wrappers
over `Daemon.register_tag`/`unregister_tag`/`list_tag_parameters`, with
no business logic beyond constructing the `TagId`/`MountParameters`
values from flat tool arguments (`types.MountParameters`'s frame
convention — robot's own frame, `+x` forward, `+y` left — is carried
forward unchanged; this ticket does not reinterpret it).

Runs strictly after ticket 001 on the shared sprint branch (no
worktree parallel execution opted into this sprint); do not start
before 001 merges. Independent of tickets 002/003/005.

## Acceptance Criteria

- [ ] `register_tag(family, number, mount_x=0.0, mount_y=0.0,
      mount_z=0.0, mount_yaw_rad=0.0, size_cm=None)` -> builds a
      `TagId` + `MountParameters` and calls `Daemon.register_tag`.
      Upserts by tag — takes effect on the next detection burst, does
      not survive a daemon restart (by contract, UC-12).
- [ ] `unregister_tag(family, number)` -> calls
      `Daemon.unregister_tag`; unregistering an absent tag is a no-op,
      not an error (enforced daemon-side, verified here by asserting no
      exception is raised).
- [ ] `list_tag_parameters()` -> every currently registered tag's
      mount parameters, keyed by tag.
- [ ] No tool in this module does anything beyond shaping arguments and
      calling the one matching `Daemon` method — per `adapters.md`'s
      "adapters hold no authoritative state, and none of them does
      vision" rule extended to "none of them re-derives daemon logic."
- [ ] `uv run mypy` clean; zero `typing.Protocol`.
- [ ] Full suite green.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/mcp/ -v`
  plus the full headless suite.
- **New tests to write**: one test per tool against a fake `Daemon`,
  including the unregister-absent-tag no-op case and a
  `list_tag_parameters` test proving the returned mapping matches
  exactly what was registered.
- **Verification command**: `uv run pytest -m "not camera"` (CI), full
  `uv run pytest` plus `uv run mypy` on this development machine.

## Implementation Plan

**Approach**: single pass — three tools, no shared state with any
other ticket's tool group, no internal helpers beyond the
`TagId`/`MountParameters` construction shared across the three.

**Files to create**: `src/aprilcam/mcp/tools/mobile_tags.py`.

**Files to modify**: `src/aprilcam/mcp/server.py` (register this
tool group).

**Documentation updates**: none beyond docstrings.
