---
id: 008
title: Integration/acceptance pass and hardware-blocked documentation
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '003'
- '004'
- '006'
- '007'
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Integration/acceptance pass and hardware-blocked documentation

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint. This is the last
ticket in the sequence — every other ticket in this sprint must be
`done` first.

**No background-notification mechanism is available in this
environment.** Run every test synchronously, in-session, and read the
actual result before moving on — do not start a long-running
verification and move on expecting to be notified when it finishes,
and do not pause "waiting for something to finish in the background."
This exact failure mode bit two tickets in sprint 026; do not repeat
it here. If a real-clock integration test genuinely takes a while
(several of this ticket's own tests do, by design — see below), run it
in the foreground and wait for its actual completion before reporting
a result.

## Description

Two things, both real work, neither skippable:

1. **Full-loop headless integration tests** exercising every UC-07/
   UC-11 scenario end to end — from `DaemonService` down through
   `daemon/streaming.py`, `daemon/stream_transport.py`, real sockets,
   `client/streams.py`, to `GrpcDaemon` — using ticket 003's
   fixture-replay backend running under a **real** clock (not a
   scripted/fake one), so genuine timing behavior (heartbeat interval,
   rate cap, queue-full timing under real thread scheduling) is
   actually exercised at least once, not only asserted against a
   controlled fake clock as the per-ticket tests already did.
2. **Explicit, written documentation of exactly which acceptance
   criteria are blocked by the obstructed, dark field** — not silently
   skipped, not silently assumed passing. Write the
   `@pytest.mark.camera` tests for real-hardware verification even
   though they cannot be meaningfully exercised this sprint; they must
   exist, be correctly marked, and skip cleanly (or run inconclusively
   without false-passing) on a machine with no usable camera view.

## Acceptance Criteria

- [x] A real-clock, real-socket integration test (or small suite)
      covers: two subscribers on one deskewed image stream receive
      identical frames with the warp called once per tick (real
      threads, real timing); a slow subscriber measurably drops frames
      while a fast one does not stall; a quiet tag stream heartbeats at
      the configured interval measured against `time.monotonic()`, not
      a fake clock; calibration-stale propagates mid-stream on both
      tag and image streams; unsubscribing (closing the client-side
      stream) is observed daemon-side to idle-gate within a bounded
      real-time window.
  - [x] The ring buffer's wall-clock-bounded claim (UC-26) is checked
      against a real clock: with a tag-stream subscriber open for a
      measured real duration, the buffer's oldest-to-newest timestamp
      span is close to what the configured detection cadence predicts
      — not merely asserted under a fake clock as ticket 003's own
      tests did.
- [x] `@pytest.mark.camera` tests are written for: real two-subscriber-
      one-warp verification against actual hardware; real detection
      cadence against `main-playfield`; the viewer visibly beating
      2 Hz on a real camera; the Stage 8 CPU-idle-budget exit criterion
      (a machine running the daemon with a camera open and zero
      subscribers sits at near-idle CPU). These must be **correctly
      marked** and **skip cleanly** (or fail with a clear,
      non-misleading message, never a false pass) given the field is
      currently obstructed and dark.
- [x] `sprint.md`'s "Hardware constraint this sprint" / Migration
      Concerns sections are cross-checked against what actually got
      built and updated if reality diverged from the plan (e.g. if an
      implementer chose a different constant, module name, or shape
      than the plan's provisional suggestion — the plan says these are
      provisional, so divergence is expected and fine; it must be
      **recorded**, not silently left stale).
- [x] A short, explicit "blocked pending field access" note is added
      (in this ticket's own completion notes, and cross-referenced from
      `sprint.md` if not already present) naming exactly which
      criteria could not be verified this sprint and what running them
      later requires (a clear, dark, unobstructed `main-playfield`
      view on a configured, calibrated camera).
- [x] `uv run mypy` clean across the whole sprint's changes. Zero
      `typing.Protocol` anywhere touched by this sprint.
- [x] Full existing suite (1099-test headless baseline, plus every test
      added by tickets 001-007) passes. Report the actual final count,
      not the baseline, in this ticket's completion notes.

## Implementation Plan

**Approach.** This ticket does not add new production code — it adds
tests and documentation only, at the seam where all six other tickets'
work meets. Read `sprint.md`'s Architecture, Test Strategy, and
Migration Concerns sections again before writing tests, since this
ticket is the one place the *whole* sprint's acceptance criteria are
checked together rather than per-module. Where a per-ticket test
already covers a scenario under a fake clock, this ticket's job is the
**real-clock** version of the same scenario, not a duplicate of the
fake-clock one.

**Files to create/modify:**
- A new integration test module (e.g.
  `tests/aprilcam/daemon/test_streaming_integration.py`) for the
  real-clock, real-socket tests
- `@pytest.mark.camera` additions to the relevant existing test files
  (`daemon/test_streaming.py` and/or a dedicated hardware test module)
- `sprint.md` (this sprint's own doc) — cross-check/update as needed
  per the acceptance criteria above

**Documentation updates:** as described above — `sprint.md`'s hardware-
constraint documentation is the deliverable, not a side effect.

## Testing

- **Existing tests to run**: the full suite, `uv run pytest`, run to
  completion and read in full — this is the ticket where "full suite
  passes" is the actual acceptance bar, not an aside.
- **New tests to write**: see Acceptance Criteria above — real-clock
  integration coverage plus written, correctly-marked
  `@pytest.mark.camera` tests.
- **Verification command**: `uv run pytest && uv run mypy` — run
  synchronously, in-session, and read the real output. Do not report
  this ticket done without having actually seen a passing run.

## Completion Notes

- **New headless integration module**:
  `tests/aprilcam/daemon/test_streaming_integration.py` (9 tests, all
  run against production's real `time.monotonic()`/bounded
  `threading.Event.wait()` — no `clock=`/`wait=` override anywhere in
  this file). Two layers, per the module's own docstring:
  - **Full stack, real socket** (`DaemonService` +
    `StreamTransportManager` + real `grpc.Server` + real `GrpcDaemon`
    over a real unix socket — ticket 005/006's own
    `_bind_stream_client` pattern, reused verbatim): two subscribers on
    one deskewed stream share exactly one warp per tick (proved via a
    monkeypatched, call-counting `Deskewer.deskew` — the real class,
    just counted — plus byte-identical-frame correlation by matching
    `ImageFrame.timestamp` across both sockets); a quiet tag stream's
    heartbeat gaps measured via `time.monotonic()` at actual socket
    receipt (fixed a measurement bug during development — the first
    draft recorded the timestamp *before* the blocking read instead of
    after, which made every gap read ~0ms; corrected to measure at
    receipt); calibration going stale mid-stream
    (`service._mark_stale(slug)`, the same hook ticket 003's own tests
    use) reaches the very next frame on both a tag stream and a
    deskewed image stream, geometry undisturbed; closing both a tag and
    an image stream concurrently on one camera idle-gates both
    producers within a bounded real-time window (a genuinely new
    scenario beyond ticket 006's single-product close tests); an inert
    (never-read) client socket does not stall a second, actively-read
    client on the same producer; the ring buffer's oldest-to-newest
    span, measured against a real 1.5s subscription window, tracks the
    measured elapsed time (generous bands — real thread scheduling
    varies run to run).
  - **Producer level, direct, real clock** (`TagStreamProducer`/
    `ImageStreamProducer` constructed with no `clock=`/`wait=` at
    all): the slow-subscriber-drops/fast-subscriber-unaffected
    scenario, for both tag and image streams — deliberately **not**
    routed through a real socket. At this sprint's default cadences and
    with tiny fixture JPEGs, the kernel's own socket send buffer can
    absorb many seconds of an unread client before `Broadcaster`'s
    bounded (depth-2) queue itself would see backpressure — the OS
    buffer would mask exactly what this scenario needs to prove, or
    make the test take an unpredictable number of real seconds to
    actually saturate it. The "does not stall" half of the same claim
    *is* proved at the real-socket layer (the inert-client test above);
    the "measurably drops" half is proved here instead, honestly
    scoped in the module docstring rather than silently claimed at a
    layer that cannot reliably show it. Full suite run 3 times in a row
    to check for flakiness: 9/9 passed every time, ~19-23s wall clock
    per run.
- **New hardware module**:
  `tests/aprilcam/daemon/test_streaming_hardware.py` (4 tests, all
  `@pytest.mark.camera` via module-level `pytestmark`, all skip cleanly
  via `_skip_if_arducam_not_attached` on a machine with no
  `arducam-ov9782-usb-camera`). Two construction patterns: a
  directly-constructed `DaemonService` around a real `OpenCVBackend`
  (mirrors `test_detection_hardware.py`'s own direct-construction
  precedent) for the one test needing internal-state assertions
  (`Deskewer.deskew` call count); the standard `daemon` fixture
  (`daemon.__main__.build()` reached over a real gRPC channel,
  identical to `test_detection_hardware.py`'s own fixture) for the
  three that only need the public `Daemon` API. One real bug found and
  fixed while writing these: `daemon.__main__.build()` always binds
  sprint 027's stream-transport listeners under `directories.socket_dir`
  (independent of the `lock_path`/`socket_path` overrides, which only
  relocate the gRPC control socket) — the first draft's `daemon`
  fixture did not relocate `XDG_RUNTIME_DIR`, so any test that actually
  called `stream_images`/`stream_tags` hit a real `AF_UNIX path too
  long` bind failure against the real (deep) default socket_dir; fixed
  by `monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))` before
  constructing `Directories()`, matching every other
  `@pytest.mark.camera` module's identical fix for the control socket.
  **This is a real, load-bearing finding for anyone building a hardware
  test that calls `stream_images`/`stream_tags` in a future sprint** —
  recorded here since it was not previously written down anywhere.
- **Real-hardware run — genuinely more informative than sprint.md's
  original "no live-camera verification is possible" assumption.** A
  real `arducam-ov9782-usb-camera` was attached in the environment this
  ticket executed in (sprint.md's "Hardware constraint" section did not
  claim otherwise, but its wording read as if none of the
  `@pytest.mark.camera` suite could do anything this sprint — running
  it found that overstated, and sprint.md has been corrected, see
  below). Of the 4 new hardware tests:
  - **3 genuinely ran against real hardware and passed**, none of them
    gated on calibration or on main-playfield's markers actually being
    visible: `test_real_detection_cadence_against_main_playfield` (23
    ring-buffer entries over a real 2.01s subscription window against a
    real, if dark, capture — cadence timing verified for real, though
    main-playfield's own marker *content* was not, since the field is
    dark/obstructed); `test_viewer_visibly_beats_default_poll_hz_on_real_hardware`
    (4.69 Hz observed, 38 frames/8.11s, against a real Tk window and a
    real camera together); `test_daemon_idle_cpu_budget_with_camera_open_and_zero_stream_subscribers`
    (ring buffer provably unchanged over a real, unsubscribed 10s
    window; 1.325s combined pytest+daemon CPU logged as informational
    only, consistent in order of magnitude with
    `test_detection_hardware.py`'s own prior 1.16s reading for the
    equivalent detection-only case).
  - **1 stayed genuinely blocked and skips cleanly**:
    `test_two_real_subscribers_on_deskewed_stream_share_one_warp_per_tick`
    requires a successful `calibrate()` against `main-playfield` first
    (needed to build a real `Deskewer`), and calibration does not
    currently succeed — three runs during this ticket's own development
    returned `matched_ids` of 7-8 out of the required 10, always
    missing 2-3 ids (observed missing sets: `{1,2,4}`, `{2,3}`,
    `{2,4}`, varying run to run), exactly what an obstructed/
    inconsistently-lit field predicts. `_calibrate_against_main_playfield`
    catches this and calls `pytest.skip(...)` with the matched/missing
    ids and reason in the message, rather than hard-failing — a
    deliberate choice given the ticket's own "skip cleanly (or fail
    with a clear, non-misleading message)" wording; `test_detection_hardware.py`'s
    pre-existing convention for the same situation (hard-`assert`) was
    considered and not followed here, since a clean skip with full
    diagnostic data is strictly more useful for this specific,
    expected-this-sprint condition.
- **Blocked pending field access — the actual, narrowed list** (see
  also sprint.md's "Hardware constraint this sprint" section, updated
  by this ticket to point back here):
  1. **Real two-subscriber-one-warp verification against actual
     hardware** (`test_two_real_subscribers_on_deskewed_stream_share_one_warp_per_tick`)
     — blocked. Requires: `main-playfield`'s calibration markers
     (ArUco 1-10) all visible and detectable simultaneously, meaning
     the obstructing cloth removed and enough light for reliable
     detection. Once calibration succeeds, this test runs unmodified.
  2. **Real detection *against main-playfield's actual markers*** (not
     the cadence-timing half, which already ran and passed) — blocked
     in the same sense ticket 021/024's own hardware suites already
     document (`test_calibration_hardware.py`/`test_detection_hardware.py`,
     11 pre-existing tests unrelated to this sprint failed in this same
     run for the identical reason — see below). Same requirement as
     item 1.
  3. **Real-hardware tuning of the sprint's provisional constants**
     (`TAG_STREAM_INTERVAL_S`, `IMAGE_STREAM_INTERVAL_S`,
     `TAG_STREAM_CHANGE_THRESHOLD_CM`, sprint.md Open Question 1) —
     still blocked; this ticket's real-cadence measurement (23 ticks in
     2.01s, ~11.4 Hz observed against ~30 Hz nominal) is a first real
     data point but is against a dark/near-static scene, not
     representative of real motion or a properly lit field, so it is
     recorded as data, not used to retune anything.
  - **What "the field is clear" needs to mean** before a re-run is
    expected to fully pass: the cloth currently draped over part of
    `main-playfield` removed, and enough room light for all 10 ArUco
    markers (1-10) plus AprilTag 1 to detect reliably across repeated
    polls — the same bar `test_calibration_hardware.py`'s own
    `test_full_set_calibration_matches_all_ten_required_aruco_ids`
    already establishes.
- **Pre-existing, unrelated hardware-test failures observed in the same
  full-suite run — not this sprint's regression.** Running the complete
  `uv run pytest` (camera tests included) surfaced 11 failures, all in
  `test_calibration_hardware.py` (8), `test_detection_hardware.py` (1),
  `test_lifecycle_integration.py` (1), and `test_mobile_tags_hardware.py`
  (1) — sprints 020/021/024/025's own hardware suites, none touched by
  sprint 027. Every one fails on the identical root cause: a real
  `calibrate()`/`camera calibrate` call against `main-playfield`
  returning `succeeded=False` with 1-3 missing ArUco ids, i.e. the same
  physical obstruction/lighting condition sprint.md already names.
  Confirmed pre-existing and unrelated to this sprint's changes: `git
  status` shows no modification to any file under `src/aprilcam` or any
  pre-existing test file this session, and re-running
  `test_calibration_hardware.py` alone (no interaction with any sprint
  027 test) reproduces the same 8 failures with the same "missing ArUco
  id(s)" message. Not fixed here — out of this ticket's scope (these
  belong to other sprints' modules) and not fixable in code (the field
  is genuinely obstructed right now).
- **`sprint.md` cross-check result**: Architecture/module table/class
  names/constant values all match what tickets 001-007 actually built
  — no naming or shape divergence found (`daemon/broadcast.py`'s
  `Broadcaster`, `daemon/streaming.py`'s `TagStreamProducer`/
  `ImageStreamProducer`, `daemon/stream_transport.py`'s
  `StreamTransportManager`, `client/streams.py`'s
  `SocketImageStream`/`SocketTagStream`, `view/viewer.py`'s
  `StreamLoop` — all present, all matching the planned module table).
  Open Question 3 (endpoint reuse) was resolved as the plan's own
  recommended default, confirmed in `daemon/stream_transport.py`'s own
  docstring. The one real divergence found and fixed by this ticket:
  the "Hardware constraint this sprint" section's flat "no live-camera
  verification is possible this sprint" claim, corrected to the
  narrower, evidence-based finding above (a camera was attached; 3 of 4
  new hardware tests ran for real). The "Migration Concerns" →
  "Hardware-blocked acceptance" bullet was updated to match.
- **Test results**: headless baseline (`uv run pytest -m "not
  camera"`) was 1197 as of ticket 007; this ticket adds 9 tests
  (`test_streaming_integration.py`) — **1206 passed, 44 deselected,
  0 failed**, confirmed on a clean run. Full suite including camera
  tests (`uv run pytest`, no marker filter): **1238 passed, 11 failed
  (all pre-existing and unrelated, see above), 1 skipped (this
  ticket's own calibration-blocked hardware test, skipping cleanly),
  0 errors**. `uv run mypy` → "Success: no issues found in 76 source
  files" (mypy's `[tool.mypy] files = ["src/aprilcam"]` scope means
  this ticket's test-only changes are outside what it checks; zero
  `typing.Protocol` confirmed by grep across both new test files —
  no matches).
- **Deviations from the plan**: none beyond what is recorded above
  (the heartbeat-measurement bug fixed during development, the
  `XDG_RUNTIME_DIR` hardware-fixture fix, and the deliberate
  skip-vs-fail choice for the calibration-gated hardware test) — all
  flagged here per the ticket's own honesty requirement.
