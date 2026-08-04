---
id: '001'
title: Wire framing + Broadcaster fan-out primitive
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-004
depends-on: []
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire framing + Broadcaster fan-out primitive

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint, even one that does
not depend on it (e.g. ticket 002).

## Description

Build the two foundational, camera-agnostic pieces every later ticket
in this sprint depends on:

1. `daemon/broadcast.py`'s `Broadcaster` — a bounded-queue fan-out
   primitive: subscribe, publish (to every current subscriber),
   unsubscribe. A full subscriber queue drops the new item silently
   (never raises, never blocks the publisher). This is the mechanism
   that makes "two subscribers, one computation" and "a slow
   subscriber never stalls a fast one" true by construction in tickets
   003/004 — see `sprint.md`'s Design Rationale 1 and 2.
2. The `[uint32 BE length][protobuf]` wire framing (`design.md` §5)
   used by every stream socket, both directions — a write helper and a
   read helper, transport-agnostic (no daemon or client knowledge),
   plus the new proto messages for `GetImageStream`/`GetTagStream`
   (an endpoint — socket path and/or TCP port — mirroring v1's
   `StreamEndpoint` shape).

No camera, vision, or gRPC *handler* wiring in this ticket — the two
RPCs are declared in the `.proto` service but their handlers stay
unimplemented until ticket 005. Everything here is testable against
synthetic in-process data and a real local socket pair, no fixtures
needed.

## Acceptance Criteria

- [x] `Broadcaster` (or equivalently named class in
      `daemon/broadcast.py`): N independent subscribers, each with its
      own bounded queue (default depth 2, a named/overridable
      constant); a publish delivers to every current subscriber; a
      full subscriber queue silently drops the new item (no exception
      to the publisher, no blocking); unsubscribing stops further
      delivery to that subscriber without affecting any other.
- [x] Framing helpers: a write function that prefixes
      `struct.pack(">I", len(payload))` to a payload, and a read
      function that reads exactly one length-prefixed message,
      raising a clear error on a short read / connection closed
      mid-message. Adapted from (not copied wholesale from)
      `archive/src/aprilcam/client/stream.py`'s
      `_read_length_prefixed`/`_recv_exactly` — no camera/tag types
      imported at this layer, it operates on raw bytes only.
- [x] `proto/aprilcam_v2.proto` gains `GetImageStreamRequest`/
      `GetImageStreamResponse` and `GetTagStreamRequest`/
      `GetTagStreamResponse` messages (endpoint fields only) and
      declares both RPCs on the service; regenerated via
      `scripts/compile_proto.py`. Every existing message/field number
      is untouched, per the proto file's own evolution rule stated in
      its header comment.
- [x] `uv run mypy` clean. Zero `typing.Protocol` anywhere in new
      code — any subscriber-handle type is a plain class or frozen
      `@dataclass(frozen=True, kw_only=True)`, never a Protocol
      (`daemon/interfaces.py`'s module docstring, `CLAUDE.md`).
- [x] Full existing suite (1099 tests, headless baseline) still
      passes with no regressions.

## Implementation Plan

**Approach.** Start from `archive/src/aprilcam/daemon/stream.py`'s
`_BaseProducer` fan-out logic (per-connection `queue.Queue(maxsize=N)`,
`put_nowait`/`queue.Full` silent-drop, its `_MAX_SENDER_QUEUE = 2`
constant) as the earned starting point — but strip out everything
socket-related. `Broadcaster` in this ticket takes and hands back
plain Python objects (bytes or dataclass instances), never a
`socket.socket`; that split is deliberate (`sprint.md` Design
Rationale 1) so `daemon/streaming.py` (tickets 003/004) is testable
without a real socket, given hardware is currently blocked. For
framing, port `archive/src/aprilcam/client/stream.py`'s
`_recv_exactly`/`_read_length_prefixed` pattern into a small, shared
module both `daemon/stream_transport.py` (ticket 005) and
`client/streams.py` (ticket 006) will import later — do not duplicate
the framing logic between the daemon and client sides.

**Files to create/modify:**
- `src/aprilcam/daemon/broadcast.py` (new)
- `proto/aprilcam_v2.proto` (extend: new messages + RPC declarations;
  handlers stay unimplemented)
- `src/aprilcam/proto/aprilcam_v2_pb2.py` /
  `aprilcam_v2_pb2_grpc.py` (regenerate via
  `scripts/compile_proto.py` — never hand-edit)
- A new shared framing-helper module (implementer's choice of exact
  name/location, e.g. alongside `proto/` since it is proto-adjacent
  and used by both daemon and client sides — record the choice in the
  ticket's completion notes so ticket 005/006 know where to import
  from)

**Documentation updates:** none beyond code docstrings — this is
internal plumbing with no public-facing behavior yet.

## Testing

- **Existing tests to run**: full suite, `uv run pytest` (1099-test
  headless baseline; confirm no regressions).
- **New tests to write**: `tests/aprilcam/daemon/test_broadcast.py`
  (N-subscriber fan-out; full-queue silent drop; unsubscribe stops
  delivery — no camera/vision fixtures needed, pure in-process
  objects); a framing round-trip test over a real local `socket pair`
  (write then read byte-exact) plus a short-read/EOF-mid-message case.
- **Verification command**: `uv run pytest && uv run mypy`.

## Completion Notes

- **Baseline was 1099; suite now 1122** (`uv run pytest -m "not camera"
  -q` → `1122 passed, 40 deselected`, 81s). This ticket added 23 new
  tests (12 in `test_broadcast.py`, 11 in `test_framing.py`). `uv run
  mypy` → "Success: no issues found in 73 source files". Zero
  `typing.Protocol` in new code.
- **Framing module location: `src/aprilcam/framing.py`, not
  proto-adjacent.** The ticket's plan suggested "alongside `proto/`"
  as one option; that directory is excluded from `[tool.mypy]`'s
  `files`/`exclude` (`^src/aprilcam/proto/`, reserved for
  `compile_proto.py`'s generated output), so a hand-written module
  placed there would silently escape the "mypy clean" gate rather than
  actually satisfy it. Placed instead at the top level, alongside
  `types.py`/`errors.py`/`guides.py` — the existing precedent for a
  dependency-free module both `daemon/` and `client/` import without
  either becoming dependent on the other. **Ticket 005
  (`daemon/stream_transport.py`) and ticket 006 (`client/streams.py`)
  import framing from `aprilcam.framing`, not from anywhere under
  `proto/`.**
- **`Broadcaster` API** (`src/aprilcam/daemon/broadcast.py`), generic
  over the published item type `T`:
  - `Broadcaster(*, queue_depth: int = DEFAULT_SUBSCRIBER_QUEUE_DEPTH,
    on_active: Optional[Callable[[], None]] = None, on_idle:
    Optional[Callable[[], None]] = None)` — `DEFAULT_SUBSCRIBER_QUEUE_DEPTH
    = 2` (v1's `_MAX_SENDER_QUEUE`, carried forward as a named,
    public, overridable constant).
  - `subscribe() -> queue.Queue[T]` — always a fresh queue.
  - `unsubscribe(q: queue.Queue[T]) -> None` — safe no-op on an
    unknown/already-removed queue (never raises).
  - `publish(item: T) -> None` — `put_nowait` to every subscriber
    current at call time; `queue.Full` is caught and swallowed per
    subscriber, never raised to the publisher, never blocks another
    subscriber's delivery.
  - `subscriber_count` (property) — observable count, the non-callback
    half of the 0↔1 hook.
  - **0↔1 transition mechanism**: `on_active`/`on_idle` constructor
    callbacks, fired exactly on the 0→1 and 1→0 transitions
    respectively (never on every subscribe/unsubscribe when the count
    doesn't cross that boundary) — proven in
    `test_on_active_fires_exactly_on_zero_to_one_transition`,
    `test_on_idle_fires_exactly_on_one_to_zero_transition`, and the
    full-lifecycle `test_on_active_and_on_idle_together_across_multiple_cycles`.
    Callbacks fire *outside* the internal lock, so an `on_active` that
    itself calls back into the same `Broadcaster` (e.g. a producer's
    first tick publishing synchronously) cannot deadlock.
  - **For ticket 003/004**: construct one `Broadcaster` per (camera,
    product) and pass `on_active=<start producer thread>`,
    `on_idle=<stop producer thread>` — that pairing *is* the idle-gating
    mechanism sprint.md Design Rationale 2 asks for ("the producer
    thread does not exist," not "exists but throttles"). No sentinel/
    close signalling was added to the queue itself — `unsubscribe`
    only stops future delivery; if a producer/consumer needs to unblock
    a thread parked in `queue.get()`, that's the caller's own concern
    (e.g. a bounded `get(timeout=...)` poll against a stop flag, the
    same pattern v1's `_sender_loop` already used), deliberately left
    out of this generic primitive.
- **`aprilcam.framing` API**: `pack_frame(payload: bytes) -> bytes`
  (pure — prefixes the 4-byte big-endian length, does not touch a
  socket) and `read_frame(sock: socket.socket) -> bytes` (reads one
  full length-prefixed message, raises `EOFError` — matching v1's
  `ImageStreamConsumer`/`TagStreamConsumer` convention — on a short
  read or connection closed mid-message, including mid-length-prefix).
  `test_round_trip_over_real_socket_pair` sends on a background thread
  (a 70 KB payload exceeds a `socket.socketpair()`'s default kernel
  buffer, so a synchronous `sendall` before any `read_frame` call would
  deadlock — a real caller always has the write and read happening
  concurrently on different threads/processes, so the test was fixed
  to match that instead of assuming a single-threaded send-then-read).
- **Proto**: `StreamEndpoint` (new shared message, `optional
  socket_path`/`optional tcp_port`, mirroring v1's message of the same
  name but with proto3 `optional` presence instead of v1's
  implicit-zero-is-absent convention), `GetImageStreamRequest/Response`,
  `GetTagStreamRequest/Response`, plus the two RPC declarations on
  `AprilCamV2`. No existing message or field number touched. Handlers
  are deliberately **not** wired in `daemon/rpc.py` — calling either
  RPC today hits gRPC's own generated `UNIMPLEMENTED` default; ticket
  005 implements `_Servicer.GetImageStream`/`.GetTagStream`.
- **Deviation from the plan**: none in scope — only the framing
  module's exact location (see above), which the plan explicitly left
  to the implementer's judgement and asked to be recorded here.
