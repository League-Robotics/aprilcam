---
id: '007'
title: Viewer migration off polling
status: done
use-cases:
- SUC-003
depends-on:
- '006'
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Viewer migration off polling

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint.

## Description

`view/viewer.py`'s `run_raw_view`/`run_playfield_view` currently poll
`get_frame`/`get_tags` on a ~2 Hz timer via `PollLoop` — sprint 022's
own module docstring calls this "a smaller, honest first step,"
explicitly deferred until "the streaming subsystem exists and the two
can be compared" (Decision 3). That subsystem now exists (tickets
001-006). Migrate both view functions to subscribe to
`daemon.stream_images(camera, deskewed=...)` instead of polling.

`playfield view` gets a real simplification, not just a speed-up:
since a deskewed `ImageFrame` already embeds its `TagFrame` and
`AnnotationSet` (`types.py`'s contract, unchanged by this sprint), the
viewer no longer needs a separate `get_tags` call before `get_frame` —
one stream, one subscription, everything the renderer needs on every
yielded frame.

Because a blocking stream read cannot run on Tk's main thread without
freezing the window, this ticket needs a background reader thread
feeding a single "latest frame" cell that the Tk main loop samples on
its own pump cadence — older undisplayed frames are dropped, matching
this sprint's backpressure model (never queued for later display).

## Acceptance Criteria

- [x] `run_raw_view`/`run_playfield_view` keep their existing public
      signatures exactly — `cli/camera.py`/`cli/playfield.py` need no
      changes (confirm by reading both call sites, not just by
      inspection of the viewer module).
- [x] `run_raw_view` subscribes to `stream_images(camera,
      deskewed=False)`; no separate raw-frame polling code path
      remains reachable in normal use.
- [x] `run_playfield_view` subscribes to `stream_images(camera,
      deskewed=True)` only — it does **not** also call
      `stream_tags`/`get_tags`, reading `frame.tags`/
      `frame.annotations` directly off each streamed `ImageFrame`.
- [x] The Tk window stays responsive (closing it still stops the loop
      promptly) while a background reader thread is active — verified
      headlessly via the existing fake-`Window` pattern this module's
      tests already use (module docstring: "tests derive a fake so
      `PollLoop` is exercised with no real GUI toolkit instantiated").
- [x] Headless: a scripted fast fake stream (frames arriving faster
      than `DEFAULT_POLL_HZ` would have polled) drives a measurably
      higher effective display throughput than `DEFAULT_POLL_HZ`
      (2 Hz) — the "visibly beats 2 Hz" success criterion, reframed as
      a headless throughput assertion since real hardware is blocked.
- [x] `calibration_stale`/`frame_dark` still surface as window status
      text only, never drawn on the image — unchanged behavior from
      today, now driven by streamed frames instead of polled ones.
- [x] `NotCalibrated` on `playfield view` still propagates untouched —
      no raw-frame substitution, no `except` added around it (UC-18's
      "never fall back" rule, module docstring's existing "never falls
      back" note must remain true of the streaming path too).
- [x] `uv run mypy` clean. Zero `typing.Protocol`.
- [x] Full existing suite (1099 tests, headless baseline) still
      passes.

## Implementation Plan

**Approach.** Keep `Window`/`TkWindow` unchanged — only the
fetch-and-display loop changes. Replace `PollLoop`'s timer-driven
`fetch()` call with a background thread that iterates the `ImageStream`
returned by `stream_images(...)` and writes each decoded frame into a
single-slot cell (e.g. `threading.Lock` + a plain attribute, or
`queue.Queue(maxsize=1)` with a non-blocking "replace, don't append"
put) that the Tk main loop's pump cycle reads from non-blockingly.
Reuse `Window.pump()`'s existing responsiveness cadence
(`_PUMP_INTERVAL_S`) for how often the main loop samples the latest
frame — do not invent a second timing constant if the existing one
already fits. Decide whether `PollLoop` is renamed/replaced outright or
kept as dead code removed entirely — per the acceptance criteria,
prefer removal over leaving an unreferenced poll path, since this
codebase's convention (`development-plan.md`: "delete first, don't
preserve") favors deleting superseded code over keeping it dark.

**Files to create/modify:**
- `src/aprilcam/view/viewer.py` (the loop internals; public function
  signatures unchanged)

**Documentation updates:** update `view/viewer.py`'s module docstring
— it currently opens with "Polls; does not stream (Decision 3)" and
explains why at length; that section needs a full rewrite now that the
opposite is true, not a patch. `specification.md` §4.8/`design.md`'s
`view/` row already describe the target end state ("subscribes to the
deskewed stream") — confirm no further doc changes are needed there
(likely none, since those already describe this ticket's result, not
today's polling).

## Testing

- **Existing tests to run**: any existing `view/viewer.py` tests
  (locate via the module's own fake-`Window`/fake-`Daemon` test
  pattern), `tests/aprilcam/cli/test_camera_cli.py` (confirms CLI call
  sites are unaffected), full suite via `uv run pytest`.
- **New tests to write**: a scripted fake `ImageStream` (yields frames
  faster than 2 Hz) driving a fake `Window` — assert display calls
  exceed `DEFAULT_POLL_HZ`'s equivalent rate over a fixed wall-clock
  window; assert closing the fake window stops the reader thread
  promptly (no leaked thread after the test); assert `playfield view`
  never calls a `get_tags`-equivalent on the fake `Daemon` (only
  `stream_images`).
- **Verification command**: `uv run pytest && uv run mypy`.
