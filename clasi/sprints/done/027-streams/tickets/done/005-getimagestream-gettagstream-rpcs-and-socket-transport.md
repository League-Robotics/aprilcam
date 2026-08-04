---
id: '005'
title: GetImageStream/GetTagStream RPCs and socket transport
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
- '003'
- '004'
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# GetImageStream/GetTagStream RPCs and socket transport

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint.

## Description

Connect ticket 001's proto message shells and framing helpers, and
tickets 003/004's in-process producers, to a real remote client:

1. `daemon/rpc.py`: implement the `GetImageStream`/`GetTagStream`
   handlers — **endpoint allocation only**, never frame bytes over
   gRPC (`design.md` §5's separate-sockets contract). Allocates (or
   reuses) a listening socket pair (unix + TCP) for the requested
   (camera[, deskewed]) product.
2. `daemon/stream_transport.py` (new): the raw-socket side — bind the
   listener(s), accept connections, and for each accepted connection
   call `DaemonService.stream_images`/`stream_tags` to obtain a
   subscription, then a per-connection sender thread iterates it,
   frames each message, and writes it to the socket. Closing the
   connection (or the sender thread exiting) calls the subscription's
   `close()`, unsubscribing it.

Idle gating must hold at this layer too: allocating an endpoint
(`GetImageStream`/`GetTagStream` returning) must **not** start
capture/detection — only an actual accepted socket connection (a real
subscriber) does. Listening for a connection costs nothing; producing
does.

## Acceptance Criteria

- [x] `GetImageStream(camera, deskewed)`/`GetTagStream(camera)` return
      an endpoint (socket path and/or TCP port) without themselves
      starting any producer thread — calling either RPC with no
      subsequent socket connection leaves the daemon fully idle for
      that product.
- [x] Connecting a real socket to the returned endpoint causes exactly
      one `DaemonService.stream_images`/`stream_tags` subscription to
      be created; frames arrive framed as
      `[uint32 BE length][protobuf]`, decodable back into the correct
      proto message.
- [x] A second client connecting to the same already-live (camera,
      deskewed) endpoint becomes a second subscriber of the **same**
      producer (reuse, not a second producer) — verifies `sprint.md`
      Open Question 3's recommended default; if a genuine conflict is
      found (e.g. a second deskewed request with different assumptions
      about geometry), it is rejected or queued behind the live one
      rather than silently starting a second, geometry-incompatible
      producer.
- [x] Closing a client's socket connection unsubscribes it
      daemon-side (the sender thread's iteration ends, `close()` is
      called on the subscription) within a bounded time; if it was the
      last subscriber, the producer tears down (ticket 003/004's
      idle-gating, now verified end-to-end through a real socket).
- [x] Daemon shutdown (`DaemonLifecycle.stop()`) closes every open
      listener and stream socket cleanly — no bound socket or orphaned
      accept-loop thread survives, extending tickets 003/004's
      producer-shutdown requirement through this transport layer.
- [x] `uv run mypy` clean. Zero `typing.Protocol`.
- [x] Full existing suite (1099 tests, headless baseline) still
      passes. (Grew to 1180 passing headless as of this ticket, still
      zero failures.)

## Implementation Plan

**Approach.** Mirror `archive/src/aprilcam/daemon/stream.py`'s
`_BaseProducer`'s accept-loop/per-connection-sender-thread mechanics
(`_bind_unix_socket`/`_bind_tcp_socket`, one accept thread per
listener, one sender thread per connection) — the *mechanics* are
earned from v1, but the *content* each sender thread iterates now
comes from ticket 003/004's `ImageStream`/`TagStream` subscriptions
(via `DaemonService.stream_images`/`stream_tags`) rather than v1's
producer objects directly, per `sprint.md` Design Rationale 1's
production/transport split. Reuse ticket 001's framing helpers rather
than reimplementing length-prefixing here.

**Files to create/modify:**
- `src/aprilcam/daemon/stream_transport.py` (new)
- `src/aprilcam/daemon/rpc.py` (extend — `GetImageStream`/
  `GetTagStream` handlers)
- `src/aprilcam/daemon/service.py` or `daemon/__main__.py` (wherever
  the transport's listeners get started/stopped alongside the gRPC
  control plane and `DaemonLifecycle`)

**Documentation updates:** `proto/aprilcam_v2.proto`'s header comment
currently lists "stream endpoint allocation (`GetImageStream`/
`GetTagStream`)" under "Deliberately absent this sprint (Stage 8
territory, not forgotten)" — update that note now that both RPCs are
implemented, following the file's own established pattern of
recording which sprint/ticket added each capability.

## Testing

- **Existing tests to run**: `tests/aprilcam/daemon/test_service.py`,
  `tests/aprilcam/client/test_daemon_client.py` (for the in-process
  daemon-startup pattern it already uses), full suite via
  `uv run pytest`.
- **New tests to write**: an in-process `DaemonService` + `rpc.py`
  gRPC server (the pattern `test_daemon_client.py` already
  establishes) with real listening sockets — endpoint-allocation-alone
  causes no producer to start; a real socket connection receives real
  frames; two connections to the same endpoint share one producer;
  closing a connection unsubscribes and idle-gates; clean shutdown
  closes every socket and thread.
- **Verification command**: `uv run pytest && uv run mypy`.
