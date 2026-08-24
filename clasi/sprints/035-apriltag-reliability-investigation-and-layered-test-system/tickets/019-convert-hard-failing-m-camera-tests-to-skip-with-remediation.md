---
id: 019
title: Convert hard-failing -m camera tests to skip-with-remediation
status: in-progress
use-cases:
- UC-22
depends-on:
- '010'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Convert hard-failing -m camera tests to skip-with-remediation

## Description

Ticket 010 ran the existing hardware suite as a regression check
(`uv run pytest -m camera`) and found **41 of 57 tests hard-fail rather
than skip** when the rig is unavailable — all camera-open failures
propagating as errors instead of clean skips. This is the exact
pathology the sprint's source issue describes: "one measured run showed
18 of 19 failures were environmental (daemon contention), not code." A
suite that reports red for an absent rig, or for an agent session with
no macOS camera TCC grant, carries no signal about whether the code is
actually broken.

Ticket 010 (commit `0c8e955`) established the correct pattern, and
tickets 011 and 012 (commits `883dc4b`, `c2352a5`) already apply it in
the new L1 test modules they introduced:

- Availability is decided by **an actual open / `get_frame` succeeding**,
  never by enumeration alone — enumeration succeeds even when opens
  fail, because a daemon started from an agent process has no macOS
  camera TCC grant
  (`docs/knowledge/daemon-camera-grant-and-detection-priming.md`).
- Each parameterized case **skips individually** with actionable
  remediation text (what is unavailable, what the operator should do),
  never as a block-level skip and never as a hard failure.
- See `tests/aprilcam/hardware_camera.py`
  (`resolve_all_hardware_camera_identities`) and
  `hardware_camera_calibration_stale`, and
  `tests/aprilcam/calibration/test_full_calibrate_hardware.py` for the
  reference implementation.

This ticket applies that same pattern to the rest of the `-m camera`
suite — the modules ticket 010 measured but did not itself convert:
`tests/aprilcam/camera/{test_discovery,test_camera,test_uvc_util,
test_backends}.py`, `tests/aprilcam/mcp/test_integration.py`,
`tests/aprilcam/cli/{test_daemon_cli,test_cli_integration}.py`,
`tests/aprilcam/daemon/{test_streaming_hardware,
test_mobile_tags_hardware,test_camera_geometry_hardware,test_service,
test_detection_hardware,test_lifecycle_integration,
test_detector_tuning_hardware,test_calibration_hardware}.py`,
`tests/aprilcam/tags/test_taggen_hardware.py`,
`tests/aprilcam/client/{test_lifecycle,test_discovery_hardware}.py`,
`tests/aprilcam/view/test_viewer.py`, and any others the re-measurement
below turns up. `tests/aprilcam/client/test_public_api_system.py`
(ticket 012) and `tests/aprilcam/mcp/test_stdio_hardware.py`
(ticket 011) already follow the pattern and are excluded from
conversion — they are in scope only for the before/after count.

**The central risk, stated explicitly**: a test must skip *only* for
genuine environmental unavailability (camera cannot be opened, daemon
absent/unreachable). A test that is hard-failing because of a real,
reachable assertion bug in the code under test must keep failing
loudly. Converting a genuine failure into a skip hides a real bug and
is worse than doing nothing — every failure this ticket touches must be
individually triaged (read the traceback, confirm it is a camera-open
or daemon-connect failure, not an assertion on returned data) before
its handling is changed.

## Acceptance Criteria

- [ ] **Before count re-measured.** `uv run pytest -m camera -v`
      (rig unavailable, i.e. from this same kind of agent session) is
      run first and its exact pass/fail/skip counts are recorded in
      Completion Notes — sprint work has landed since the 41/57
      measurement (tickets 011-018 in progress or done), so the
      starting point must be re-established, not assumed.
- [ ] **Each hard failure triaged individually before conversion.** For
      every currently-failing test, the failure is confirmed to be a
      camera-open / daemon-connect / rig-unavailable failure (not a
      real assertion failure) before its skip handling is touched. Any
      test whose failure turns out to be a genuine bug is left failing
      and flagged in Completion Notes as out of this ticket's scope
      (a separate bug, not a skip-conversion target) rather than
      silently converted.
- [ ] **Availability decided by open/`get_frame`, never enumeration.**
      Every converted test's skip decision reuses
      `hardware_camera.py`'s established helpers
      (`resolve_hardware_camera_identity`/`resolve_hardware_camera_slug`/
      `resolve_all_hardware_camera_identities`,
      `hardware_camera_calibration_stale`) rather than a new,
      independently-written availability check.
- [ ] **Individual, actionable skips.** Every converted parameterized
      case skips on its own (not the whole test, not the whole module)
      with remediation text naming what is unavailable and what the
      operator should do — matching ticket 010's established message
      style, not a generic "camera unavailable" string.
- [ ] **anyio TaskGroup skip-swallowing fix applied where needed.**
      Ticket 011 found that `pytest.skip()` raised inside a nested
      anyio `stdio_client`/`ClientSession` body gets swallowed into an
      opaque `"unhandled errors in a TaskGroup"` with no message. Any
      converted test built on that structure (e.g.
      `tests/aprilcam/mcp/test_integration.py`) gets the same
      catch-and-re-raise-outside-the-taskgroup fix ticket 011 used
      (extract the session body into its own function, catch/store/
      re-raise around it from outside the task-group-guarded scope).
- [ ] **After count re-measured, rig unavailable.** `uv run pytest
      -m camera -v` from an agent session (no camera TCC grant) shows
      zero hard failures — every case either skips cleanly with
      remediation text or, for anything flagged as a genuine bug above,
      is explicitly excluded from this criterion and named in
      Completion Notes.
- [ ] **Gate exclusion unchanged.** `scripts/test-gate.sh`'s selection
      (`-m "not camera and not integration"`) still deselects every
      converted test, unchanged, before and after this ticket — grep-
      or run-verified, not just asserted in prose.
- [ ] **No regression when the rig IS available.** Every converted test
      still exercises its real assertions (not just its skip path) when
      run from an operator Terminal session with a camera attached —
      either genuinely re-run by an operator, or, if that is not
      practical within this ticket's session, explicitly named in
      Completion Notes as unverified-against-real-hardware (mirroring
      ticket 011's own honesty precedent about unexecuted hardware
      paths), never silently assumed.

## Implementation Plan

**Approach**: Triage first, convert second, verify third — do not batch
"convert everything that currently fails" without first confirming each
failure's actual cause. For each module in the modified-files list:
1. Run it in isolation with the rig unavailable, read the traceback.
2. If the failure is a camera-open/daemon-connect failure, convert its
   availability check to the `hardware_camera.py` pattern and add
   individual per-case skip-with-remediation.
3. If the module drives a session through nested anyio
   `stdio_client`/`ClientSession` bodies, apply ticket 011's
   catch-and-re-raise-outside-the-taskgroup structure so skips (and
   real failures) surface with their real message.
4. If the failure is not environmental, leave it failing and record it
   in Completion Notes as out of scope.

**Rules that apply throughout this ticket** (per project rules and this
sprint's Test Strategy):
- **NEVER run `aprilcam daemon restart`, `daemon stop`, or start a
  daemon outside an isolated test fixture** — the operator's real
  daemon is running; a daemon started from an agent process loses its
  camera grant and breaks the rig for everyone. All isolated-daemon
  work in this ticket uses the existing
  `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR`/`XDG_RUNTIME_DIR` +
  private-daemon isolation pattern already used throughout this sprint.
- `camera_position` is sacred: never write
  `~/.config/aprilcam/cameras/*/config.json`.
- See `docs/knowledge/daemon-camera-grant-and-detection-priming.md` for
  why an agent-launched daemon cannot open cameras on this rig, and why
  that is expected, not a bug to chase.

**Files to modify** (subject to the triage step above — a file is only
touched if its failures are confirmed environmental):
- `tests/aprilcam/camera/test_discovery.py`,
  `tests/aprilcam/camera/test_camera.py`,
  `tests/aprilcam/camera/test_uvc_util.py`,
  `tests/aprilcam/camera/test_backends.py`
- `tests/aprilcam/mcp/test_integration.py`
- `tests/aprilcam/cli/test_daemon_cli.py`,
  `tests/aprilcam/cli/test_cli_integration.py`
- `tests/aprilcam/daemon/test_streaming_hardware.py`,
  `tests/aprilcam/daemon/test_mobile_tags_hardware.py`,
  `tests/aprilcam/daemon/test_camera_geometry_hardware.py`,
  `tests/aprilcam/daemon/test_service.py`,
  `tests/aprilcam/daemon/test_detection_hardware.py`,
  `tests/aprilcam/daemon/test_lifecycle_integration.py`,
  `tests/aprilcam/daemon/test_detector_tuning_hardware.py`,
  `tests/aprilcam/daemon/test_calibration_hardware.py`
- `tests/aprilcam/tags/test_taggen_hardware.py`
- `tests/aprilcam/client/test_lifecycle.py`,
  `tests/aprilcam/client/test_discovery_hardware.py`
- `tests/aprilcam/view/test_viewer.py`
- No changes to `tests/aprilcam/hardware_camera.py` itself expected
  (its helpers are reused, not modified) unless triage surfaces a gap
  in the existing helper API that a converted test genuinely needs.

**Testing plan**:
- Run the full `-m camera` suite with the rig unavailable (agent
  session) before and after conversion; record both counts.
- Run `scripts/test-gate.sh` (or its underlying deselect expression)
  to confirm the gate's exclusion is unchanged.
- If an operator session is available during this ticket, re-run the
  converted suite with a camera attached to confirm real assertions
  still execute past the skip gate; otherwise name this explicitly as
  unverified in Completion Notes.
- **Verification command**: `uv run pytest -m camera -v` (rig
  unavailable, expect zero failures, clean skips with remediation
  text), then `uv run pytest -m "not camera and not integration" -v`
  to confirm the gate's own selection is unaffected.

**Documentation updates**:
- None required beyond Completion Notes; traceability is owned by
  ticket 018's final sweep, not this ticket.
