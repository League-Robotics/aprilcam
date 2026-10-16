---
id: '006'
title: Proto, RPC handlers, and client wrappers for the four intrinsics commands
status: done
use-cases:
- SUC-001
- SUC-003
depends-on:
- '005'
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Proto, RPC handlers, and client wrappers for the four intrinsics commands

## Description

Expose the four commands over the wire: proto messages, `daemon/rpc.py` handlers, `GrpcDaemon` wrappers in `client/daemon_client.py`, and proto<->value converters in `client/models.py`, following the sprint 037 flat-field ticket (037-003/004) for shape and error mapping.

## Acceptance Criteria

- [x] `proto/aprilcam_v2.proto` defines StartIntrinsics, GetIntrinsicsStatus, FinishIntrinsics, AbortIntrinsics request/response messages and an `IntrinsicsStatus` message mirroring the value type (3x3 coverage as a flat repeated field, tilt counts, optionals for rms/residual/correction); generated `aprilcam_v2_pb2*.py` regenerated with `scripts/compile_proto.py`.
- [x] `daemon/rpc.py` handlers map service errors (unknown camera, session already active, no active session, camera unreadable) onto the existing typed-error scheme; `PROTOCOL_VERSION` handling checked and noted.
- [x] `GrpcDaemon.start_intrinsics/get_intrinsics_status/finish_intrinsics/abort_intrinsics` return the same value types the service returns; round trip preserves every status field including Nones.
- [x] Client modules stay cv2-free (`tests/test_no_cv2_in_client_modules.py` passes).

## Implementation Plan

Modify `proto/aprilcam_v2.proto`, regenerated files in `src/aprilcam/proto/`, `daemon/rpc.py`, `client/daemon_client.py`, `client/models.py`. Update `src/aprilcam/proto/DESIGN.md` message table only if one exists there.

## Testing

Extend the existing RPC round-trip tests (look at how flat-field RPCs are tested under `tests/aprilcam/daemon` and `tests/aprilcam/client`): in-process gRPC server against a fake `DaemonService` returning canned statuses; error mapping; Nones preserved. Run `uv run pytest tests/aprilcam/daemon tests/aprilcam/client tests/test_no_cv2_in_client_modules.py`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)

## Implementation Notes

- `PROTOCOL_VERSION` checked: stays `1`. The change is purely additive (new messages and RPCs; no existing field renumbered or removed), which `daemon/rpc.py`'s own rule says needs no bump.
- Error mapping: `IntrinsicsSessionActive` -> ALREADY_EXISTS, `NoIntrinsicsSession` -> NOT_FOUND (both carry `aprilcam-camera` metadata and are reconstructed client-side), `IntrinsicsError` base -> FAILED_PRECONDITION, unknown camera -> NOT_FOUND and unreadable camera (`CameraNotPresent`) -> FAILED_PRECONDITION via existing mappings.
- Lens summary dict crosses the wire as an optional JSON string (`lens_json`) since it is free-form.
- Coverage dicts flatten to fixed-order repeated ints (empty list <-> empty dict). `src/aprilcam/proto/DESIGN.md` has no message table, so it is unchanged.
- Tests: `tests/aprilcam/client/test_daemon_client_intrinsics.py`.
