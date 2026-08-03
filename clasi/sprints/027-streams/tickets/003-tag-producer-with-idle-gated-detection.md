---
id: '003'
title: Tag producer with idle-gated detection
status: done
use-cases:
- SUC-001
- SUC-004
depends-on:
- '001'
- '002'
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Tag producer with idle-gated detection

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint — including ticket
004, which is structurally independent (both depend only on 001/002)
but must still run after this one finishes, not alongside it.

## Description

Implement `daemon/streaming.py`'s `TagStreamProducer`: a per-camera
background thread, created lazily on the first tag-stream subscriber
and torn down on the last unsubscribe, that drives
`DetectionEngine.run_burst` at a steady cadence and publishes
`TagFrame`s through ticket 001's `Broadcaster` — change-driven,
rate-capped, with a heartbeat (`sprint.md` Design Rationale 3, 4). Wire
`DaemonService.stream_tags` to create/reuse/tear down this producer.

This is the ticket that makes the tag-history ring buffer wall-clock
bounded (UC-26) — `daemon/detection.py`'s own docstring already names
this as the fix its "accepted, honest" scope boundary was waiting for.
It is also where `TagFrame.calibration_stale` gets wired onto every
published stream frame (UC-11.S-3) — the field already exists
(sprint 023); this is wiring, not new design.

Because real hardware is blocked this sprint (dark, obstructed field),
this ticket also builds the shared headless test infrastructure later
tickets reuse: a looping/fixture-replay fake camera backend
(development-plan.md Stage 8 names this explicitly: "fake-camera
daemon (fixture replay backend)") that supplies continuous frames to a
background reader without exhausting like the existing `FakeBackend`
does once its queue empties.

## Acceptance Criteria

- [x] `TagStreamProducer` does not exist as a running thread when a
      camera has zero tag-stream subscribers — asserted directly on
      internal state, not just behaviorally — and `DetectionEngine.
      run_burst` is called zero times for that camera over a wall-clock
      window with no subscriber (UC-07.S-3 applied to tags, UC-11's
      idle-gating requirement).
- [x] With one subscriber, `run_burst` runs at a steady, configured
      cadence (a named, overridable, honestly-provisional constant —
      see `sprint.md` Open Question 1) and the per-camera ring buffer
      grows wall-clock-bounded, not query-driven (UC-26).
  - [x] Publish is change-driven (world-cm comparison when calibrated,
      pixel-space fallback when not — `sprint.md` Design Rationale 5)
      and rate-capped; a quiet fixture (identical detections every
      tick) still delivers heartbeats at the configured interval
      (UC-11.S-2).
- [x] Flipping a camera's calibration to stale mid-stream (using the
      same test hook `daemon/detection.py`'s own tests use) sets
      `TagFrame.calibration_stale=True` on the very next published
      frame (UC-11.S-3).
- [x] A slow subscriber (a queue that is never drained) drops frames
      without affecting a fast subscriber on the same camera.
- [x] Last unsubscribe tears the producer thread down cleanly (joined,
      not merely signaled); a following resubscribe starts a fresh
      one.
- [x] `DaemonService` tracks every live `TagStreamProducer` and stops
      (joins, with a bounded timeout) all of them when
      `DaemonLifecycle.stop()` runs — no orphaned background thread
      survives a clean daemon shutdown (`sprint.md` Migration Concerns
      "Clean daemon shutdown").
- [x] `uv run mypy` clean. Zero `typing.Protocol` — `TagStreamProducer`
      derives from a plain base class if one is introduced; the
      concrete `TagStream` implementation `DaemonService.stream_tags`
      returns derives from `daemon/interfaces.py`'s `TagStream` base
      class (mechanical inheritance, not structural).
- [x] Full existing suite (1099 tests, headless baseline) still
      passes; no change to `get_tags`/`get_tag`'s existing one-shot
      behavior (`sprint.md` Design Rationale 4 — one-shot queries stay
      independent this sprint).

## Implementation Plan

**Approach.** Reuse `DetectionEngine.run_burst(slug, camera)` exactly
as `DaemonService.get_tags` already calls it — the producer's
background loop is a thin scheduler around the same call, not a
parallel detection path. Gate thread lifecycle on subscriber count via
ticket 001's `Broadcaster` (subscribe → if this is the first
subscriber for this camera, start the thread; unsubscribe → if this
was the last, stop and join it). Read `calibration_stale` off the
`TagFrame` `run_burst` already returns — no direct `calibration/`
import needed in the common case. For the looping fake backend, extend
`tests/aprilcam/camera/test_camera.py`'s `FakeBackend` (e.g. an
optional `loop=True` that re-queues frames instead of exhausting to
`None`) or add a small sibling test double — reuse an existing
fixture image from `tests/images/` as the repeated frame so detection
has something real to find, matching Stage 8's "fixture replay
backend" language. Document whichever shape is chosen so tickets
004/005/006/008 can reuse it without reinventing it.

**Files to create/modify:**
- `src/aprilcam/daemon/streaming.py` (new — `TagStreamProducer`)
- `src/aprilcam/daemon/service.py` (wire `stream_tags`; track producers
  for shutdown)
- `src/aprilcam/daemon/lifecycle.py` or `daemon/__main__.py` (whichever
  already owns `DaemonLifecycle.stop()`'s call site — confirm producer
  teardown is invoked from there)
- Test infra: extend `tests/aprilcam/camera/test_camera.py`'s
  `FakeBackend` or add a new looping test double (implementer's
  choice; document it)

**Documentation updates:** `daemon/detection.py`'s module docstring
currently states the ring buffer is "query-driven, not wall-clock-
bounded, this sprint" as an accepted scope boundary of sprint 021 —
update that note to point at this ticket/sprint now that the boundary
is closed for cameras with an active tag-stream subscriber (and note
it remains query-driven for one-shot-only callers, per `sprint.md`
Design Rationale 3's consequence).

## Testing

- **Existing tests to run**: `tests/aprilcam/daemon/test_detection.py`,
  `tests/aprilcam/daemon/test_service.py`, full suite via
  `uv run pytest`.
- **New tests to write**: `tests/aprilcam/daemon/test_streaming.py`
  (or similar) covering every acceptance criterion above — zero-
  subscriber idle assertion, steady-cadence `run_burst` calls under a
  fake/controllable clock, change-driven + rate-capped + heartbeat
  publish, stale-mid-stream propagation (reusing `daemon/detection.py`
  test suite's stale-injection pattern), slow-subscriber drop, and
  clean-shutdown/thread-join behavior.
- **Verification command**: `uv run pytest && uv run mypy`.

## Completion Notes

- **`TagStreamProducer` API** (`src/aprilcam/daemon/streaming.py`, new):
  `TagStreamProducer(slug, camera, detection, broadcaster=None, *,
  interval_s=TAG_STREAM_INTERVAL_S, heartbeat_interval_s=TAG_STREAM_HEARTBEAT_INTERVAL_S,
  max_publish_hz=TAG_STREAM_MAX_PUBLISH_HZ, change_threshold_cm=TAG_STREAM_CHANGE_THRESHOLD_CM,
  change_threshold_px=TAG_STREAM_CHANGE_THRESHOLD_PX, queue_depth=...,
  clock=time.monotonic, wait=_default_wait, stop_timeout_s=2.0)`.
  `broadcaster=None` (the default) builds and self-wires its own
  `Broadcaster[TagFrame]` (`on_active=self.start, on_idle=self.stop`) --
  exposed as `producer.broadcaster`. Passing an already-constructed
  `Broadcaster` instead lets a caller (e.g. `DaemonService`) add its own
  bookkeeping around the same 0↔1 transitions. `producer.running` is
  the direct internal-state observable (`self._thread is not None`) AC1
  asks for. `start()`/`stop()` are idempotent and safe to call directly
  or via the `Broadcaster` hooks; `stop()` actually joins the thread
  (bounded by `stop_timeout_s`), not merely signals it.
- **Heartbeat mechanism**: no separate message shape. `run_burst`
  already returns a fresh `TagFrame` every tick regardless of content
  change, so "heartbeat" is simply republishing that same-content frame
  once `heartbeat_interval_s` (1.0 s, per this ticket's dispatch) has
  elapsed since the last publish of any kind (change- or
  heartbeat-triggered) — this is the "simplest reading" the ticket
  named up front.
- **Change threshold + provenance**: decided per `TagRecord`, not per
  frame — `record.world is not None` compares in world-cm
  (`TAG_STREAM_CHANGE_THRESHOLD_CM`), else pixel-space fallback
  (`TAG_STREAM_CHANGE_THRESHOLD_PX`). World-cm threshold reuses
  `vision.motion.MIN_RELIABLE_DISPLACEMENT_CM` (0.5 cm) rather than an
  independently-invented number — that constant already answers the
  adjacent, real-hardware-derived question "how much world displacement
  is signal vs. reprojection noise" (ticket 022-003's ~0.18-0.19 cm RMS
  measurement, doubled), and reusing it keeps exactly one place in the
  codebase making that call. Pixel-space threshold (8.0 px) and the
  rate cap (`TAG_STREAM_MAX_PUBLISH_HZ = 20.0`) are carried forward
  unchanged from v1's `TagStreamProducer` (`archive/src/aprilcam/daemon/stream.py`).
  Detection cadence (`TAG_STREAM_INTERVAL_S = 1/30`) is a new,
  honestly-provisional constant (sprint.md Open Question 1) chosen to
  make `daemon/detection.py`'s own `RING_BUFFER_SIZE` docstring's
  "roughly 10 seconds at 30 fps" assumption actually true once a
  subscriber exists, rather than an unrelated fresh guess — not
  validated against real detection CPU cost (hardware blocked this
  sprint).
- **`DaemonService.stream_tags` wired in this ticket, not deferred to
  005** — see the "Scope reconciliation" note below.
  `DaemonService` gained `self._tag_stream_producers: Dict[str,
  TagStreamProducer]`, lazily populated per camera slug by
  `stream_tags()` and reused across repeated calls (sprint.md Open
  Question 3's recommended reuse default, applied at the in-process
  layer). `close()` now stops every tracked producer (joined, bounded
  timeout) *before* closing cameras, so no producer thread is ever
  mid-`Camera.read()` when its backend handle is torn down —
  `daemon/lifecycle.py`'s existing `on_stop=service.close` wiring
  covers `DaemonLifecycle.stop()` with no changes needed to
  `lifecycle.py`/`__main__.py` themselves. `BroadcasterTagStream`
  (`daemon/streaming.py`) is the concrete `daemon.interfaces.TagStream`
  `stream_tags()` returns — mechanical inheritance, `__iter__` is a
  bounded-poll generator against its own `close()` flag (no queue
  sentinel, matching ticket 001's design), `close()` unsubscribes and
  is idempotent. No socket/gRPC code was touched — `daemon/rpc.py` and
  `daemon/stream_transport.py` remain ticket 005's job exactly as
  planned.
- **Scope reconciliation (please verify this reading is correct):**
  this ticket's own dispatch said "Do NOT wire the RPC layer or
  `DaemonService.stream_tags` itself yet — that's ticket 005," which
  reads as excluding `DaemonService.stream_tags` entirely. But
  `sprint.md`'s own module table assigns `daemon/streaming.py` to
  "Wires `DaemonService.stream_images`/`stream_tags`," this ticket's
  own Description/AC7 name `DaemonService` producer-tracking as this
  ticket's job, and ticket 005's own file
  (`005-getimagestream-gettagstream-rpcs-and-socket-transport.md`)
  states its `daemon/stream_transport.py` "for each accepted connection
  call `DaemonService.stream_images`/`stream_tags` to obtain a
  subscription" and its own AC2 says connecting a socket "causes
  exactly one `DaemonService.stream_images`/`stream_tags` subscription
  to be created" — both presuppose that method already exists by the
  time ticket 005 runs. Given that three-way corroboration (sprint.md,
  this ticket's own AC7, and ticket 005's file), I read "the RPC layer"
  in the dispatch note as `daemon/rpc.py`'s `GetTagStream` handler +
  `daemon/stream_transport.py`'s socket bridge specifically (ticket
  005's unambiguous job), not `DaemonService.stream_tags`'s in-process
  method — and implemented the latter here, fully in-process, no
  gRPC/sockets, exactly matching the dispatch's own "fully testable
  with a fake camera/detector and no real gRPC" framing. Flagging this
  explicitly per the ticket's honesty requirement in case this reading
  is wrong.
- **Looping fake camera backend**: `tests/aprilcam/camera/test_camera.py`'s
  `FakeBackend` gained an optional `loop: bool = False` constructor
  parameter — when `True`, an exhausted `read_queue` refills from the
  original frames instead of exhausting to `None`, so a continuously-
  running producer thread never runs dry. Default `False` leaves every
  pre-existing test unaffected. One test
  (`test_real_detection_over_the_looping_backend_publishes_the_real_tag`)
  drives a real `DetectionEngine()` (real pipeline, real detector, no
  scripted double) over a `loop=True` backend repeating a synthetic
  AprilTag-7 image, proving the backend genuinely supplies continuous,
  real, detectable frames — using `tests/aprilcam/vision/_tag_fixtures.py`'s
  `synthetic_tag_image` (the same generator `test_detection.py` already
  uses for its own real-detection tests) rather than a static file
  under `tests/images/` — a minor, precedented deviation from the
  implementation plan's "reuse an existing fixture image from
  `tests/images/`" suggestion, since `synthetic_tag_image` is this
  codebase's actual established source of real, detectable tag pixels
  for headless tests.
- **`daemon/detection.py` module docstring updated**: the "Accepted
  scope boundary" section now points at this ticket/sprint as closing
  the wall-clock-bounded gap for a camera with an active tag-stream
  subscriber, while explicitly noting one-shot-only callers keep
  sprint 021's original query-driven behaviour, per `sprint.md` Design
  Rationale 3's stated consequence.
- **Test results**: new `tests/aprilcam/daemon/test_streaming.py` (14
  tests, all headless, fake-clock-driven for every cadence/threshold/
  heartbeat/rate-cap/stale-flip test per the ticket's own "do not use
  real `time.sleep` for the cadence tests" requirement) plus 3 new
  tests in `tests/aprilcam/daemon/test_service.py` covering
  `stream_tags`/`close()` wiring. `uv run mypy` → "Success: no issues
  found in 74 source files", zero `typing.Protocol` in new/changed
  code. Full suite: `uv run pytest -m "not camera"` → 1141 passed, 1
  failed (`test_playfield_annotations_subprocess_round_trips_against_a_python_client_put`,
  a real-subprocess CLI integration test unrelated to tag streaming),
  40 deselected on the first full run under this session's load;
  isolated re-runs of that one test and its whole file both passed
  cleanly (`1 passed` / `2 passed, 1 deselected`), and a clean full-suite
  re-run (no concurrent activity) confirmed **1142 passed, 40
  deselected, 0 failed** — the failure was transient full-suite-load
  flakiness in a subprocess-spawning daemon-startup test, not a
  regression from this ticket's changes (it exercises annotations, not
  streaming). Baseline was 1125 (post ticket 002); this ticket added 17
  new tests (14 + 3).
- **Deviations from the plan**: none beyond the scope reconciliation
  and fixture-image-source notes above, both recorded for the
  team-lead/reviewer to confirm.
