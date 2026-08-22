---
id: '001'
title: Protocol version handshake for v1/v2 daemon collision detection
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: sprint-020-followups-socket-collision-and-cli-polish.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Protocol version handshake for v1/v2 daemon collision detection

## Description

A running v1 daemon (from a sibling checkout) and a running v2 daemon
share the same conventional control-plane socket path. A v2 client that
happens to reach the v1 daemon gets an uninterpretable `Method not
found!` / generic RPC failure rather than a clear diagnosis. This was
observed live in sprint 020's acceptance pass and independently
confirmed as the root cause of 18 of 19 `-m camera` test failures during
sprint 030 (see the "RESOLVED" section of
`camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`,
this ticket's second linked issue's counterpart investigation — the same
structural fix closes both).

Currently, `client/daemon_client.py`'s `_reconstruct_error` inspects a
`grpc.RpcError`'s trailing metadata for a `aprilcam-error-type` key (set
by `daemon/rpc.py`'s `_abort` contract on every typed v2 error). A v1
daemon's response carries none of that metadata — it isn't a v2 daemon —
so today it falls through to a generic `ConnectionFailed(target,
reason=exc.details() or str(exc.code()))`. That is indistinguishable
from "nothing is listening" or "network hiccup" to the person reading
the error.

Add a `protocol_version` field to `GetStatusResponse`
(`proto/aprilcam_v2.proto`) and teach the client to recognize the
specific failure shape of "this daemon doesn't understand v2's RPC set
at all" (`UNIMPLEMENTED`/no recognized metadata on the first RPC a
freshly-connected client makes) as a distinct, named error rather than
a generic connection failure.

## Acceptance Criteria

- [x] `proto/aprilcam_v2.proto`'s `GetStatusResponse` gains a
      `protocol_version` field (additive, no renumbering of existing
      fields); `aprilcam_v2_pb2.py`/`aprilcam_v2_pb2_grpc.py` are
      regenerated from it.
- [x] `daemon/rpc.py`'s `GetStatus` handler populates `protocol_version`
      with this codebase's own protocol version constant.
- [x] A new typed error (`errors.py`, following the existing
      `UnknownGuide`/`InvalidTagId` naming and metadata pattern — e.g.
      `DaemonVersionMismatch`) is raised by
      `client/daemon_client.py`'s `_reconstruct_error` when an RPC
      response carries no recognized `aprilcam-error-type` metadata and
      no valid `protocol_version` can be read (the `UNIMPLEMENTED` /
      unparseable-response shape a v1 daemon actually produces),
      instead of falling through to `ConnectionFailed`.
- [x] The new error's message names the socket/host target and states
      plainly that the daemon speaks an incompatible protocol — not a
      generic "connection failed."
- [x] A v2 client connecting to a compatible v2 daemon is unaffected: no
      new failure mode, no additional round trip beyond the existing
      first `status()`/`GetStatus` call.
- [x] `uv run mypy` clean.

## Implementation Plan

**Approach**: additive proto change first (so the regenerated stubs
exist before any code references `protocol_version`), then the
client-side detection logic, then the new error type. Keep the daemon
side minimal — populate one field on an existing response, no new RPC.
On the client side, do not attempt to distinguish "v1 daemon" from
"some other incompatible responder" beyond what the evidence actually
supports (no recognized metadata, no valid protocol version) — name the
failure by what was observed, not by a guess at which specific v1
process is on the other end.

**Files**:
- `proto/aprilcam_v2.proto` — add `protocol_version` to
  `GetStatusResponse`.
- `src/aprilcam/proto/aprilcam_v2_pb2.py`,
  `src/aprilcam/proto/aprilcam_v2_pb2_grpc.py` — regenerated (do not
  hand-edit).
- `src/aprilcam/daemon/rpc.py` — `_status_to_proto`/`GetStatus`
  populate the new field.
- `src/aprilcam/client/daemon_client.py` — `_reconstruct_error` (or a
  narrow wrapper around the first RPC a fresh connection makes) detects
  the incompatible-daemon shape and raises the new error.
- `src/aprilcam/errors.py` — new `DaemonVersionMismatch` (or similarly
  named) error class.

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/client/
  test_daemon_client.py tests/aprilcam/daemon/test_service.py -v`, plus
  `uv run pytest -m "not camera"` for the full headless regression pass.
- **New tests to write**: a fake gRPC stub/server that returns
  `UNIMPLEMENTED` (or an empty/garbled response) for `GetStatus`,
  asserting the client raises the new named error, not
  `ConnectionFailed`; a positive-path test that a compatible v2
  `GetStatus` response (with `protocol_version` set) is read normally
  with no behavior change; a round-trip test that `GetStatusResponse`
  proto serialization carries the new field correctly.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none required in `docs/design/` proper (the
subsystem `DESIGN.md` files this affects — `daemon/`, `client/`,
`proto/` — do not exist yet; ticket 008 documents this behavior when it
bootstraps them, after this ticket lands per the dependency ordering in
`sprint.md`'s Architecture section).
