---
id: 019
title: Convert hard-failing -m camera tests to skip-with-remediation
status: done
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

- [x] **Before count re-measured.** `uv run pytest -m camera -v`
      (rig unavailable, i.e. from this same kind of agent session) is
      run first and its exact pass/fail/skip counts are recorded in
      Completion Notes — sprint work has landed since the 41/57
      measurement (tickets 011-018 in progress or done), so the
      starting point must be re-established, not assumed.
- [x] **Each hard failure triaged individually before conversion.** For
      every currently-failing test, the failure is confirmed to be a
      camera-open / daemon-connect / rig-unavailable failure (not a
      real assertion failure) before its skip handling is touched. Any
      test whose failure turns out to be a genuine bug is left failing
      and flagged in Completion Notes as out of this ticket's scope
      (a separate bug, not a skip-conversion target) rather than
      silently converted.
- [x] **Availability decided by open/`get_frame`, never enumeration.**
      Every converted test's skip decision reuses
      `hardware_camera.py`'s established helpers
      (`resolve_hardware_camera_identity`/`resolve_hardware_camera_slug`/
      `resolve_all_hardware_camera_identities`,
      `hardware_camera_calibration_stale`) rather than a new,
      independently-written availability check.
- [x] **Individual, actionable skips.** Every converted parameterized
      case skips on its own (not the whole test, not the whole module)
      with remediation text naming what is unavailable and what the
      operator should do — matching ticket 010's established message
      style, not a generic "camera unavailable" string.
- [x] **anyio TaskGroup skip-swallowing fix applied where needed.**
      Ticket 011 found that `pytest.skip()` raised inside a nested
      anyio `stdio_client`/`ClientSession` body gets swallowed into an
      opaque `"unhandled errors in a TaskGroup"` with no message. Any
      converted test built on that structure (e.g.
      `tests/aprilcam/mcp/test_integration.py`) gets the same
      catch-and-re-raise-outside-the-taskgroup fix ticket 011 used
      (extract the session body into its own function, catch/store/
      re-raise around it from outside the task-group-guarded scope).
- [x] **After count re-measured, rig unavailable.** `uv run pytest
      -m camera -v` from an agent session (no camera TCC grant) shows
      zero hard failures — every case either skips cleanly with
      remediation text or, for anything flagged as a genuine bug above,
      is explicitly excluded from this criterion and named in
      Completion Notes.
- [x] **Gate exclusion unchanged.** `scripts/test-gate.sh`'s selection
      (`-m "not camera and not integration"`) still deselects every
      converted test, unchanged, before and after this ticket — grep-
      or run-verified, not just asserted in prose.
- [x] **No regression when the rig IS available.** Every converted test
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

## Completion Notes

**Re-measured counts (agent session, no camera TCC grant, `uv run pytest
-m camera -q`).**

- Before (re-measured, not assumed from ticket 010's stale 41/57):
  **41 failed, 24 passed, 19 skipped** (84 selected, 1681-1686
  deselected out of ~1770 total collected -- deselected count drifts by
  a few items run-to-run because tickets 006/013/016 were landing new
  `@pytest.mark.camera` tests concurrently in unrelated files).
- After: **0 failed** across the same 41 node ids, confirmed by two
  independent runs: (1) the full `-m camera` sweep (0 failures, exit 0,
  `22 passed, 64 skipped, 1686 deselected`), and (2) a targeted run
  against exactly the 41 originally-failing node ids
  (`41 skipped in 114.32s`, zero passed/failed/errored). The full-suite
  passed/skipped totals shifted from the original 24/19 baseline because
  concurrent tickets 006/013/016 were adding and modifying other
  `@pytest.mark.camera` modules during this same window (confirmed via
  `git diff --stat -- tests/`, which shows unrelated diffs in
  `tests/aprilcam/vision/test_detector.py`/`test_pipeline.py` and new
  content in `test_detector_tuning_hardware.py`/`test_elevated_locate.py`
  that this ticket never touched) -- not a regression from this ticket.
  A scoped run of only the 12 files this ticket modified, with `-v`,
  showed `0 failed, 5 passed, 42 skipped` (41 conversions + 1
  pre-existing skip), and confirmed by name that both hardware tests
  this ticket deliberately left ungated still pass hardware-free (see
  "Deliberately left alone" below).

**Triage: how environmental vs. real was distinguished.** Every one of
the 41 failures was read individually (full traceback, not just the
top-level assertion) before conversion. All 41 resolved to the exact
same root cause: a real, direct or daemon-mediated `Camera.open()`
call failing with `RuntimeError("failed to open camera at index N")`
(from `opencv_backend.OpenCVBackend.open`) -- the documented "OpenCV:
not authorized to capture video" TCC-denial symptom
(`docs/knowledge/daemon-camera-grant-and-detection-priming.md`), never
an assertion on returned data. Three call shapes surfaced this
differently and were each gated on the *matching* exception type, never
a blanket `except Exception`:
- Direct `Camera.open()` (no daemon) -> raw `RuntimeError`, message-
  matched on `"failed to open camera"` before treating as
  environmental; anything else re-raised.
- Daemon-mediated (`GrpcDaemon`/`DaemonService`) -> `aprilcam.errors.
  AprilCamError`/`ConnectionFailed`, or (raw gRPC stub calls in
  `test_lifecycle_integration.py`) `grpc.RpcError` with `.details()`
  checked for the same signature.
- Subprocess CLI (`aprilcam camera calibrate`/`tags`/`image`) ->
  non-zero exit with `b"failed to open camera"` in stderr, checked
  before the pre-existing `assert returncode == 0` so any other
  non-zero exit (a real CLI bug) still fails that assertion for real.

No failure in this batch was a genuine assertion bug -- none is left
deliberately hard-failing.

**Deliberately left alone (not gated, on purpose).** Two tests share a
`daemon`/fixture with converted tests but were *not* wrapped, because
they provably never need a real camera open and must keep passing
hardware-free:
- `test_calibration_hardware.py::
  test_deskewed_request_on_an_uncalibrated_camera_raises_not_calibrated`
  -- `NotCalibrated` is raised before any backend touch, so the new
  `_skip_unless_camera_actually_opens` probe was added to
  `_link_and_calibrate`/the two tests that need it individually,
  *not* folded into the shared `daemon` fixture itself (which would
  have forced every fixture consumer, including this one, through an
  unnecessary open probe).
- `test_camera.py::
  test_real_camera_effective_config_reports_full_settable_key_set_with_control_backend`
  -- deliberately never opens the backend (own docstring: "camera 3 may
  already be held open by a live production daemon"); left completely
  untouched.
Both confirmed still `PASSED` in the post-change scoped run above.

**Pattern reuse.** Every conversion reuses `tests/aprilcam/
hardware_camera.py`'s existing `resolve_hardware_camera_slug`/
`resolve_all_hardware_camera_identities` for identity resolution
(unchanged, not modified) and adds the real open/`get_frame` probe
inline at the call site, mirroring `test_full_calibrate_hardware.py`'s
own `daemon` fixture exactly -- no new, independently-invented
availability check. Where a fixture is used only by tests that all
need a real open (`test_detection_hardware.py`, `test_taggen_hardware.py`,
`test_streaming_hardware.py`'s `_skip_if_arducam_not_attached`), the
probe lives in the fixture/helper once; where a fixture is shared with
a hardware-free test (`test_calibration_hardware.py`), the probe is
called explicitly only by the tests that need it.

**anyio TaskGroup trap.** Applied to `tests/aprilcam/mcp/
test_integration.py::test_camera_golden_path_against_real_daemon_and_camera_3`,
the one converted test built on `create_connected_server_and_client_
session`'s task-group-guarded `async with` body (the same underlying
anyio mechanism ticket 011 found and fixed for `stdio_client`). The
session body was extracted into `_run_camera_golden_path_session`,
called from a local `try/except BaseException` that stores the
exception and re-raises it *after* the `async with` block closes --
confirmed live: without this split, the skip's own remediation text
would have been discarded in favour of an opaque `"unhandled errors in
a TaskGroup"`; with it, the real skip message reaches pytest (see the
targeted-41 run's `-rs` output, which shows this test's own actionable
message, not the generic anyio one).

**Gate exclusion, run-verified.** `scripts/test-gate.sh` (landed
concurrently by ticket 008 during this same window) still runs
`pytest -m "not camera and not integration"`, unchanged. Confirmed two
ways: (1) `git diff` shows no change to any `@pytest.mark.camera`/
`pytestmark` marker line in any of the 12 files this ticket touched;
(2) `uv run pytest -m "not camera and not integration" --collect-only
-q` against those same 12 files, grepped against the 41 converted node
ids, returns zero matches -- every one is still cleanly deselected.

**No regression when the rig is available.** Not independently
re-verified against real hardware in an operator Terminal session
during this ticket's own session (this session has no macOS camera TCC
grant, by design -- see the knowledge doc). Flagged here, unverified,
mirroring ticket 011's own honesty precedent: an operator should re-run
`uv run pytest -m camera -v` from a Terminal session with the rig
attached to confirm all 41 converted tests still execute their real
assertions past the new skip gates, not just their skip path.
