---
id: '006'
title: End-to-end integration and acceptance
status: open
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
depends-on:
- '002'
- '003'
- '004'
- '005'
github-issue: ''
issue: mcp-server-the-agent-facing-product.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# End-to-end integration and acceptance

## Description

The sprint's acceptance ticket and the last one in serial execution
order — by the time it completes, the issue's full Verification
section is satisfied, which is why it carries `completes_issue: true`
(every other ticket in this sprint carries `completes_issue: false`).

Runs last: depends on all four tool-group tickets (002-005), each of
which individually depends only on 001. This sprint runs strictly
serially (001 -> 002 -> 003 -> 004 -> 005 -> 006) on the shared branch
— no worktree parallel execution was opted into, so even though 002-005
are mutually independent in principle, concurrent dispatch against the
same branch risks real file conflicts (all four register into the same
`mcp/server.py`). Do not dispatch 002-005 concurrently.

Proves the tools built in 001-005 compose into one real MCP session and
that the sprint's cross-cutting properties hold across the whole
package, not just per-module: an in-process MCP client session (the
`mcp` SDK's in-memory client/server transport, no subprocess needed)
drives the full golden path from the issue's Verification section
against a fake daemon; an `@camera`-marked variant repeats it against
the real daemon and camera 3's calibrated `main-playfield` baseline
(~0.18cm reprojection error). Also verifies the late-binding property
(UC-22.S-2, UC-22.S-3) against a **real** daemon process — ticket 001's
unit tests exercise the connection manager against a fake, this ticket
is what proves it against the genuine restart behaviour.

## Acceptance Criteria

- [ ] In-process session (fake daemon), no shell access, all via MCP
      tool calls: `get_guide` -> `list_cameras` -> `set_playfield` ->
      `set_camera_config` -> `calibrate` -> `get_tags` ->
      `get_frame(deskewed=True, format="base64")` -> `draw_marker` ->
      `list_annotations` (finds the marker just drawn) -> `clear_layer`.
- [ ] `@camera`-marked variant of the same session against a real
      daemon and camera 3 — matches the issue's Verification section
      exactly ("orient via the guide, list cameras, calibrate, read
      tags in world coordinates, fetch a deskewed frame, draw a marker,
      and read it back").
- [ ] Server starts with **no daemon running**; `get_guide` succeeds;
      a perception tool call fails with a tool-level `ConnectionFailed`
      error, not a server crash (UC-22.S-2); starting a daemon and
      retrying the identical call **in the same session** succeeds.
- [ ] Daemon restart mid-session, against a real daemon process: the
      in-flight call fails; the *next* call reconnects and succeeds
      (UC-22.S-3) — this is what proves ticket 001's connection-manager
      eviction path against a genuine restart, not only a fake.
- [ ] `cv2` absent from `sys.modules` after importing every `mcp/`
      module — a full-package sweep, not just the single-module check
      ticket 001 added to `tests/test_no_cv2_in_client_modules.py`.
- [ ] `uv run mypy` clean across the whole `mcp/` package.
- [ ] Full suite green: baseline 976 tests passing (headless + hardware)
      plus every test tickets 001-006 added.
- [ ] `AGENT_GUIDE.md` / packaged guide content reviewed and updated if
      it still describes `aprilcam mcp` as unavailable or missing —
      verify first; edit only if actually stale.

## Testing

- **Existing tests to run**: full `uv run pytest` (headless) and
  `uv run pytest -m camera` (hardware) suites — this ticket's job is
  proving no regression across the *entire* sprint, not just its own
  new code.
- **New tests to write**: the in-process golden-path session against a
  fake daemon; the `@camera` hardware variant of the same session; the
  no-daemon-at-startup / daemon-restart-mid-session resilience tests
  (the second against a real daemon process, started/stopped by the
  test); the full-package no-cv2-in-`sys.modules` sweep.
- **Verification command**: `uv run pytest -m "not camera"` (CI), then
  full `uv run pytest` plus `uv run mypy` on this development machine
  before considering the sprint's issue closed.

## Implementation Plan

**Approach**: this ticket is verification-only for tool *logic* —
tickets 001-005 already unit-test each tool against a fake `Daemon`
individually. This ticket's job is composition: prove the tools work
together in one real session, and that the sprint-wide properties
(late binding against a real restart, no-cv2 across the whole package,
mypy across the whole package, guide-content accuracy) hold at the
package level, not per-module.

**Files to create**: `tests/aprilcam/mcp/test_integration.py` (or
equivalent), containing both the fake-daemon and `@camera` sessions.

**Files to modify**: `AGENT_GUIDE.md`, only if review finds it stale.

**Documentation updates**: `AGENT_GUIDE.md`, conditional on the review
above.
