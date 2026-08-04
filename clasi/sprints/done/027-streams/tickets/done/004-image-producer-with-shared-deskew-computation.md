---
id: '004'
title: Image producer with shared deskew computation
status: done
use-cases:
- SUC-002
- SUC-004
depends-on:
- '001'
- '002'
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Image producer with shared deskew computation

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint — including ticket
003, which this ticket does not depend on but must still follow, not
run alongside.

## Description

Implement `daemon/streaming.py`'s `ImageStreamProducer`: a background
thread per (camera, deskewed) pair, created lazily on the first
image-stream subscriber and torn down on the last unsubscribe, that
captures, (warps if deskewed,) and JPEG-encodes at a steady cadence,
embedding the latest `TagFrame` and (deskewed only) `AnnotationSet`
exactly as `DaemonService.get_frame` does today, and publishes through
ticket 001's `Broadcaster`. Wire `DaemonService.stream_images` to
create/reuse/tear down this producer.

This is the ticket that proves `sprint.md`'s central shared-computation
claim: two subscribers to the same (camera, deskewed=True) stream must
cause **one** warp, not two (UC-07.S-1) — a direct structural
consequence of there being exactly one producer per (camera, deskewed)
pair feeding one `Broadcaster`, not a special-cased dedup check.

## Acceptance Criteria

- [x] `ImageStreamProducer` does not exist as a running thread for a
      given (camera, deskewed) pair with zero subscribers — asserted
      directly on internal state — and zero capture/warp/encode calls
      happen for it over a wall-clock window with no subscriber
      (UC-07.S-3).
- [x] Two subscribers to the same (camera, deskewed=True) producer
      receive frames with identical metadata (`px_per_cm`/
      `world_extent`), and the warp routine is called **once** per
      tick, not twice — proven with a call-counting double around the
      deskew/warp step (UC-07.S-1).
- [x] A slow subscriber (queue never drained) drops frames without
      affecting a fast subscriber on the same producer (UC-07.S-2).
- [x] Frame geometry (`px_per_cm`/`world_extent`) is computed once at
      producer start and stays stable for the life of the stream, even
      if a config push changes something else mid-stream (UC-07's
      "frame geometry stable for the life of a stream" contract).
- [x] Every published frame embeds the latest `TagFrame` (raw and
      deskewed) and, for deskewed frames only, the linked playfield's
      current `AnnotationSet` — matching `get_frame`'s existing
      embed contract exactly (no new embed policy invented here).
- [x] Calibration going stale mid-stream sets
      `ImageFrame.calibration_stale=True` on the very next published
      deskewed frame.
- [x] Last subscriber leaving a (camera, deskewed) pair stops
      capture/encode/warp for it; a resubscribe starts fresh.
- [x] `DaemonService` tracks every live `ImageStreamProducer` and stops
      (joins, bounded timeout) all of them on `DaemonLifecycle.stop()`
      — no orphaned thread survives a clean shutdown (`sprint.md`
      Migration Concerns "Clean daemon shutdown"), consistent with
      ticket 003's identical requirement for `TagStreamProducer`.
- [x] `uv run mypy` clean. Zero `typing.Protocol` — the concrete
      `ImageStream` `DaemonService.stream_images` returns derives from
      `daemon/interfaces.py`'s `ImageStream` base class.
- [x] Full existing suite (1099 tests, headless baseline) still
      passes; `get_frame`'s existing one-shot behavior is unchanged
      by this ticket (`sprint.md` Design Rationale 4).

## Implementation Plan

**Approach.** Reuse the existing capture/encode/warp code paths
`DaemonService.get_frame`/`_get_deskewed_frame` already call — the
producer's background loop is a thin scheduler around the same
routines, not a second implementation of them. Reuse ticket 003's
looping/fixture-replay fake backend for headless testing rather than
building a second one. Gate thread lifecycle on subscriber count via
the `Broadcaster`, mirroring ticket 003's pattern exactly (first
subscriber starts the thread, last unsubscribe stops and joins it) —
the two producers should feel like siblings, not independently
invented mechanisms.

**Files to create/modify:**
- `src/aprilcam/daemon/streaming.py` (extend — `ImageStreamProducer`,
  alongside ticket 003's `TagStreamProducer`)
- `src/aprilcam/daemon/service.py` (wire `stream_images`; track
  producers for shutdown, alongside ticket 003's tracking)

**Documentation updates:** none beyond code docstrings — no public
contract changes (`get_frame` is unchanged; `ImageStream`'s contract
was already documented in `daemon/interfaces.py` by a prior sprint).

## Testing

- **Existing tests to run**: `tests/aprilcam/daemon/test_service.py`,
  full suite via `uv run pytest`.
- **New tests to write**: extend `tests/aprilcam/daemon/
  test_streaming.py` (from ticket 003) with image-producer coverage —
  zero-subscriber idle assertion, two-subscribers-one-warp via a
  call-counting warp double, slow-subscriber drop, geometry-stability
  assertion across a mid-stream config push, tags/annotations embed
  parity with `get_frame`, stale-mid-stream propagation, and clean-
  shutdown/thread-join behavior.
- **Verification command**: `uv run pytest && uv run mypy`.

## Completion Notes

- **`ImageStreamProducer` API** (`src/aprilcam/daemon/streaming.py`,
  extended): `ImageStreamProducer(slug, camera, detection,
  broadcaster=None, *, deskewed=False, calibration_lookup=None,
  playfield_lookup=None, annotations_lookup=None,
  deskewer_factory=_default_deskewer_factory,
  interval_s=IMAGE_STREAM_INTERVAL_S, queue_depth=..., clock=time.monotonic,
  wait=_default_wait, stop_timeout_s=2.0)`. Mirrors
  `TagStreamProducer`'s shape exactly: `broadcaster=None` self-wires its
  own `Broadcaster[ImageFrame]` (`on_active=self.start,
  on_idle=self.stop`); `producer.running` is the direct
  `self._thread is not None` observable; `start()`/`stop()` are
  idempotent and `stop()` actually joins (bounded by `stop_timeout_s`).
  `deskewed=True` requires `calibration_lookup`/`playfield_lookup`/
  `annotations_lookup` (`ValueError` otherwise) -- the third positional
  parameter is the same shared `DetectionEngine` `TagStreamProducer`
  already reads/writes, used here read-only (`.latest(slug)`) to embed
  tags without ever calling `run_burst` itself.
- **No change-detection, no heartbeat, no rate cap -- deliberately
  simpler than `TagStreamProducer`.** `sprint.md`'s SUC-002 main flow
  says plainly "captures, (warps if deskewed,) and JPEG-encodes at a
  steady cadence" -- every tick publishes unconditionally. Streams are
  always JPEG (`Daemon.get_frame`'s own docstring: "streams are JPEG");
  unlike `get_frame`'s one-shot `encoding` parameter, there is no
  encoding choice on the streaming path.
- **Shared computation, proven by call-counting, not asserted.** The
  new test `test_deskewed_producer_warps_once_per_tick_shared_by_two_subscribers`
  (`tests/aprilcam/daemon/test_streaming.py`) injects a
  `_CountingDeskewer` (the real `calibration.deskew.Deskewer`, wrapped
  with a call counter on `.deskew()`, via the new `deskewer_factory`
  constructor hook) into a producer with two subscribers, and asserts
  `created[0].calls == len(received1) == len(received2)` -- the warp
  ran exactly once per *published frame*, not once per
  (frame, subscriber) pair -- plus `received1[i] is received2[i]` for
  every index, proving the *same* `ImageFrame` object (not an
  equal-looking copy) reached both subscribers. This falls out
  structurally from `DaemonService.stream_images` keying its producer
  cache by `(slug, deskewed)` (one `ImageStreamProducer` per pair,
  reused across calls) feeding one `Broadcaster` -- there is no
  separate dedup/cache check anywhere in the production loop itself.
- **Frame geometry stability -- snapshot-at-start, not
  recompute-but-verify-stable (the ticket's own "your call" choice).**
  `calibration.deskew.Deskewer` already computes `output_size`/
  `px_per_cm`/`world_extent` once, at its *own* construction, and never
  recomputes them (that class's own docstring). `ImageStreamProducer`'s
  contribution is constructing exactly **one** `Deskewer` per
  `start()` call (the 0->1 subscriber transition that begins a stream's
  life) via the internal `_build_deskew_context`/`_deskew_ctx` cache,
  and reusing that same instance for every tick until `stop()` --
  never rebuilding it from whatever `calibration_lookup`/
  `playfield_lookup` currently say mid-stream. `calibration_stale` is
  deliberately *not* part of that snapshot: it is read fresh from
  `calibration_lookup` on every tick, so a mid-stream staleness flip
  still reaches the very next published frame (AC6) without disturbing
  the frozen geometry (AC4). Proven by two tests:
  `test_deskewed_image_geometry_stays_stable_across_a_mid_stream_playfield_replace`
  (a `set_playfield`-shaped push mid-stream, with a *different*
  `width_cm`/`height_cm`, has zero effect on an already-running
  stream's published `px_per_cm`/`world_extent`) and
  `test_image_producer_resubscribe_starts_fresh_and_rebuilds_geometry`
  (the same push, but landed while idle between two separate stream
  lives, *does* take effect on the next `start()` -- "stable for the
  life of a stream," not "stable forever").
- **Preconditions checked before any producer exists, never inside the
  background thread.** `DaemonService.stream_images` applies the exact
  same two-step check `_get_deskewed_frame` already uses for the
  one-shot path (calibration exists, then its linked playfield
  definition is still held) *before* constructing or reusing an
  `ImageStreamProducer` for a deskewed request -- `NotCalibrated`
  therefore raises synchronously, in the caller's own thread, and no
  producer or subscription is ever created for the rejected request
  (`test_stream_images_deskewed_raises_not_calibrated` asserts
  `service._image_stream_producers == {}` after the raise). A raw
  request needs no calibration, unchanged. The background thread's own
  `_build_deskew_context` re-checks defensively (logs and retries next
  tick rather than crashing) purely because calibration is never
  un-set once established, making that a should-not-happen path, not
  because the precondition is meant to be enforced there.
- **`DaemonService.stream_images` wired in this ticket, not deferred to
  005** -- same scope reconciliation ticket 003 already flagged and
  applied for `stream_tags`, restated here for `stream_images`:
  `sprint.md`'s module table, this ticket's own Description/AC8, and
  ticket 005's own file all three assign the in-process
  `DaemonService.stream_images` wiring to this ticket, with only
  `daemon/rpc.py`'s `GetImageStream` handler and
  `daemon/stream_transport.py`'s socket bridge left as ticket 005's
  job. `DaemonService` gained `self._image_stream_producers: Dict[
  Tuple[str, bool], ImageStreamProducer]`, keyed one dimension wider
  than `_tag_stream_producers` (raw and deskewed are independent
  producers for the same camera -- proven by
  `test_stream_images_raw_and_deskewed_are_independently_tracked` and
  `test_raw_and_deskewed_producers_for_the_same_camera_are_independent`),
  lazily populated and reused per `(slug, deskewed)` key exactly like
  `stream_tags`'s per-slug cache. `close()` now stops every tracked
  image producer (joined, bounded timeout) alongside the existing tag
  producers, both before cameras are closed, unchanged wiring from
  `daemon/lifecycle.py`'s side (its existing `on_stop=service.close`
  already covers this, same as ticket 003 found). No socket/gRPC code
  touched.
  `daemon/service.py`'s own module docstring (the "Only a subset of
  `Daemon`'s methods are implemented" paragraph) was stale after
  ticket 003 -- it still said `stream_images`/`stream_tags` were "the
  remaining unimplemented pair" even though ticket 003 had already
  implemented `stream_tags`. Corrected here as part of this ticket's
  own change to the same paragraph, rather than left further out of
  date.
- **Embed parity with `get_frame`, checked two ways.** Producer-level
  (`tests/aprilcam/daemon/test_streaming.py`):
  `test_raw_image_producer_embeds_latest_tags_but_never_annotations`
  and
  `test_deskewed_image_producer_embeds_latest_tags_and_current_annotations`
  pin the three-way `None`/empty/populated contract (raw: `tags` set,
  `annotations` always `None`; deskewed: both set, `annotations` always
  a real `AnnotationSet` even when empty) against an injected
  `annotations_lookup`. Service-level (`tests/aprilcam/daemon/
  test_service.py`):
  `test_stream_images_deskewed_embed_matches_get_frame_for_the_same_moment`
  runs one real detection burst via `get_tags`, then asserts both
  `service.stream_images(deskewed=True)`'s first frame and a
  `service.get_frame(deskewed=True)` call immediately after embed the
  *same* `TagFrame` object (`==` to the burst result) and equal
  `AnnotationSet` content (annotations tuple + revision; `timestamp` is
  deliberately excluded from the comparison since each `get_annotations`
  call legitimately stamps its own wall-clock value) -- proving the
  streaming and one-shot paths read from the identical underlying
  stores, not two independently-arrived-at, coincidentally-equal
  answers.
- **Test results**: `tests/aprilcam/daemon/test_streaming.py` gained 13
  new `ImageStreamProducer`/`BroadcasterImageStream` tests (idle
  gating, start/stop idempotency, raw/deskewed independence, the
  deskewed-requires-lookups `ValueError`, shared-warp call-counting,
  slow-subscriber drop, geometry stability across a mid-stream push,
  geometry-resets-on-resubscribe, calibration-stale propagation, embed
  parity for both raw and deskewed, and `BroadcasterImageStream.close`
  idempotency); `tests/aprilcam/daemon/test_service.py` gained 6 new
  `stream_images` wiring tests (raw works uncalibrated, deskewed raises
  `NotCalibrated` with no producer created, a real calibrated deskewed
  stream publishes real 800x532 frames, embed parity against
  `get_frame`, producer reuse across repeated calls, raw/deskewed
  independent tracking, and `close()` stopping every tracked image
  producer). `uv run mypy` -> "Success: no issues found in 74 source
  files", zero `typing.Protocol` in new/changed code (the
  `deskewer_factory`/`calibration_lookup`/`playfield_lookup`/
  `annotations_lookup` injection points are all plain `Callable[...]`
  type hints against concrete classes, not structural Protocols). Full
  suite: `uv run pytest -m "not camera"` -> **1162 passed, 40
  deselected, 0 failed** (baseline 1142, post ticket 003 -- this ticket
  added 20 net new passing tests: 13 + 6 in the two files above, plus 1
  from correcting an off-by-one in the initial draft of the parity
  test).
- **Deviations from the plan**: none beyond the `daemon/service.py`
  module-docstring correction noted above (a pre-existing staleness
  from ticket 003, not introduced by this ticket, fixed in passing
  since this ticket's own change touches the same paragraph) and the
  `stream_images` scope reconciliation, both recorded for the
  team-lead/reviewer to confirm -- the second is a restatement of
  ticket 003's own precedent, not a new judgment call.
