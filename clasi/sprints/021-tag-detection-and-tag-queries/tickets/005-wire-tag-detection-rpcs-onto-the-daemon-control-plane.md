---
id: '005'
title: Wire tag-detection RPCs onto the daemon control plane
status: open
use-cases: [SUC-001, SUC-002, SUC-003]
depends-on: ['004']
github-issue: ''
issue: tag-detection-and-tag-queries.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire tag-detection RPCs onto the daemon control plane

## Description

Extends `proto/aprilcam_v2.proto` additively with `GetTags`/`GetTag`/
`GetTagHistory` RPCs and `TagId`/`TagRecord`/`TagFrame` messages
mirroring `types.py` field-for-field, then wires `daemon/rpc.py`
(server-side marshalling), `client/models.py` (wire→Python
`from_proto`), and `client/daemon_client.py` (client-side `GrpcDaemon`
methods) to match — the exact pattern sprint 020 already established
for `GetFrame`/`ListCameras`/`ProbeCameras`/`GetStatus`. No business
logic lives in any of these three modules; every method body here is
(de)serialization and error-code mapping only, delegating to
`DaemonService` (ticket 004).

Every field number in the existing proto messages
(`CameraState`, `GetFrameResponse`, etc.) is permanent — this ticket
only appends new fields and new messages, per the proto file's own
stated evolution rule.

## Acceptance Criteria

- [ ] `proto/aprilcam_v2.proto` gains: `GetTags(GetTagsRequest) ->
      GetTagsResponse`, `GetTag(GetTagRequest) -> GetTagResponse`,
      `GetTagHistory(GetTagHistoryRequest) -> GetTagHistoryResponse`;
      `TagId` (family enum or string + number), `TagRecord`
      (mirroring `types.TagRecord` field-for-field — `world`/
      `heading_rad`/`world_velocity`/`speed`/`yaw_rad`/`world_corners`
      as `optional`, `pixel`/`pixel_corners` populated,
      `calibrated` as a plain `bool`), `TagFrame` (`tags` repeated,
      `timestamp`, `frame_index`, `calibration_stale`).
- [ ] `GetFrameResponse` gains an `optional TagFrame tags = 9` field
      (next available field number — verify against the current file
      before assigning) to carry ticket 004's `ImageFrame.tags` embed;
      no existing field is renumbered.
- [ ] Regenerated via `scripts/compile_proto.py`; generated
      `aprilcam_v2_pb2.py`/`aprilcam_v2_pb2_grpc.py` committed per the
      existing convention (these files are checked in, not
      gitignored — confirm against current repo state).
- [ ] `daemon/rpc.py`'s `_Servicer` gains `GetTags`/`GetTag`/
      `GetTagHistory` methods, each: try/except around the
      `DaemonService` call, `_abort(context, exc)` on failure
      (reusing the existing `_STATUS_CODES` map — `UnknownCamera` and
      `CameraNotPresent`, ticket 002, already map correctly with zero
      changes here), marshal the successful result via new
      `_tag_record_to_proto`/`_tag_frame_to_proto` helper functions
      following the existing `_camera_state_to_proto` pattern
      (optional fields set only when not `None`, matching
      `HasField()` semantics).
- [ ] `client/models.py` gains `tag_record_from_proto`/
      `tag_frame_from_proto` — the reverse, field-for-field, following
      `camera_state_from_proto`'s existing pattern exactly (including
      the `HasField()` → `None` discipline for optional fields).
- [ ] `client/daemon_client.py`'s `GrpcDaemon` gains `get_tags`,
      `get_tag`, `get_tag_history` methods matching the `Daemon` base
      class signatures, each building the request message and calling
      through `self._call(...)` exactly like the existing four
      methods; `get_frame`'s response marshalling picks up the new
      `tags` field via `image_frame_from_proto`.
- [ ] RPC round-trip tests (real `grpc.Server` over a `tmp_path` unix
      socket, real generated stub — matching `test_service.py`'s
      `stub` fixture pattern) cover all three new RPCs plus the
      `GetFrame` tag-embed addition, including the not-detected
      (`get_tag` → `None`/empty response) and unknown-camera error
      paths across the wire.
- [ ] `uv run mypy` stays clean (note: `aprilcam.proto.*` is
      `follow_imports = "skip"` in `pyproject.toml`'s mypy config —
      generated code is exempt, but every hand-written module that
      touches it is not). `uv run pytest` stays green.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/
  tests/aprilcam/client/ -v`, full suite, `uv run mypy`.
- **New tests to write**: extend
  `tests/aprilcam/daemon/test_service.py`'s RPC-round-trip section
  (the `stub` fixture) with `test_rpc_get_tags_round_trip`,
  `test_rpc_get_tag_round_trip` (both found and not-found cases),
  `test_rpc_get_tag_history_round_trip` (including the explicit-gap
  case), `test_rpc_get_frame_embeds_tags_round_trip`. Add
  equivalent-shaped tests to `tests/aprilcam/client/
  test_daemon_client.py` if that file tests `GrpcDaemon` methods
  independently of the live-server round trip (check the existing
  pattern there before choosing where new tests live).
- **Verification command**:
  `uv run pytest tests/aprilcam/daemon/ tests/aprilcam/client/ -v &&
  uv run mypy`

## Implementation Plan

### Approach

1. Read the current `proto/aprilcam_v2.proto` in full before editing
   — confirm the next free field numbers on every message being
   extended, and follow the file's existing comment style (each RPC
   and message documents which use case/scenario it serves and any
   sprint-020-carried-forward notes that are now stale and should be
   removed, e.g. the "Deliberately absent this sprint... GetTags" note
   at the top of the file needs updating since this sprint adds it).
2. Edit the `.proto` file, then run `scripts/compile_proto.py` to
   regenerate — do not hand-edit the generated `_pb2` files.
3. `daemon/rpc.py`: add the three servicer methods plus
   `_tag_record_to_proto`/`_tag_frame_to_proto`, following
   `_camera_state_to_proto`/`_image_frame_to_proto`'s existing shape
   exactly (including the "optional fields: only set when not None"
   comment convention).
4. `client/models.py`: add the reverse converters, following
   `camera_state_from_proto`'s `HasField()` pattern.
5. `client/daemon_client.py`: add the three `GrpcDaemon` methods.
6. Update `GetFrameResponse`'s docstring comment in the `.proto` file
   — it currently states these fields "are NOT carried this sprint";
   that sentence becomes false for `tags` and should be corrected,
   not left stale.

### Files to create/modify

- `proto/aprilcam_v2.proto` (modify)
- `src/aprilcam/proto/aprilcam_v2_pb2.py`,
  `src/aprilcam/proto/aprilcam_v2_pb2_grpc.py` (regenerated, modify)
- `src/aprilcam/daemon/rpc.py` (modify)
- `src/aprilcam/client/models.py` (modify)
- `src/aprilcam/client/daemon_client.py` (modify)
- `tests/aprilcam/daemon/test_service.py` (modify)
- `tests/aprilcam/client/test_daemon_client.py` (modify, if applicable)

### Documentation updates

`proto/aprilcam_v2.proto`'s own header comment (the "deliberately
absent this sprint" list) needs correcting now that `GetTags` exists.
