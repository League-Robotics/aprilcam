---
id: '027'
title: Streams
status: planning-docs
branch: sprint/027-streams
worktree: false
use-cases: []
issues:
- streams-for-real-time-consumers.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 027: Streams

## Goals

Implement the last two unimplemented `Daemon` methods, `stream_images`
and `stream_tags` (`daemon/interfaces.py`), closing the gap between
what the wire contracts already specify (`specification.md` §4.1,
`design.md` §5) and what the daemon actually does. Concretely:

- The viewer (`view/viewer.py`) stops polling `get_tags`/`get_frame` at
  ~2 Hz (sprint 022's explicit "smaller, honest first step") and
  subscribes to a real image stream instead.
- The tag-history ring buffer (`daemon/detection.py`) becomes
  wall-clock bounded once a tag-stream subscriber exists, closing
  sprint 021's recorded scope boundary (its own module docstring:
  "accepted, honest consequence of shipping burst detection before
  streaming... revisit once a background/streaming producer can
  genuinely fill the buffer at frame rate").
- Robot control loops and a future ROS bridge (`adapters.md` §4.3,
  "streams only") become possible — not built this sprint, but this
  sprint is their prerequisite.

## Problem

Every product the daemon publishes today is fetched one shot at a
time: a poll for tags, a poll for a frame. Nothing in this system
computes continuously and pushes. This is fine for an MCP agent (which
explicitly does not stream — UC-07/UC-11's Interfaces lines say
"MCP: out"), but it is wrong for a robot control loop (A-01, which
"cannot tolerate multi-hundred-ms stalls" per `use-cases/README.md`)
and it is why the viewer is capped at 2 Hz and the ring buffer's
"~10 seconds" claim is only true when something happens to poll at
frame rate.

## Solution

Add a new in-process streaming subsystem inside the daemon
(`daemon/streaming.py` + `daemon/broadcast.py`) that runs a background
production loop per camera product — only while at least one
subscriber exists — and fans its output out to bounded, independently
draining subscriber queues with silent drop. Bridge that in-process
subscription to the wire two ways: a raw length-prefixed socket
transport (`daemon/stream_transport.py`, client-side
`client/streams.py`) for the actual bytes, and two new gRPC RPCs
(`GetImageStream`/`GetTagStream`) that only allocate the socket
endpoint — control-plane traffic stays on gRPC, bulk streaming data
does not, exactly as `design.md` §5 already specifies. Wire
`GrpcDaemon` (the client) and `view/viewer.py` (the first real
consumer) on top.

## Success Criteria

- Two subscribers to the same camera's deskewed image stream cause
  **one** warp, not two (UC-07.S-1).
- A slow subscriber drops frames without stalling a fast one or the
  producer (UC-07.S-2, UC-11's backpressure requirement).
- A quiet field still delivers tag-stream heartbeats, distinguishable
  from a dead stream (UC-11.S-2).
- Calibration going stale mid-stream reaches the subscriber in-band,
  on both the tag stream and the image stream (UC-11.S-3).
- Unsubscribing (closing the stream) returns the daemon to idle for
  that product: no background thread, no encode, no warp, no
  detection running for it (UC-07.S-3).
- The tag-history ring buffer fills at a steady, wall-clock cadence
  while a tag-stream subscriber exists (UC-26, closing sprint 021's
  scope boundary).
- The viewer (`view/viewer.py`) runs on the stream and beats 2 Hz.
- All of the above is verified headlessly, without live camera
  hardware — see "Hardware constraint this sprint" below. Real-camera
  verification is written as `@pytest.mark.camera` and documented as
  blocked, not silently skipped.

## Scope

### In Scope

- `daemon/broadcast.py` (new): a generic bounded-queue fan-out
  primitive (subscribe / publish / unsubscribe, silent drop on a full
  queue). No camera, vision, or proto knowledge.
- `daemon/streaming.py` (new): `TagStreamProducer` and
  `ImageStreamProducer` — per-(camera[, deskewed]) background threads,
  created lazily on first subscriber and torn down on last
  unsubscribe, driving `DetectionEngine.run_burst` / capture+encode(+
  warp) at a steady cadence and publishing through
  `daemon/broadcast.py`. Wires `DaemonService.stream_images`/
  `stream_tags`.
- `camera/camera.py`: a read lock on `Camera` so the new background
  producer and existing one-shot RPC reads never race on the
  underlying capture handle.
- `daemon/stream_transport.py` (new) + `proto/aprilcam_v2.proto` +
  `daemon/rpc.py`: the raw-socket wire transport
  (`[uint32 BE length][protobuf]`, separate sockets from the gRPC
  control plane) and the `GetImageStream`/`GetTagStream` endpoint-
  allocation RPCs.
- `client/streams.py` (new) + `client/daemon_client.py`: the
  client-side socket consumers and `GrpcDaemon.stream_images`/
  `stream_tags`.
- `view/viewer.py`: `run_raw_view`/`run_playfield_view` migrate from
  `PollLoop` to a stream-consuming loop.
- In-band staleness wiring (`TagFrame.calibration_stale`/
  `ImageFrame.calibration_stale`, both fields already exist since
  sprint 023) onto every published stream frame.
- Headless test infrastructure: a looping/fixture-replay fake camera
  backend so a background producer thread has continuous frames to
  read without hardware (development-plan.md Stage 8's own test list
  names this: "fake-camera daemon (fixture replay backend)").

### Out of Scope

- **MCP stream tools.** UC-07/UC-11 both say "MCP: out" — an MCP agent
  takes single frames (`get_frame`/`get_tags`), never streams. No MCP
  server changes this sprint.
- **A new CLI stream-dump verb.** UC-07/UC-11 list "CLI, Python" as
  their interfaces; that is satisfied by the viewer (a CLI entry
  point, ticket 007) plus the Python client library itself (a robot
  program is a Python script calling `daemon.stream_tags()` directly,
  which is "the CLI, Python interface," not a new subcommand). No new
  `aprilcam camera stream` command is added.
- **The ROS bridge.** Explicitly this sprint's prerequisite, not its
  job (`adapters.md` §4.3, `specification.md` §8).
- **The ~1 fps idle keepalive** development-plan.md Stage 8 mentions
  for hotplug/self-heal on a fully idle daemon. This sprint's idle
  gating is stronger and simpler — no background thread at all when
  there are zero subscribers — and self-heal continues to trigger from
  real read failures during an active production tick, exactly as
  today. A persistent low-rate keepalive independent of streaming is
  a distinct capability, deferred (see Open Questions).
- **Redirecting one-shot `get_frame`/`get_tags` to reuse a live
  producer's latest frame.** One-shot queries keep their existing,
  independent demand-driven behavior this sprint (see Design Rationale
  4). A camera with an active stream subscriber and a concurrent
  one-shot query performs two independent captures, made safe (not
  merged) by this sprint's camera read lock.
- **Tuning the detection cadence, image cadence, and tag-stream change
  threshold against real hardware.** Blocked this sprint — see below.
  Provisional values are recorded in Design Rationale/Open Questions.

## Hardware constraint this sprint

The physical playfield is currently obstructed (a cloth draped over
part of it) and the room is dark: zero tags detect right now. This is
a physical issue, not a code issue, and it is expected to clear before
long — but it means **no calibrated, tag-content-verified live-camera
check is possible this sprint**. Every hardware-dependent acceptance
criterion below is scoped as `@pytest.mark.camera`, documented as
blocked pending the stakeholder clearing the field, and carries no
pass/fail weight in this sprint's own closure — it exists so the check
is ready to run the moment the field is clear, not so it can be
silently skipped. Headless, fixture/fake-backend-based tests carry the
real verification weight for every acceptance criterion in this sprint
(framing correctness, backpressure/silent-drop, idle-gating thread
lifecycle, heartbeat timing, in-band staleness wiring, ring-buffer
wall-clock behavior under both a controlled fake clock and — ticket
008 — a real one, and the viewer's throughput beating
`DEFAULT_POLL_HZ` against both a scripted fast stream and — ticket 008
— a real camera).

**Reality check, recorded by ticket 008 (cross-checked against the
plan above, not assumed):** the original wording above reads as "no
live-camera verification is possible" flatly — running ticket 008's
`@pytest.mark.camera` suite found that overstated. A real
`arducam-ov9782-usb-camera` **is** attached in the environment ticket
008 executed in, and 3 of its 4 hardware tests do not depend on
calibration or on main-playfield's markers actually being visible —
they genuinely ran, against real hardware, and passed: real detection
cadence timing (23 ring-buffer entries over a real 2.01s window against
a real, if dark, capture), the real Tk viewer beating `DEFAULT_POLL_HZ`
(4.69 Hz observed, 38 frames/8.11s), and the Stage 8 CPU-idle-budget
check (1.325s combined pytest+daemon CPU over a real, unsubscribed 10s
window — consistent with `test_detection_hardware.py`'s own prior
1.16s reading for the equivalent detection-only case). Only the one
criterion that genuinely requires calibration against main-playfield's
markers — real two-subscriber-one-warp verification — stayed blocked:
`calibrate()` returned `succeeded=False` (matched a shifting subset of
`{1,2,3,4,5,6,7,8,9,10}`, always missing 2-3 ids run to run), which is
exactly what an obstructed/dark field predicts, and the test skips
cleanly with that data in its message rather than failing. See ticket
008's own completion notes for the full breakdown and what "the field
is clear" needs to mean before a re-run is expected to fully pass.

## Test Strategy

Headless-first, per development-plan.md's standing rule ("test without
hardware wherever possible") and this sprint's hardware constraint:

- **`daemon/broadcast.py`**: pure unit tests — N subscribers each get
  every publish; a subscriber whose queue is full silently drops
  without blocking the publisher or other subscribers; unsubscribe
  stops delivery; no camera/vision double needed at all.
- **`daemon/streaming.py`**: headless, using an extended/looping fake
  camera backend (development-plan.md Stage 8's "fixture replay
  backend") plus a fake/controllable clock:
  - zero subscribers → the producer thread does not exist (assert
    directly on internal state, not just behavior) → zero
    `DetectionEngine.run_burst`/encode/warp calls over a wall-clock
    window.
  - one tag-stream subscriber → `run_burst` at the configured cadence;
    the ring buffer grows wall-clock-bounded, not query-driven.
  - two image-stream subscribers on the same (camera, deskewed) →
    exactly one warp per tick (a call-counting double), both receive
    identical frames.
  - a slow subscriber (a queue that is never drained) drops frames; a
    fast subscriber on the same producer is unaffected.
  - a quiet fixture (identical frames every tick) still heartbeats at
    the configured interval.
  - flipping calibration stale mid-stream (the same test hook
    `daemon/detection.py`'s own tests use) surfaces
    `calibration_stale=True` on the very next published frame, both
    tag and image streams.
  - last unsubscribe tears the producer thread down; a following
    resubscribe starts a fresh one.
- **`camera/camera.py`**: two threads calling `read()` concurrently
  against a `FakeBackend` never interleave/corrupt calls (call-count
  and ordering assertions), with and without the new lock, to prove
  the lock is load-bearing.
- **`daemon/stream_transport.py` + `client/streams.py`**: an in-process
  daemon (`DaemonService` + `rpc.py`'s gRPC server, the pattern
  `test_daemon_client.py` already uses) plus a real socket connection:
  framing round-trips byte-exact; a client that never reads still lets
  other clients receive frames; closing the client-side stream
  unsubscribes and idle-gates the daemon side.
- **`view/viewer.py`**: a fake `Daemon`/fake stream double (matching
  the existing "tests derive a fake `Window`" pattern already in this
  module) asserts the poll loop is gone and the effective frame
  throughput exceeds `DEFAULT_POLL_HZ` against a scripted fast stream.
- **`@pytest.mark.camera` tests** (written, not expected to run this
  sprint): two real subscribers cause one real warp; real detection
  cadence against `main-playfield`; the viewer visibly beats 2 Hz on
  real hardware; the Stage 8 CPU-idle-budget exit criterion. All
  explicitly documented as blocked pending field access (see ticket
  008).
- `uv run mypy` clean throughout (`tests/test_typing.py`'s standing
  gate) — every new interface derives from `daemon/interfaces.py`'s
  `ImageStream`/`TagStream` base classes per the zero-`Protocol` rule.

## Architecture

**Substantial** — this sprint introduces a new subsystem (background,
subscriber-gated stream production) with a new cross-module dependency
(a new module sitting between `camera/`+`vision/`+`calibration/` and
the RPC layer) and a new wire-transport channel (raw sockets alongside
the existing gRPC control plane). More than three modules are touched
(`daemon/streaming.py` new, `daemon/broadcast.py` new,
`daemon/stream_transport.py` new, `daemon/rpc.py`, `daemon/service.py`,
`camera/camera.py`, `client/daemon_client.py`, `client/streams.py` new,
`view/viewer.py`, `proto/aprilcam_v2.proto`). Full 7-step methodology,
diagrams included.

### Architecture Overview

**Step 1 — the problem.** Every `Daemon` product today is one-shot:
`get_frame`/`get_tags` synchronously drive a single capture (and, for
tags, a single `DetectionEngine.run_burst`) per RPC call, on whichever
thread the gRPC server's `ThreadPoolExecutor` happens to run it on.
Nothing in the codebase computes continuously. `stream_images`/
`stream_tags` need to add exactly that — continuous, subscriber-gated
production — without breaking the one-shot path, and while keeping
`design.md` §2's "compute once, publish to all" and "single vision
authority" principles intact.

**Step 2 — responsibilities.** Four distinct responsibilities, each
changing for different reasons:

1. *Fan-out with backpressure* — given one stream of values, deliver
   them to N independently-paced subscribers, dropping for a slow one
   without affecting the others. Pure queueing; no camera, no proto,
   no sockets. Changes only if the backpressure policy itself changes.
2. *Subscriber-gated production* — decide, per camera and per product
   (raw image, deskewed image, tags), whether a background loop should
   be running at all, and if so drive the existing capture/encode/
   warp/`DetectionEngine` machinery at a steady cadence. Changes when
   camera/vision/calibration integration changes.
2a. *Camera read safety* — a background reader and a one-shot RPC
   reader must not race on the same `Camera`'s underlying capture
   handle. Changes only if the camera layer's concurrency model
   changes.
3. *Wire transport* — get bytes from an in-process subscription to a
   remote client and back, using the length-prefixed raw-socket
   framing `design.md` §5 already specifies, plus the gRPC calls that
   allocate a socket endpoint. Changes when the transport/framing
   changes, independent of what is being streamed.
4. *Client consumption* — turn a socket connection back into typed
   Python objects (`ImageFrame`/`TagFrame`) behind the `Daemon`
   interface's `ImageStream`/`TagStream` contract, and give the first
   real consumer (the viewer) something to subscribe to instead of
   polling. Changes when client ergonomics change, independent of the
   daemon-side implementation.

**Step 3 — modules.**

| Module | Purpose (one sentence) | Boundary | Serves |
|---|---|---|---|
| `daemon/broadcast.py` (new) | Fan out one producer's output to bounded, independently-draining subscriber queues with silent drop. | No camera/vision/proto knowledge; operates on opaque payloads. | UC-07.S-2, UC-11's backpressure requirement |
| `daemon/streaming.py` (new) | Run a per-(camera[, product]) background production loop, only while ≥1 subscriber exists. | Depends on `camera/` (read), `daemon/detection.py` (`DetectionEngine`), `calibration/` (staleness/deskew); publishes through `daemon/broadcast.py`; never touches sockets or proto. | UC-07 (images), UC-11 (tags), UC-26 (ring buffer) |
| `camera/camera.py` (extended) | Make `Camera.read()` safe under a concurrent background reader and one-shot RPC readers. | A lock local to `Camera`; no behavior change for any existing single-threaded caller. | Correctness precondition for `daemon/streaming.py` |
| `daemon/stream_transport.py` (new) | Bridge an in-process `ImageStream`/`TagStream` subscription to a raw length-prefixed socket for a remote client. | Owns the unix/TCP listener + accept-loop + per-connection sender thread; calls `DaemonService.stream_images`/`stream_tags` per accepted connection; runs no vision or camera code. | UC-07/UC-11's wire contract |
| `daemon/rpc.py` (extended) | Marshal `GetImageStream`/`GetTagStream` — **endpoint allocation only**, never frame bytes. | Unchanged role: marshalling and error mapping only. | UC-07/UC-11 main-flow step 1 |
| `client/streams.py` (new) | Client-side socket consumers (`SocketImageStream`, `SocketTagStream`), decoding via `client/models.py`'s `from_proto`. | cv2-free; no business logic; wire decode + Python iteration only. | UC-07/UC-11 (client side) |
| `client/daemon_client.py` (extended) | `GrpcDaemon.stream_images`/`stream_tags`: call the new RPCs, then hand off to `client/streams.py`. | Unchanged role: thin gRPC wrapper. | UC-07/UC-11 (client side) |
| `view/viewer.py` (extended) | Replace `PollLoop` with a stream-consuming loop for both raw and playfield views. | Unchanged public surface (`run_raw_view`/`run_playfield_view`); internals only. | Issue's "the viewer stops polling" |

**Step 4 — diagram.** Required: a new subsystem plus new cross-module
dependencies (3+ modules touched, `daemon/streaming.py` is a brand-new
consumer of `camera/`+`daemon/detection.py`+`calibration/`).

```mermaid
graph LR
    CAM["camera.Camera\n(+ read lock)"] --> STREAM["daemon.streaming\nImageStreamProducer /\nTagStreamProducer"]
    DET["daemon.detection\nDetectionEngine"] --> STREAM
    CALIB["calibration\n(staleness / deskew)"] --> STREAM
    STREAM --> BCAST["daemon.broadcast\nBroadcaster"]
    BCAST --> SVC["daemon.service.DaemonService\nstream_images / stream_tags"]
    SVC --> XPORT["daemon.stream_transport\nsocket accept + sender loop"]
    RPC["daemon.rpc\nGetImageStream / GetTagStream"] -.endpoint alloc only.-> SVC
    XPORT ==["[len][protobuf] socket"]==> CSTREAM["client.streams\nSocketImageStream /\nSocketTagStream"]
    CSTREAM --> GRPC["client.daemon_client\nGrpcDaemon"]
    GRPC --> VIEWER["view.viewer"]
    GRPC --> ROBOT["robot program\n(client library)"]
```

No dependency-direction change from today: `daemon/streaming.py` sits
at the same layer `daemon/detection.py` already occupies (a
daemon-internal consumer of `camera/`/`vision/`/`calibration/`), it
just adds a sibling module rather than inverting anything. No ERD —
this sprint changes no persisted data model (annotations, mount
registry, calibration, registry files are all untouched).

**Step 5 — What Changed / Why / Impact / Migration** are captured
above (Solution, Design Rationale, Migration Concerns) and in the
per-module table.

### Design Rationale

**1. Separate in-process production from socket transport, unlike
v1's coupled `_BaseProducer`.**
*Context*: v1's `ImageStreamProducer`/`TagStreamProducer`
(`archive/src/aprilcam/daemon/stream.py`) mixed capture/publish logic,
fan-out, and raw socket accept/sender threads in one class hierarchy.
*Alternatives*: (a) port that coupled design as-is; (b) split
production from transport as this sprint does.
*Why*: `DaemonService.stream_images`/`stream_tags` must be usable
in-process (tests, and the `Daemon` interface contract itself says
nothing about sockets) — coupling to sockets would also break the
existing `daemon/rpc.py` convention that only the RPC/transport layer
touches the wire, and would force every headless test of idle-gating
or backpressure through a real socket.
*Consequences*: one extra, thin bridging module
(`daemon/stream_transport.py`); an extra indirection (producer →
`Broadcaster` → subscriber queue → socket sender) that v1 did in one
hop. Worth it given hardware is currently blocked and headless testing
must carry this sprint's full verification weight.

**2. Idle gating is "the producer thread does not exist," not "the
producer thread exists but throttles."**
*Context*: the issue and development-plan.md Stage 8 both require zero
encode/warp/detection with no subscribers.
*Alternatives*: an always-on producer thread per camera that sleeps
near-zero when idle; a persistent low-rate (~1 fps) keepalive loop
(Stage 8 mentions this for hotplug/self-heal).
*Why*: a nonexistent thread makes "zero work" structurally true, not
merely behaviorally true — this mirrors `DetectionEngine`'s own
declared invariant ("no loop, no timer... an idle daemon runs zero
detections by construction, not because a scheduler decided not to
run one") exactly, extended from one-shot bursts to a persistent-
while-subscribed thread.
*Consequences*: first-subscriber latency (thread start, detector
cold-build) on every 0→1 transition — the same accepted trade
`DetectionEngine`'s lazy per-camera detector already makes. The Stage
8 idle keepalive is explicitly **not** built this sprint (see Out of
Scope / Open Questions); self-heal keeps triggering from real read
failures during an active production tick, unchanged from today.

**3. The ring buffer's wall-clock fix comes from the `TagStreamProducer`
driving `run_burst` at a fixed cadence while ≥1 subscriber exists —
not a separate always-on loop.**
*Context*: sprint 021 accepted the ring buffer as query-driven; this
issue names streaming as the fix.
*Alternatives*: an unconditional background detection loop (violates
idle gating); documenting the limitation without fixing it.
*Why*: ties the fix to actual demand — the buffer is wall-clock-bounded
exactly when someone is consuming a live tag stream, which is the
condition under which its "~10 seconds" claim needs to hold.
*Consequences*: a camera with a tag-stream subscriber gets a wall-
clock buffer; a camera queried only via one-shot `get_tags` keeps
sprint 021's query-driven growth unchanged — intentional, not a
regression, and stated as such in `daemon/detection.py`'s docstring
update.

**4. One-shot queries stay fully independent of any active producer
this sprint.**
*Context*: "compute once, publish to all" could be read as applying to
every consumer, not just stream subscribers of the same product.
*Alternatives*: redirect a one-shot `get_frame`/`get_tags` to the
active producer's latest cached frame when one exists, avoiding a
duplicate capture.
*Why*: the spec's "compute once" language is scoped to UC-07.S-1's
literal two-*stream-subscriber* scenario. Redirecting one-shot queries
too would change `get_frame`/`get_tags`'s existing demand-driven
contract (sprints 021/022) for every current caller — a bigger change
than this issue asks for, and no use case demands it.
*Consequences*: a concurrent one-shot query against a camera with an
active stream performs its own independent capture; the new
`camera/camera.py` read lock (module 5) is what makes that safe rather
than racy, not a merge of the two paths. Recorded as an explicit open
question for a future sprint, not solved here.

**5. The tag-stream change threshold moves from v1's pixel-space
comparison to a world-cm comparison when calibrated.**
*Context*: v1 (`archive/.../stream.py`'s `TagStreamProducer`) compared
`cx_px`/`cy_px` against an 8 px threshold; v2 is world-first
end-to-end and v1 never had world coordinates at all.
*Alternatives*: keep comparing in pixel space unconditionally (a
straight port); drop change-detection and always publish at the rate
cap.
*Why*: comparing in whatever space is actually the contract keeps
"did anything meaningfully change" honest — a pixel-space comparison
on a calibrated camera would fire on capture noise unrelated to real
motion once world coordinates exist, and world cm is what UC-11's
contract actually promises subscribers.
*Consequences*: for an uncalibrated camera (no `world`, diagnostic
pixels only), change detection falls back to pixel comparison exactly
as v1 did; the world-cm threshold's numeric value is provisional this
sprint (see Open Questions) — it cannot be tuned against real motion
because the field is dark.

### Migration Concerns

- Existing one-shot `get_frame`/`get_tags` callers see **no** behavior
  change (Design Rationale 4).
- `view/viewer.py`'s public functions (`run_raw_view`/
  `run_playfield_view`) keep their existing signatures; only their
  internals move from polling to streaming. `cli/camera.py`/
  `cli/playfield.py` need no changes (ticket 007 confirms this).
- New proto messages/RPCs are pure additions — `aprilcam_v2.proto`'s
  own evolution rule ("every message/field number below is permanent")
  is unbroken; nothing existing is removed or renumbered.
- `daemon/interfaces.py`'s `ImageStream`/`TagStream` base classes
  already exist, untouched; this sprint supplies their first real
  implementations, both server- and client-side.
- **Clean daemon shutdown.** `DaemonService` must track every live
  producer (per camera, per product) and stop them — joining their
  threads with a bounded timeout, mirroring
  `daemon/rpc.py`'s `_GrpcListener.stop`'s own "wait for full
  shutdown" discipline — when `DaemonLifecycle.stop()` runs, so a
  daemon restart or `daemon stop` never leaves an orphaned background
  thread or a stream socket bound after the process is meant to be
  down. Ticket 003 (the first producer ticket) owns this; ticket 005
  verifies it end-to-end once sockets exist.
- **Hardware-blocked acceptance**: real-camera CPU-idle-budget numbers
  (Stage 8's exit criterion), a real viewer visibly beating 2 Hz, and
  real two-subscribers-one-warp verification against actual hardware
  were the criteria expected to be uncapturable this sprint.
  `@pytest.mark.camera` tests are written, correctly marked, and skip
  cleanly on a machine with no attached camera. **Ticket 008's actual
  run corrected the plan's assumption**: a camera *was* attached where
  it executed, so the CPU-idle-budget check and the real-viewer-beats-
  2-Hz check both ran for real and passed (see "Hardware constraint
  this sprint" above for the numbers) — only the calibration-dependent
  two-subscribers-one-warp check stayed genuinely blocked, skipping
  cleanly with the real `calibrate()` failure data (missing marker ids)
  in its message. Ticket 008's completion notes name exactly this one
  remaining criterion and what "the field is clear" needs to mean
  before it is expected to pass. Headless fixture/fake-backend tests
  (now including ticket 008's real-clock, real-socket integration
  suite) carry this sprint's full verification weight for everything
  not gated on a physically clear field.

### Open Questions

1. **Detection cadence, image cadence, and the world-cm change
   threshold are provisional constants.** Chosen honestly (a starting
   guess, like `DEFAULT_POLL_HZ`/`CONFIRMATION_STREAK` before them),
   not tuned — real-hardware tuning is blocked by the obstructed,
   dark field this sprint. Revisit once the field is clear.
2. **Should one-shot queries eventually share a live producer's latest
   frame** instead of capturing independently (Design Rationale 4)?
   Deferred, not decided.
3. **Does `GetImageStream`/`GetTagStream` reuse one already-live
   listener across repeated allocation calls for the same (camera,
   deskewed), or allocate a fresh one each time?** Recommend reuse
   (matches v1, minimizes socket churn), but the implementer must
   guard against a second concurrent deskewed geometry request for the
   same camera while one is already active — "frame geometry stable
   for the life of a stream" (UC-07) must not be violated by a second
   subscriber arriving with different assumptions.
4. **The ~1 fps idle keepalive** development-plan.md Stage 8 names for
   hotplug/self-heal on a fully-idle daemon is explicitly deferred
   past this sprint (Out of Scope, Design Rationale 2) — not the same
   thing as this sprint's per-product idle gating.

## Use Cases

**Substantial** — full use-case treatment, per the effort decision
above.

### SUC-001: Subscribe to a camera's tag stream
Parent: UC-11

- **Actor**: A-01 (a robot program)
- **Preconditions**: UC-05 satisfied (camera configured, linked,
  calibrated) for world-coordinate records; an uncalibrated camera may
  still subscribe and receives pixel-only, uncalibrated records.
- **Main Flow**:
  1. Client calls `daemon.stream_tags(camera)`.
  2. If no producer exists yet for this camera, the daemon starts one:
     a background thread begins driving `DetectionEngine.run_burst` at
     a steady cadence.
  3. The daemon publishes a `TagFrame` whenever detections change
     (world-cm when calibrated, pixel when not), rate-capped, plus a
     heartbeat when nothing has changed for the heartbeat interval.
  4. The client iterates the returned `TagStream`, receiving `TagFrame`
     records until it closes the stream.
  5. Closing the stream unsubscribes; if it was the last subscriber,
     the daemon tears the producer down and detection for this camera
     stops.
- **Postconditions**: No lasting state — the ring buffer retains
  whatever accumulated while the stream was open (bounded, per UC-26).
- **Acceptance Criteria**:
  - [ ] A moving tag's stream shows world x/y changing, matching
        UC-11.S-1.
  - [ ] A quiet field still delivers heartbeats at the configured
        interval (UC-11.S-2).
  - [ ] Calibration going stale mid-stream sets
        `TagFrame.calibration_stale=True` on the very next published
        frame (UC-11.S-3) — verified headlessly via the same
        stale-injection hook `daemon/detection.py`'s own tests use.
  - [ ] A slow subscriber drops frames without affecting a fast
        subscriber on the same camera (headless, call-counting/queue
        assertions).
  - [ ] Unsubscribing (closing the stream) stops the background
        thread; `run_burst` is called zero more times for that camera
        until a new subscriber arrives.
  - [ ] While a tag-stream subscriber is active, the ring buffer's
        span is wall-clock bounded (UC-26), not query-driven.

### SUC-002: Subscribe to a camera's image stream, raw or deskewed
Parent: UC-07

- **Actor**: A-03 (developer/CLI), A-01 (robot program, raw path)
- **Preconditions**: As UC-06 — deskewed requires calibration; raw does
  not.
- **Main Flow**:
  1. Client calls `daemon.stream_images(camera, deskewed=...)`.
  2. If no producer exists yet for this (camera, deskewed) pair, the
     daemon starts one: a background thread captures, (warps if
     deskewed,) and JPEG-encodes at a steady cadence, embedding the
     latest `TagFrame` and (deskewed only) `AnnotationSet` exactly as
     `get_frame` does today.
  3. Every published frame is fanned out to every current subscriber
     of this (camera, deskewed) pair — computed once, shared, never
     recomputed per subscriber.
  4. The client iterates the returned `ImageStream` until it closes it.
  5. Closing the stream unsubscribes; last-subscriber-leaves stops the
     producer.
- **Postconditions**: No lasting state.
- **Acceptance Criteria**:
  - [ ] Two subscribers to the same (camera, deskewed=True) stream
        receive frames with identical metadata, and the warp routine
        is called once per tick, not twice (UC-07.S-1, headless
        call-counting double).
  - [ ] A slow subscriber drops frames; a fast subscriber on the same
        producer is unaffected (UC-07.S-2).
  - [ ] Frame geometry (`px_per_cm`/`world_extent`) is stable for the
        life of a stream.
  - [ ] Last subscriber leaving stops capture/encode/warp for that
        (camera, deskewed) pair (UC-07.S-3).
  - [ ] Calibration going stale mid-stream sets
        `ImageFrame.calibration_stale=True` on the very next published
        deskewed frame.

### SUC-003: The viewer runs on a stream instead of polling
Parent: UC-07 (also closes the issue's named viewer item)

- **Actor**: A-03
- **Preconditions**: `aprilcam view <camera>` / `playfield view
  <camera>` as today.
- **Main Flow**:
  1. `run_raw_view`/`run_playfield_view` call `daemon.stream_images`
     instead of polling `get_frame`/`get_tags` on a timer.
  2. A background reader thread keeps the Tk main loop responsive
     while frames arrive, always displaying the latest received frame
     (older undisplayed frames are dropped, matching the streaming
     backpressure model, not queued for later display).
  3. `playfield view` reads `frame.tags`/`frame.annotations` directly
     off each streamed deskewed `ImageFrame` — no separate `get_tags`
     call, since a deskewed frame already carries everything needed
     (`ImageFrame`'s own contract).
- **Postconditions**: Window behavior is otherwise unchanged
  (`calibration_stale`/`frame_dark` still surface as status text,
  never drawn on the image; closing the window still stops the loop).
- **Acceptance Criteria**:
  - [ ] `run_raw_view`/`run_playfield_view` keep their existing public
        signatures — `cli/camera.py`/`cli/playfield.py` need no
        changes.
  - [ ] `PollLoop`/timer-driven fetch is gone from the streaming path
        (the old poll-based code may remain only if still exercised by
        a documented fallback — otherwise removed, not left dead).
  - [ ] Headless: a scripted fast fake stream drives an effective
        display throughput measurably above `DEFAULT_POLL_HZ` (2 Hz).
  - [ ] `NotCalibrated` on `playfield view` still propagates untouched
        (no raw-frame substitution) — UC-18's "never fall back" rule,
        unchanged by this ticket.

### SUC-004: Every tag/image stream honors backpressure and idle gating
Parent: UC-07, UC-11 (the shared cross-cutting acceptance both build on)

- **Actor**: A-01, A-03
- **Preconditions**: A running daemon.
- **Main Flow**: As SUC-001/SUC-002, with focus on the shared
  `daemon/broadcast.py` primitive both producers use.
- **Postconditions**: None — a cross-cutting property, not a new
  capability.
- **Acceptance Criteria**:
  - [ ] `daemon/broadcast.py` unit tests: N subscribers each receive
        every publish; a full subscriber queue drops silently
        (`queue.Full` handled, never raised to the publisher); an
        unsubscribed queue receives nothing further.
  - [ ] Zero subscribers on any (camera, product) → the producer
        thread does not exist (asserted directly, not just
        behaviorally) → zero encode/warp/`run_burst` calls over a
        wall-clock window.
  - [ ] `camera/camera.py`'s new read lock: two threads calling
        `read()` concurrently against a `FakeBackend` never interleave
        or corrupt call ordering.

## GitHub Issues

(None linked yet — this sprint implements `clasi/issues/
streams-for-real-time-consumers.md` directly, tracked via this
sprint's `issues:` frontmatter.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [x] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Wire framing + Broadcaster fan-out primitive | — |
| 002 | Camera read synchronization | — |
| 003 | Tag producer with idle-gated detection | 001, 002 |
| 004 | Image producer with shared deskew computation | 001, 002 |
| 005 | GetImageStream/GetTagStream RPCs and socket transport | 001, 003, 004 |
| 006 | Client-side stream consumption | 005 |
| 007 | Viewer migration off polling | 006 |
| 008 | Integration/acceptance pass and hardware-blocked documentation | 003, 004, 006, 007 |

Tickets execute serially in the order listed. **No worktree opt-in for
this sprint** (`worktree: false` above) — tickets 003 and 004 are
structurally independent of each other (both depend only on 001/002)
but must still run one after the other on the shared branch, never
concurrently, same as every other pair in this list.
