---
id: '002'
title: Wire find_feature into daemon, RPC, and CLI
status: done
use-cases:
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: complete-the-daemon-query-surface.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire find_feature into daemon, RPC, and CLI

## Description

Wire ticket 001's `FeatureQuery` into `Daemon.find_feature`
(`daemon/service.py`), add the `FindFeature` RPC (`proto/
aprilcam_v2.proto`, `daemon/rpc.py` dispatch, `client/daemon_client.py`
's `GrpcDaemon` stub), and the CLI surface `aprilcam playfield where
<query>` (`cli/playfield.py`, the issue's own named spelling).

**`DaemonService.find_feature(playfield, query)`**: resolve the named
playfield's current `PlayfieldDefinition` (reuse whatever existing
playfield-lookup path `get_playfield`/`self._playfields` already
uses — no new playfield storage), construct a `query.FeatureQuery()`
over `definition.features()`, and return `.find(query)`. Unknown
playfield -> reuse `errors.UnknownPlayfield` (already raised by
`get_playfield`), naming the playfields held — the same "reuse the
existing error, don't invent a new one" discipline sprint 024 ticket
001 followed for `UnknownPlayfield` across five new methods.

**Proto**: add `rpc FindFeature (FindFeatureRequest) returns
(FindFeatureResponse)`, with `FindFeatureRequest{string playfield = 1;
string query = 2;}` and `FindFeatureResponse{repeated FeatureMatch
features = 1;}`. Add a new `FeatureMatch` message
(`slug`, `kind`, `WorldPoint position`, `optional string colour`) —
`PlayfieldFeatureEntry` (the existing message) is the wrong shape to
reuse here: it carries `tag_id`, not `kind`, and `config.interfaces.
Feature`'s wire shape needs `kind` as a string. Regenerate via
`scripts/compile_proto.py` — never hand-edit `_pb2.py`/`_pb2_grpc.py`.

**`daemon/rpc.py`**: one new `_Servicer.FindFeature` method, following
the existing try/except/`_abort` pattern exactly — marshalling only,
no business logic (this module's own docstring).

**`client/daemon_client.py`**: one new `GrpcDaemon.find_feature`
method, proto request/response conversion only, mirroring every
existing method in this class.

**CLI**: `aprilcam playfield where <query> [-d HOST] [--all]` in
`cli/playfield.py`. Read-only, so it fans out like `playfield list`/
bare `playfield annotations` (`cli.md`'s fan-out table: nothing that
changes state or streams interactively fans out; a lookup does).
Prints each matched feature's slug, kind, colour (if any), and world
position; prints an explicit `(no matches)` on an empty result,
matching this file's established `(no playfields)`/`(no annotations)`
convention rather than silent empty output.

## Acceptance Criteria

- [x] `DaemonService.find_feature` implemented, delegating to ticket
      001's `FeatureQuery`; raises `UnknownPlayfield` (naming the
      playfields held) for an unknown playfield name.
- [x] `FindFeature` RPC round-trips correctly through a real gRPC
      channel — a fixture-backed test proves the same UC-08.S-1/S-2/
      S-3 scenarios ticket 001 tested at the pure-logic layer now work
      end-to-end through the wire.
- [x] `aprilcam playfield where "the orange dot"` (against a fixture/
      fake daemon in tests, and manually against the live daemon
      holding `main-playfield`) returns all four orange dots.
- [x] `aprilcam playfield where` opens no camera and requires no
      calibration — verified by a test asserting no `Camera`/capture
      backend is touched during the call.
- [x] CLI prints an explicit `(no matches)` for a query that matches
      nothing, never silent empty output.
- [x] `uv run mypy` clean.
- [x] Full headless suite green (baseline 795 headless tests), plus
      this ticket's new tests.

## Implementation Plan

**Approach**: proto first (the wire contract), then
`DaemonService.find_feature` (pure Python, testable by constructing
`DaemonService` directly, per `tests/aprilcam/daemon/test_service.py`'s
existing pattern for `get_playfield`), then `daemon/rpc.py`'s dispatch
+ `client/daemon_client.py`'s stub (thin marshalling, mirrors
`GetPlayfield`'s existing shape), then the CLI last.

**Files**: `proto/aprilcam_v2.proto` (extend), regenerated
`aprilcam_v2_pb2.py`/`_grpc.py`, `src/aprilcam/daemon/service.py`
(extend), `src/aprilcam/daemon/rpc.py` (extend),
`src/aprilcam/client/daemon_client.py` (extend),
`src/aprilcam/cli/playfield.py` (extend: new `cmd_where` + subparser).

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/daemon/test_service.py
  tests/aprilcam/client/test_daemon_client.py
  tests/aprilcam/cli/test_playfield_cli.py -v`, plus the full headless
  suite.
- **New tests to write**: `DaemonService.find_feature` unit tests
  (unknown playfield, delegated matching); a gRPC round-trip test
  (real channel, mirroring sprint 024 ticket 001's `GetAnnotations`-
  style pattern); CLI argv-wiring tests for `playfield where` (mocked
  `Discovery`/`Daemon`, mirroring `test_playfield_cli.py`'s existing
  pattern for `cmd_config`/`cmd_annotations`).
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none beyond docstrings — design already
recorded in `sprint.md` and UC-08.
