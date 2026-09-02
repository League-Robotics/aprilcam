---
id: '003'
title: Proto messages, CaptureFlatField/GetFlatField/ClearFlatField RPCs
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '002'
github-issue: ''
issue: flat-field-gain-map-capture-as-a-daemon-workflow.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Proto messages, CaptureFlatField/GetFlatField/ClearFlatField RPCs

## Description

Put ticket 002's daemon capability on the wire. Add `CaptureFlatField`,
`GetFlatField`, `ClearFlatField` RPCs and their request/response
messages to `proto/aprilcam_v2.proto`, mirroring `Calibrate`'s shape
exactly: `CaptureFlatFieldRequest` carries `camera` plus optional
`exposure`/`frames` (int32) and `force`/`dry_run` (bool, defaulted
false, no presence-check needed); the response wraps a `FlatFieldResult`
submessage (`succeeded`, `spread_before`/`spread_after` — repeated
double, one per channel — `reason` (`HasField`-guarded), `forced_checks`
— repeated string). `GetFlatFieldResponse` wraps an optional
`FlatFieldInfo` submessage (`present`, `captured_at`, `shape` — repeated
int32 — `gain_min`/`gain_max`, `stale`), `HasField`-guarded absent when
never captured, mirroring `GetCalibrationResponse.calibration`'s
presence discipline. Extend `CameraState` with `flatfield_present`/
`flatfield_stale` (plain bool, not `HasField`-guarded — same as
`calibrated`/`calibration_stale`). Regenerate via
`scripts/compile_proto.py`.

Implement the server side in `daemon/rpc.py`'s `_Servicer`:
`CaptureFlatField`/`GetFlatField`/`ClearFlatField` handlers, each a thin
try/except delegating to the matching `DaemonService` method and mapped
through `_abort` on error — the identical shape every existing RPC
method in this file already follows (see `Calibrate`/`GetCalibration`
immediately above where these are added). Add the new
`_flatfield_result_to_proto`/`_flatfield_info_to_proto` converters next
to the existing `_calibration_result_to_proto`/`_calibration_to_proto`.
Map `errors.NoFieldMask` to `grpc.StatusCode.FAILED_PRECONDITION` in
`_STATUS_CODES`, and add its trailing-metadata entry to `_abort`'s
metadata table (naming whatever fields `NoFieldMask` carries, mirroring
`NoPlayfieldLinked`'s `aprilcam-camera` key), including the extra
metadata-key documentation `_abort`'s own docstring already itemises per
exception type.

## Acceptance Criteria

- [x] `proto/aprilcam_v2.proto` gains the three RPCs and their
      request/response messages, plus `CameraState.flatfield_present`/
      `flatfield_stale`, following this file's existing
      optional-scalar/submessage presence conventions.
- [x] `scripts/compile_proto.py` regenerates `aprilcam_v2_pb2.py`/
      `aprilcam_v2_pb2_grpc.py` cleanly with no manual edits to either
      generated file.
- [x] `daemon/rpc.py`'s `_Servicer` implements all three new RPC
      methods, each delegating to the matching `DaemonService` method
      inside the same try/except-mapped-through-`_abort` shape every
      existing RPC method uses.
- [x] `NoFieldMask` is added to `_STATUS_CODES` (`FAILED_PRECONDITION`)
      and `_abort`'s metadata table, with its docstring entry alongside
      the other typed exceptions.
- [x] Fake-backend RPC round-trip tests exercise `CaptureFlatField`/
      `GetFlatField`/`ClearFlatField` end-to-end through a real
      in-process gRPC server/servicer pair, covering success, a
      quality-gate refusal, `force=True`, `dry_run=True`, and
      `NoFieldMask` (client reconstructs the typed exception from
      trailing metadata).
- [x] `tests/test_no_cv2_in_client_modules.py` continues to pass (the
      generated proto module itself carries no `cv2` import; this
      ticket touches only `proto/`/`daemon/rpc.py`, both daemon-side).

## Testing

- **Existing tests to run**: the existing daemon RPC round-trip test
  module (mirrors the `Calibrate`/annotation RPC round-trip tests
  already in `tests/aprilcam/daemon/`) — no regressions.
- **New tests to write**: RPC round-trip tests for the three new RPCs
  per the acceptance criteria above.
- **Verification command**: `uv run pytest tests/aprilcam/daemon/`;
  regenerate and diff-check the proto output via `python
  scripts/compile_proto.py` (no unexpected diff beyond this ticket's
  additions).
