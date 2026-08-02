---
id: '004'
title: ResetCameraConfig end to end
status: done
use-cases:
- SUC-003
depends-on:
- '001'
- '003'
github-issue: ''
issue: config-service-completion-and-annotations.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# ResetCameraConfig end to end

## Description

`Daemon.reset_camera_config` is declared but deliberately unimplemented
since sprint 022 (`daemon/interfaces.py`'s base raises
`NotImplementedError`; `DaemonService` does not override it). This is
how UC-01's experiment workflow ends without a daemon restart. Per
sprint.md Design Rationale Decision 3: reset always means "re-read
`config_dir/cameras/<slug>/config.json`" specifically — never "whatever
file was last pushed" (a scratch-file push, UC-23.S-2, has no natural
on-disk config of its own). A camera with a live override but nothing
installed in `config_dir` falls back to the introspected-plus-blank
template (ticket 002) rather than erroring.

Full stack, mirroring every other RPC in this codebase (e.g.
`Calibrate`): proto message + RPC, `daemon.rpc` servicer method,
`DaemonService.reset_camera_config`, `Camera.reset()` wired to a real
`ConfigLoader` read of the camera's on-disk path,
`client.daemon_client.GrpcDaemon.reset_camera_config`, and a CLI verb:
`aprilcam camera config <camera> --reset` (confirmed spelling,
consistent with the existing `--dump` flag — see sprint.md Decisions).

## Acceptance Criteria

- [x] `proto/aprilcam_v2.proto` gains `ResetCameraConfig
      (ResetCameraConfigRequest) returns (ResetCameraConfigResponse)`,
      regenerated.
- [x] `daemon/rpc.py`'s `_Servicer.ResetCameraConfig` delegates to
      `DaemonService.reset_camera_config`, with the same error-mapping
      discipline as every other RPC method in that file (typed
      `aprilcam.errors`, never a bare string).
- [x] `DaemonService.reset_camera_config(camera)`: resolves
      `config_dir/cameras/<slug>/config.json` if present, loads it via
      `ConfigLoader`, and calls `Camera.reset()`/`apply_config` to
      re-apply it to the held-open camera, discarding any live
      override; if no on-disk file exists, falls back to the blank
      template (ticket 002) rather than erroring.
- [x] `client/daemon_client.py`'s `GrpcDaemon.reset_camera_config`
      added, mirroring `set_camera_config`'s shape.
- [x] `aprilcam camera config <camera> --reset` CLI verb added to
      `cli/camera.py`.
- [x] UC-01's reset flow verified live on camera 3: push a live
      override different from what's installed on disk, `--reset`,
      hardware reflects the on-disk value with no daemon restart.
- [x] Reset on a camera with a live override but no installed
      `config_dir` file falls back to the blank template (headless
      test).
- [x] `uv run mypy` clean; baseline 629 passed / 1 skipped stays green;
      zero `typing.Protocol` — the new RPC's Python-side types are
      base classes / frozen kw_only dataclasses, matching every
      existing interface in this codebase.

## Implementation Plan

**Files to modify:**
- `proto/aprilcam_v2.proto` — new RPC + messages (mirror
  `SetCameraConfig`'s request/response shape; request just needs the
  camera token).
- `src/aprilcam/daemon/rpc.py` — new servicer method.
- `src/aprilcam/daemon/service.py` — `reset_camera_config` override.
- `src/aprilcam/camera/camera.py` — `reset()`: wire to a `ConfigLoader`
  instance and the resolved on-disk path (passed in, or resolved via
  an injected `Directories`).
- `src/aprilcam/client/daemon_client.py` — `GrpcDaemon.
  reset_camera_config`.
- `src/aprilcam/cli/camera.py` — `--reset` flag on the `config`
  subparser; `cmd_config` branches on it (mutually exclusive with the
  positional file, per `--dump`'s shape in ticket 005).

**Archive-reuse pointers:** none beyond the established in-codebase
RPC-addition pattern (proto → rpc.py → service.py → daemon_client.py →
cli), already used for every existing RPC (e.g. `Calibrate`).

## Testing

- **Existing tests to run**: `tests/aprilcam/daemon/test_service.py`,
  `tests/aprilcam/cli/test_camera_cli.py`, full `uv run pytest` for the
  629/1 baseline.
- **New tests to write**: headless — proto (de)serialization test for
  the new RPC, matching `tests/aprilcam/daemon/test_service.py`'s
  existing per-RPC pattern; `reset_camera_config` against a `tmp_path`
  config_dir (file present / file absent branches). `@pytest.mark.camera`
  — live reset on camera 3 after a live override.
- **Verification command**: `uv run pytest`; `uv run pytest -m camera`;
  `uv run mypy`.
