---
status: in-progress
sprint: '027'
tickets:
- 027-001
- 027-002
- 027-003
- 027-004
- 027-005
- 027-006
- 027-007
- 027-008
---

# Streams: the last two unimplemented Daemon methods

## Description

`stream_images` and `stream_tags` are the final capabilities on
`Daemon` that still raise `NotImplementedError`. Everything downstream
of them is currently faked by polling.

**What exists**: `ImageStream` and `TagStream` are declared base classes
in `daemon/interfaces.py` with close-to-unsubscribe semantics, defined
next to the methods that return them precisely because unsubscription
is a *daemon-visible* event — when the last subscriber leaves, the
daemon drops that product to idle.

**What this fixes:**

- **The viewer stops polling.** `view/viewer.py` currently polls
  `get_tags` then `get_frame(deskewed=True)` at ~2 Hz (sprint 022
  Decision 3, an explicit "smaller, honest first step"). Streaming
  gives a real frame rate.
- **The ring buffer becomes wall-clock bounded.** Sprint 021 recorded
  this as an accepted scope boundary: detection is query-driven, so a
  sparsely-polled camera's `get_tag_history` spans far more than the
  designed ~10 seconds. With a stream subscriber, detection runs at a
  steady cadence and the buffer means what its docstring says.
- **Robot control loops become viable.** A robot needs tag updates at
  frame rate; `docs/design/use-cases/README.md`'s actor A-01 is
  explicit that it "cannot tolerate multi-hundred-ms stalls." Polling
  RPC per frame is not that.
- **The ROS bridge becomes possible** — `adapters.md` §4.3 defines it
  as **streams only**, republishing image and tag streams as ROS
  topics. It needs nothing else from v2 but stable stream contracts.

**Contracts already specified** (spec §4.1, design.md §5): separate
sockets with `[uint32 BE length][protobuf]` framing; the deskewed
stream computed **once and shared** so two subscribers cause one warp;
tag stream change-driven and rate-capped **with a heartbeat** so a
quiet field stays distinguishable from a dead stream; backpressure is
bounded per-subscriber queues with **silent drop** — a slow consumer
loses frames, never stalls the pipeline or another subscriber; frame
geometry stable for the life of a stream.

**Staleness must surface mid-stream** (UC-11.S-3): if calibration goes
stale while a stream is open, the subscriber is told rather than
silently served plausible wrong positions. The in-band carriers already
exist — `TagFrame.calibration_stale` and `ImageFrame.calibration_stale`
— and sprint 023 made staleness actually reachable, so this is wiring,
not new design.

**Idle gating is a requirement, not an optimization**: no subscribers
means no JPEG encode, no warp, no detection. The development plan's
stage 8 treats this as CPU-budget-tested behaviour, and sprint 021's
demand-driven detection already established the pattern.

## Cause

Deferred deliberately through sprints 020–024: one-shot queries were
enough to build and verify every layer beneath, and streaming is a
transport concern that benefits from a proven pipeline underneath it.

## Verification

- Two subscribers to one deskewed stream cause **one** warp, not two.
- A slow consumer drops frames without stalling a fast one.
- A quiet field still delivers heartbeats, distinguishable from a dead
  stream.
- Calibration going stale mid-stream reaches the subscriber.
- Unsubscribing returns the daemon to idle: detection and encode stop.
- The viewer runs on the stream and visibly beats 2 Hz.

## Related

- `src/aprilcam/daemon/interfaces.py` — `ImageStream`, `TagStream`,
  `stream_images`, `stream_tags`
- `docs/design/specification.md` §4.1; `docs/design/design.md` §5
- `docs/design/use-cases/daemon.md` UC-07, UC-11
- Sprint 027 in `docs/design/development-plan.md`'s sequence
