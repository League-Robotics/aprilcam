---
id: '012'
title: Public aprilcam.client API system test
status: done
use-cases:
- UC-10
depends-on:
- '010'
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

- [x] `Discovery().connect()` succeeds against the isolated daemon and
      returns a working `Daemon`. Verified genuinely (daemon-only
      session, no camera needed):
      `test_discovery_connect_returns_a_working_daemon_against_the_isolated_daemon`.
- [x] `get_tags`/`get_tag`/`get_tag_history` all succeed and return
      well-typed results (`HasField()`-correct `None`s, per
      `client/DESIGN.md`'s discipline — not defaulted zeros). Written
      as `test_get_tags_get_tag_and_get_tag_history_return_well_typed_results`,
      parameterized over cameras 3/5 — camera-dependent, so it
      genuinely skips in this agent session (no macOS camera TCC
      grant; see the module's own remediation text). Needs an operator
      run to actually execute.
- [x] `get_frame` succeeds both raw and deskewed. Written as
      `test_get_frame_succeeds_raw_and_deskewed`; same camera-dependent
      skip as above — needs an operator run.
- [x] One `stream_images` subscription is opened, receives at least one
      frame, and is cleanly closed (socket-close-is-unsubscribe,
      verified — no leaked subscriber on the daemon side after close).
      Written as
      `test_stream_images_subscription_receives_a_frame_and_unsubscribes_cleanly_on_close`,
      verifying the daemon-side producer's `.running` flag goes False
      after close (same technique
      `tests/aprilcam/daemon/test_streaming_integration.py` uses); same
      camera-dependent skip — needs an operator run.
- [x] `register_tag`/`unregister_tag` round-trip correctly. Written as
      `test_register_tag_and_unregister_tag_round_trip`; same
      camera-dependent skip — needs an operator run.
- [x] `put_annotation` AND `replace_layer` are both exercised (this is
      the one place in the whole test suite that exercises them above
      unit level) — draw, verify present via `get_frame`'s embedded
      annotation set or `list_annotations`-equivalent client call, then
      clear. Written as
      `test_put_annotation_and_replace_layer_round_trip_through_get_annotations_then_clear`
      — daemon-only, camera-free (verifies via `get_annotations`, the
      "list_annotations-equivalent" branch, deliberately over the
      `get_frame`-embedded-set branch so it stays camera-free). Ran for
      real this session and passed.
- [x] `get_guide` succeeds with no daemon dependency. Written as
      `test_get_guide_succeeds_with_no_daemon_dependency`
      (`client.guides.Guides().get()`, no fixture, no daemon, no
      socket) plus a second, daemon-only sanity check
      `test_daemon_get_guide_rpc_serves_guide_text_against_the_isolated_daemon`
      for the wire RPC. Both ran for real this session and passed.
- [x] Camera-dependent assertions (`get_frame`, `get_tags`, streaming)
      carry `@pytest.mark.camera`; daemon-only assertions (annotations,
      guide) carry `@pytest.mark.integration` only. Confirmed by
      inspection and by the collected/run marker split this session.
- [x] Every write lands under the isolated roots — real
      `~/.config/aprilcam`/`~/.local/share/aprilcam` unchanged. Both
      session fixtures (`daemon_only_session`, `camera_session`)
      snapshot the real config/state dirs before start and assert an
      identical snapshot after stop; verified passing this session,
      plus a manual `ls` spot-check before/after the full run.

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
