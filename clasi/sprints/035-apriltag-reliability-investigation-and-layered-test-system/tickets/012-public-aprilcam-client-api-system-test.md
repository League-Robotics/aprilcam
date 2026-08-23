---
id: '012'
title: Public aprilcam.client API system test
status: open
use-cases: [UC-10]
depends-on: ['010']
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Public aprilcam.client API system test

## Description

Drives `aprilcam.client` directly, the way a robot program would — the
sanctioned surface named in `src/aprilcam/client/interfaces.py`'s module
docstring (`Discovery().connect()` → typed `Daemon`). This is
client-library-only coverage that does not exist above unit-test level
today, most notably for `put_annotation`/`replace_layer`.

Isolated roots + a real camera (via ticket 010's enumeration), same
isolation discipline as ticket 011.

## Acceptance Criteria

- [ ] `Discovery().connect()` succeeds against the isolated daemon and
      returns a working `Daemon`.
- [ ] `get_tags`/`get_tag`/`get_tag_history` all succeed and return
      well-typed results (`HasField()`-correct `None`s, per
      `client/DESIGN.md`'s discipline — not defaulted zeros).
- [ ] `get_frame` succeeds both raw and deskewed.
- [ ] One `stream_images` subscription is opened, receives at least one
      frame, and is cleanly closed (socket-close-is-unsubscribe,
      verified — no leaked subscriber on the daemon side after close).
- [ ] `register_tag`/`unregister_tag` round-trip correctly.
- [ ] `put_annotation` AND `replace_layer` are both exercised (this is
      the one place in the whole test suite that exercises them above
      unit level) — draw, verify present via `get_frame`'s embedded
      annotation set or `list_annotations`-equivalent client call, then
      clear.
- [ ] `get_guide` succeeds with no daemon dependency.
- [ ] Camera-dependent assertions (`get_frame`, `get_tags`, streaming)
      carry `@pytest.mark.camera`; daemon-only assertions (annotations,
      guide) carry `@pytest.mark.integration` only.
- [ ] Every write lands under the isolated roots — real
      `~/.config/aprilcam`/`~/.local/share/aprilcam` unchanged.

## Implementation Plan

**Approach**: Mirror ticket 011's isolated-session fixture (same roots,
same private-daemon pattern) but drive `aprilcam.client` directly instead
of going through MCP stdio — this is deliberately a second, independent
path into the same isolated daemon, proving the client library itself
(not just the MCP wrapper around it) works end to end.

**Files to create**:
- `tests/aprilcam/client/test_public_api_system.py`

**Testing plan**:
- Requires a real camera for the camera-dependent assertions; the
  annotation/guide assertions can run daemon-only.
- **Verification command**: `uv run pytest tests/aprilcam/client/test_public_api_system.py -v`

**Documentation updates**:
- None required; traceability filled in ticket 018's final sweep.
