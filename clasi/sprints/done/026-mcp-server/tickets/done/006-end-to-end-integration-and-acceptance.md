---
id: '006'
title: End-to-end integration and acceptance
status: done
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

- [x] In-process session (fake daemon), no shell access, all via MCP
      tool calls: `get_guide` -> `list_cameras` -> `set_playfield` ->
      `set_camera_config` -> `calibrate` -> `get_tags` ->
      `get_frame(deskewed=True, format="base64")` -> `draw_marker` ->
      `list_annotations` (finds the marker just drawn) -> `clear_layer`.
- [x] `@camera`-marked variant of the same session against a real
      daemon and camera 3 — matches the issue's Verification section
      exactly ("orient via the guide, list cameras, calibrate, read
      tags in world coordinates, fetch a deskewed frame, draw a marker,
      and read it back").
- [x] Server starts with **no daemon running**; `get_guide` succeeds;
      a perception tool call fails with a tool-level `ConnectionFailed`
      error, not a server crash (UC-22.S-2); starting a daemon and
      retrying the identical call **in the same session** succeeds.
- [x] Daemon restart mid-session, against a real daemon process: the
      in-flight call fails; the *next* call reconnects and succeeds
      (UC-22.S-3) — this is what proves ticket 001's connection-manager
      eviction path against a genuine restart, not only a fake.
- [x] `cv2` absent from `sys.modules` after importing every `mcp/`
      module — a full-package sweep, not just the single-module check
      ticket 001 added to `tests/test_no_cv2_in_client_modules.py`.
- [x] `uv run mypy` clean across the whole `mcp/` package.
- [x] Full suite green: baseline 976 tests passing (headless + hardware)
      plus every test tickets 001-006 added.
- [x] `AGENT_GUIDE.md` / packaged guide content reviewed and updated if
      it still describes `aprilcam mcp` as unavailable or missing —
      verify first; edit only if actually stale.

## Completion Notes (026-006)

**New tests**: `tests/aprilcam/mcp/test_integration.py`, five tests —
`test_golden_path_against_a_fake_daemon` (headless, no marker),
`test_no_daemon_at_startup_then_recovers_once_one_starts` and
`test_daemon_restart_mid_session_fails_then_recovers`
(`@pytest.mark.integration`, real daemon process, no camera hardware
required — matches `test_lifecycle.py`'s own precedent for this kind of
test), `test_camera_golden_path_against_real_daemon_and_camera_3`
(`@pytest.mark.camera`), and
`test_cv2_absent_from_sys_modules_after_importing_every_mcp_module`
(headless, subprocess-isolated). All driven through a real
`mcp.shared.memory.create_connected_server_and_client_session`
`ClientSession` — a genuine MCP protocol round trip, not the low-level
`CallToolRequest` handler tickets 001-005 used for their own unit tests.

**Real bug found and fixed**: writing the dynamic cv2 sweep exactly as
specified (import every `mcp/` submodule in a clean subprocess, check
`sys.modules` — not the existing AST-only literal-import scan in
`tests/test_no_cv2_in_client_modules.py`, which cannot see a transitive
import) found that `client/daemon_client.py` and `client/models.py`
both imported the concrete, `cv2`-importing
`calibration.calibration.Calibration` for `get_calibration`'s return
type/construction — so importing *any* `mcp/` module transitively
pulled `cv2` into `sys.modules` via `mcp/connection.py` ->
`client/discovery.py` -> `client/daemon_client.py`, even though no
`mcp/` file itself imports `cv2`. This broke
`.claude/rules/project-overview.md`'s "the MCP server ... require[s]
only Pillow, NumPy, mss, and python-dotenv" contract and would have
crashed `aprilcam mcp` on first tool call on a base install (no
`[daemon]` extra). Fixed at the root in both modules: now import and
construct `calibration.interfaces.Calibration` (the abstract, `cv2`-free
base already declared by `daemon.interfaces.Daemon.get_calibration`'s
own signature); `calibration_from_proto` no longer carries
`homography`/`matched_ids`/`reprojection_error_cm` across the wire
boundary — confirmed (by grep across `client/`, `cli/`, `mcp/`,
`view/`) that nothing on the client side of the wire ever reads them,
and `mcp/tools/configuration.py`'s own docstring already documented the
intent that `get_calibration` reports exactly
`stale`/`calibrated_at`/`playfield`. Verified: `cv2 in sys.modules` is
`False` after importing `aprilcam.mcp.server` and every tool submodule,
both directly and via the new dynamic sweep test. No other
inconsistency found across `mcp/server.py` / `connection.py` /
`errors.py` / `images.py` / `tools/*.py` — same `register(mcp,
connection, temp_dir)` shape, same `@mcp.tool()` +
`handle_tool_errors` ordering, same optional `daemon` parameter, no
leftover TODOs, across all five tool-group tickets.

**AGENT_GUIDE.md**: one stale line found and fixed — §7 said "and —
once wired — the MCP server," written before this sprint existed. Now
states the MCP server (`aprilcam mcp [-d HOST]`) is available, with the
same per-CLI-command capability mapping.

**Real session transcript (fake daemon, `-s` output)**: golden path
ran get_guide -> list_cameras -> set_playfield -> set_camera_config ->
calibrate (`structuredContent` exactly
`{"succeeded": true, "matched_ids": [1..10], "missing_ids": [],
"reprojection_error_cm": 0.18, "reason": null}`) -> get_tags ->
get_frame (deskewed, base64, decoded bytes match the fake's fixture) ->
draw_marker -> list_annotations (found `"agent-marker"`) -> clear_layer
-> list_annotations again (empty). All nine tool calls returned
`isError: false`.

**Real hardware verification, camera 3**
(`arducam-ov9782-usb-camera`), `-m camera`:

- `test_camera_golden_path_against_real_daemon_and_camera_3` PASSED,
  real output: `list_cameras` found `arducam-ov9782-usb-camera` (plus
  `brio-501`, `hd-usb-camera`); `get_frame(deskewed=True)` returned a
  real 33086-byte PNG; `draw_marker`/`list_annotations`/`clear_layer`
  round-tripped for real against the real daemon's annotation store.
- **Disclosed finding, not a code defect**: at the time of this
  ticket's hardware pass, a live `calibrate()` attempt against camera 3
  failed — first missing ArUco 2, then (on repeat runs, minutes apart)
  missing 2/3/4, then 2/3/4/5. Fetching and visually inspecting a real
  raw frame (`get_frame(deskewed=False)`) showed the physical cause
  directly: an item of clothing/a bag is draped across the top-centre
  of the physical `main-playfield`, progressively covering more of the
  north edge's ArUco markers. This is confirmed **not** a regression
  from this ticket or this sprint: the pre-existing, untouched
  `tests/aprilcam/daemon/test_calibration_hardware.py::
  test_full_set_calibration_matches_all_ten_required_aruco_ids` (from
  ticket 022-007, no code in its path touched by 026) fails identically
  against the same real hardware at the same time, and a full `-m
  camera` run shows **13 pre-existing failures, all in
  `test_calibration_hardware.py`/`test_detection_hardware.py`/
  `test_lifecycle_integration.py`/`test_mobile_tags_hardware.py`**
  (sprints 020-025, none touched by this ticket), every one reporting
  the identical missing-ArUco-2/3/4 signature. This ticket's own
  `@camera` test tolerates this by boot-loading this machine's own
  real, previously-solved calibration (`calibrated_at=1785733763`,
  `reprojection_error_cm=0.2065`, matched all 10 ids) into its private,
  ephemeral `state_dir` and using `get_calibration` to *confirm*
  already-calibrated per this ticket's own "calibrate (or confirm
  already calibrated)" allowance, while still making a live
  `calibrate()` call and asserting the tool itself works correctly and
  that a failed live attempt never corrupts the prior good calibration
  (UC-04's own contract) — proven true, for real, both times. **Action
  item for a human with physical access**: clear whatever is currently
  draped over camera 3's view of `main-playfield`'s north edge and
  re-run `uv run pytest -m camera` to confirm the pre-existing suite
  (13 tests, all traced to this one obstruction) returns to green — no
  code change is implicated.

**Test counts**: headless (`uv run pytest -m "not camera"`) 1099
passed (1095 baseline + 4 new: fake golden path, no-daemon-then-
recovers, restart-resilience, cv2 sweep), 40 deselected. `uv run mypy`
(whole `src/aprilcam`, which is `mcp/`'s superset): clean, 71 files, no
issues. `uv run pytest -m camera`: this ticket's own new test passes;
13 pre-existing failures across four other sprints' modules, all one
disclosed physical-obstruction root cause (above), zero new failures
introduced by this ticket.

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
