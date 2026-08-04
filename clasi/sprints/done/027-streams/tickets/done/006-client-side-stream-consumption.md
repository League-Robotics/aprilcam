---
id: '006'
title: Client-side stream consumption
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '005'
github-issue: ''
issue: streams-for-real-time-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Client-side stream consumption

**Execute strictly serially.** Sprint 027 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint.

## Description

Give `GrpcDaemon` (`client/daemon_client.py`) real implementations of
`stream_images`/`stream_tags`, completing the client side of the
`Daemon` interface — the last two of its methods still inherited
as-is from the base class (`client/daemon_client.py`'s own module
docstring currently says "Only these twenty-five methods are
implemented... `stream_images`/`stream_tags` remain the unimplemented
pair"). This is the first ticket where a real end-to-end round trip
(daemon → socket → client) becomes possible and testable.

Add `client/streams.py` (new): `SocketImageStream`/`SocketTagStream`,
deriving from `daemon/interfaces.py`'s `ImageStream`/`TagStream` base
classes, wrapping a raw socket connection opened against the endpoint
`GetImageStream`/`GetTagStream` returns, decoding each framed message
via `client/models.py`'s existing `from_proto` constructors (the
established proto boundary — this module never invents a second
decode path).

## Acceptance Criteria

- [x] `GrpcDaemon.stream_images(camera, deskewed=...)` calls the
      `GetImageStream` RPC, connects a socket to the returned endpoint
      (unix preferred when local, matching
      `archive/src/aprilcam/client/stream.py`'s `_host_is_local`
      precedent — TCP fallback for a remote daemon), and returns a
      `SocketImageStream` whose `__iter__` yields decoded
      `types.ImageFrame` objects until the connection closes or
      `.close()` is called.
- [x] `GrpcDaemon.stream_tags(camera)` does the same for
      `GetTagStream`/`TagFrame`.
- [x] `.close()` on either stream closes the socket; the daemon side
      observes the disconnect and unsubscribes (verified against
      ticket 005's daemon-side behavior, not merely asserted
      client-side).
- [x] `isinstance(client.stream_images(...), ImageStream)` and the
      `TagStream` equivalent are both true — mechanical inheritance
      from `daemon/interfaces.py`, per this codebase's zero-`Protocol`
      rule.
- [x] `client/streams.py` stays cv2-free (`tests/
      test_no_cv2_in_client_modules.py`'s existing check must pass
      against the new module without modification to that test).
- [x] A full in-process round trip (real `DaemonService` + `rpc.py`
      gRPC server + `daemon/stream_transport.py` + a real
      `GrpcDaemon` over a unix socket, the pattern
      `test_daemon_client.py` already uses for one-shot RPCs) receives
      real frames end-to-end, using ticket 003's looping fake camera
      backend as the frame source.
- [x] `uv run mypy` clean. Zero `typing.Protocol`.
- [x] Full existing suite (1099 tests, headless baseline) still
      passes.

## Implementation Plan

**Approach.** Mirror `archive/src/aprilcam/client/stream.py`'s
`ImageStreamConsumer`/`TagStreamConsumer` connection logic (unix
socket preferred when local via `_host_is_local`, TCP fallback,
`set_timeout` for a bounded wait on the first frame) — the connection
*mechanics* are earned from v1; the *decode* step is new, going
through `client/models.py`'s `from_proto` rather than v1's
`aprilcam_pb2`/hand-rolled Pydantic conversion. Reuse ticket 001's
shared framing-helper module for the read side rather than
reimplementing length-prefixed reads here.

**Files to create/modify:**
- `src/aprilcam/client/streams.py` (new)
- `src/aprilcam/client/daemon_client.py` (extend —
  `stream_images`/`stream_tags`)
- `src/aprilcam/client/models.py` (only if a new `from_proto` helper is
  needed beyond what `image_frame_from_proto`/`tag_frame_from_proto`
  already provide — check before adding; these likely already cover
  the per-frame decode, since streamed frames are the same
  `ImageFrame`/`TagFrame` proto messages as the one-shot RPCs use)

**Documentation updates:** `client/daemon_client.py`'s module docstring
currently states "Only these twenty-five methods are implemented" —
update the count/list now that `stream_images`/`stream_tags` are real,
following the module's own established pattern of naming which ticket
added each method.

## Testing

- **Existing tests to run**: `tests/aprilcam/client/
  test_daemon_client.py`, `tests/test_no_cv2_in_client_modules.py`,
  full suite via `uv run pytest`.
- **New tests to write**: `tests/aprilcam/client/test_streams.py` (or
  extend `test_daemon_client.py`) — full daemon-to-client round trip
  over a real local socket, decode correctness against known
  `ImageFrame`/`TagFrame` values, `.close()` behavior, and a bounded-
  wait timeout when no frame arrives (mirroring
  `set_timeout`'s v1 precedent).
- **Verification command**: `uv run pytest && uv run mypy`.
