---
id: '004'
title: 'Daemon detection integration: ring buffer, get_tags/get_tag/get_tag_history'
status: open
use-cases: [SUC-001, SUC-002, SUC-003]
depends-on: ['002', '003']
github-issue: ''
issue: tag-detection-and-tag-queries.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon detection integration: ring buffer, get_tags/get_tag/get_tag_history

## Description

This is the sprint's core ticket: wires `vision/detector.py` (ticket
003) into the daemon as `daemon.interfaces.Daemon`'s already-declared
`get_tags`/`get_tag`/`get_tag_history` methods (no `interfaces.py`
changes needed — the sufficiency trace already verified these three
methods are sufficient for UC-09/UC-10/UC-26). Implements
**demand-driven scheduling**: each `get_tags`/`get_tag` call triggers
exactly one detection burst against the named camera; there is no
background thread this sprint. `get_tag_history` is a pure buffer
read — it triggers no detection of its own (sprint.md Architecture
Decision 1's full reasoning). Also implements `vision/motion.py` (a
small, separate pure-function module for velocity/speed/heading) and
extends `get_frame` to embed the latest ring-buffer `TagFrame` when one
exists.

**Every record this sprint reports `calibrated=False`.** No
calibration subsystem exists yet, so `world`, `world_velocity`,
`speed`, `heading_rad`, `yaw_rad`, and `world_corners` are all `None`
on every `TagRecord` this sprint produces — only `pixel`/
`pixel_corners` are populated. This is not a partial implementation to
finish later; it is the correct, honest output for an uncalibrated
camera (sprint.md's Decision 2, and the primary issue's explicit
acceptance rule). Do not compute a "pixel yaw" and put it in
`yaw_rad` — that field's contract is world-frame (0 = east, CCW
positive), which is meaningless without calibration.

## Acceptance Criteria

- [ ] `daemon/detection.py` exists with a per-camera ring buffer
      (bounded, ~300 entries, matching v1's `RingBuffer(maxlen=300)`
      sizing) and a burst function: given an open `Camera`, run
      `vision/pipeline.py` + `vision/detector.py`, reorder corners to
      the boundary/hat contract (per ticket 003's verified
      convention), build `TagRecord`s with `calibrated=False` and only
      `pixel`/`pixel_corners`/`tag`/`timestamp` set, wrap into a
      `TagFrame`, append to that camera's buffer, and return it.
- [ ] `vision/motion.py` exists: a pure function computing velocity,
      speed, and heading from a sequence of `(timestamp, WorldPoint)`
      pairs (`heading_rad = atan2(world_vy, world_vx)`, 0 = east, CCW
      positive — `types.py`'s stated convention). Not invoked with a
      non-`None` world position anywhere this sprint (nothing is
      calibrated) — tested directly against synthetic
      `(timestamp, WorldPoint)` sequences, not through the daemon.
- [ ] `DaemonService.get_tags(camera)` and `get_tag(camera, tag)` each
      trigger **exactly one** detection burst per call (verified with
      a call-counting/mock detector in tests — this is the CPU-budget
      regression pin, dev-plan Stage 8). `get_tag` filters the burst's
      `TagFrame` for the requested `TagId`, returning `None` when
      absent — never a zero-valued record (UC-09.S-4).
- [ ] `DaemonService.get_tag_history(camera, tag=None, frames=None)`
      triggers **zero** detection bursts of its own — it only reads
      the existing per-camera buffer. A buffer with fewer entries than
      `frames` requested returns everything available, not an error.
      When `tag` is given, each returned `TagFrame` still carries its
      real `timestamp`/`frame_index` even when that frame's `.tags`
      is empty for the requested tag — the explicit-gap requirement
      (UC-26.S-4); frames are never dropped to make the tag's presence
      look continuous.
- [ ] An idle daemon (zero `get_tags`/`get_tag`/`get_tag_history`
      calls) triggers zero detections over a simulated 10 s window —
      trivially true with no background loop, asserted as a named
      regression test anyway (dev-plan Stage 8 treats this as a
      requirement with CPU-budget tests, not an assumption).
- [ ] `DaemonService.get_frame` gains: if the named camera's ring
      buffer has an entry, the returned `ImageFrame.tags` carries that
      entry; if the buffer is empty for that camera, `tags` stays
      `None`. `get_frame` itself never triggers a new burst — asking
      for pixels is not asking for tags (sprint.md Decision 4).
      `get_frame`'s existing `deskewed=True` → `NotCalibrated`
      behavior is unchanged.
- [ ] `_camera_for` (ticket 002) is reused unmodified by all three new
      methods — number-or-slug addressing and `CameraNotPresent`
      apply to `get_tags`/`get_tag`/`get_tag_history` automatically,
      with a test proving it (e.g. `get_tags("3")` and
      `get_tags(<slug>)` on the same held camera agree).
- [ ] `uv run mypy` stays clean — no new `typing.Protocol` anywhere;
      if any new interface-shaped thing is needed beyond the
      already-declared `Daemon` methods, it is a plain base class
      (`NotImplementedError` bodies) or a frozen
      `@dataclass(kw_only=True)`, per `daemon/interfaces.py`'s module
      docstring. `uv run pytest` (including `tests/test_typing.py`)
      stays green; baseline is 216 passed before this sprint's work.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/
  tests/aprilcam/vision/ -v`, full suite, `uv run mypy`.
- **New tests to write**:
  `tests/aprilcam/daemon/test_detection.py` — ring buffer eviction at
  capacity; burst construction end-to-end using the `FakeBackend`
  pattern already established in `test_service.py` (a `Camera` backed
  by `FakeBackend` with queued synthetic frames containing renderable
  tag images, or a substitutable detector double for pure
  scheduling/counting tests that don't need real pixels).
  `tests/aprilcam/vision/test_motion.py` — velocity/speed/heading over
  synthetic `WorldPoint` sequences, including a zero-motion case and a
  known-heading case (e.g. due-east motion → `heading_rad == 0`).
  Extend `tests/aprilcam/daemon/test_service.py` — `get_tags`/
  `get_tag`/`get_tag_history` burst-counting assertions; the
  `get_frame` tag-embed test (empty buffer → `tags is None`; populated
  buffer → `tags` matches the latest entry, `get_frame` call itself
  adds no new buffer entry); the explicit-gap history test; the
  `calibrated=False`/`world is None` assertion on every field named in
  the Description.
- **Verification command**:
  `uv run pytest tests/aprilcam/daemon/ tests/aprilcam/vision/ -v &&
  uv run mypy`

## Implementation Plan

### Approach

1. `vision/motion.py` first (small, independent, no daemon
   dependency) — a function like
   `compute_motion(history: Sequence[Tuple[float, WorldPoint]]) ->
   Tuple[Optional[WorldPoint], Optional[float], Optional[float]]`
   (velocity, speed, heading), pure, no state.
2. `daemon/detection.py`: a small class or module-level functions
   owning `Dict[str, Deque[TagFrame]]` keyed by camera slug (the
   ring buffers), plus `run_burst(camera: Camera, detector, pipeline)
   -> TagFrame`. Consider constructor-injectable `detector`/pipeline
   config so tests can substitute a mock detector for pure
   burst-counting assertions without needing real pixel data.
3. `DaemonService` (extend, per the sprint's established pattern of
   `daemon/service.py` owning RPC method bodies): add `get_tags`,
   `get_tag`, `get_tag_history`, wire `_camera_for` for camera
   resolution, delegate detection work entirely to
   `daemon/detection.py` — no vision logic inline in `service.py`
   (matches the sprint architecture's module boundary table).
4. Extend `get_frame`'s existing body with the one-line ring-buffer
   read for `tags=`.
5. Corner reordering to the boundary/hat contract happens here (not
   in `vision/detector.py` — see ticket 003's note): this is where raw
   `Detection` objects become `TagRecord`s, so it is also where the
   family+id become a proper `TagId` and where the reorder to
   `(front-left, front-right, back-right, back-left)` is applied,
   using ticket 003's verified convention.

### Files to create/modify

- `src/aprilcam/vision/motion.py` (new)
- `src/aprilcam/daemon/detection.py` (new)
- `src/aprilcam/daemon/service.py` (modify — `get_tags`, `get_tag`,
  `get_tag_history`, `get_frame`)
- `tests/aprilcam/vision/test_motion.py` (new)
- `tests/aprilcam/daemon/test_detection.py` (new)
- `tests/aprilcam/daemon/test_service.py` (modify)

### Documentation updates

None beyond this sprint's `sprint.md` (already written) — no
`interfaces.py` changes, so no interface-contract docs to update.
