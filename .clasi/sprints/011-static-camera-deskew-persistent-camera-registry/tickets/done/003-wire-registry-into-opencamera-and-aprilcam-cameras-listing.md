---
id: '003'
title: Wire registry into OpenCamera and aprilcam cameras listing
status: done
use-cases:
- SUC-006
- SUC-007
depends-on:
- '002'
github-issue: ''
issue: persistent-camera-registry-with-stable-identity-and-enumeration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire registry into OpenCamera and aprilcam cameras listing

## Description

Wire the registry (tickets 001–002) into the two consumers so the behavior is
visible end-to-end. This ticket completes the persistent-camera-registry issue.

1. **`daemon/grpc_server.py` `OpenCamera`.** Replace
   `cam_name = device_name_slug(get_device_name(index))` (with the `cam-<index>`
   fallback) with a registry resolution: resolve the OpenCV `index` → a registry
   record (creating one on first sight, reusing on reconnect), and use the
   record's dir key as `cam_name`. The proto `OpenCameraResponse` is unchanged
   (`cam_name`, `camera_dir`); `camera_dir` is still
   `cameras_dir / cam_name`. Reconnect of a known camera must resolve to the
   same record/dir with no restart. Keep the `calibration.json` `detection_fps`
   lookup intact against the resolved dir.
2. **`cli/cameras_cli.py`.** List **all** registry records, not just live
   devices. Print each camera's enumeration number; connected cameras show
   their current OS index, disconnected ones are rendered grayed-out (via
   `rich`, already a base dep) and marked offline. The pattern-selector
   continues to operate on connected cameras only.

## Acceptance Criteria

- [x] `OpenCamera` resolves the camera through the registry; reconnect of a
  known camera (same `unique_id`) reuses its `cam_name`/dir with no daemon
  restart and no manual reindex.
- [x] A genuinely new camera receives a new monotonic enumeration number and a
  fresh dir; existing cameras are unaffected.
- [x] `aprilcam cameras` lists connected and previously-seen-disconnected
  cameras; disconnected ones are grayed out and marked offline; all retain
  their enumeration numbers.
- [x] Connected cameras display their current OS index in the listing.
  (Superseded by stakeholder follow-up below: the OS index is now shown only
  under `--details`; the default listing prints exactly ONE number — the
  stable enumeration number.)
- [x] The proto/wire contract is unchanged; existing clients still work.
- [x] `uv run pytest` passes.

## Stakeholder follow-up (one user-facing number)

`aprilcam cameras` previously printed TWO numbers per line (`#<enum> [<os>]`),
which was confusing. The enumeration number is now the single user-facing
camera selector.

- [x] New resolver `resolve_enum_to_index(enum_no, registry, live_identities)`
  in `camera/registry.py` (with `CameraSelectError`): maps an enumeration
  number → live OS index via the record's `unique_id` and
  `identity.resolve_all()`. Clear errors for unknown (`no camera #N`) and
  disconnected (`camera #N (...) is not connected`) enum numbers.
- [x] `aprilcam view CAMERA`: an integer CAMERA is the enumeration number,
  resolved to the live OS index before `open_camera`. Name selection still
  works. Arg help updated.
- [x] `aprilcam calibrate`: numeric specs are enumeration numbers, resolved
  to live OS indices. The old volatile-OS-index warning is removed.
- [x] `aprilcam cameras` prints ONE number (the enumeration number); the OS
  index appears only under `--details`. Offline records still show their
  number, dimmed.
- [x] Low-level gRPC `OpenCamera(index)` left as-is (OS index for agents).
- [x] Tests added/updated: resolver tests in
  `tests/unit/test_camera_registry.py`; one-number listing + `--details` in
  `tests/test_cameras_cli.py`; view/calibrate enum-number selection in
  `tests/test_camera_enum_selection.py`. `uv run pytest` passes.

## Implementation Plan

- **Approach**: Inject/construct a `CameraRegistry` at the daemon servicer and
  in the CLI, both reading from `config.cameras_dir`. Thread resolution through
  `OpenCamera`; render the merged registry+live view in the CLI.
- **Files to modify**: `src/aprilcam/daemon/grpc_server.py`,
  `src/aprilcam/cli/cameras_cli.py`.
- **Testing plan**: see Testing; mock the registry/live device list for CLI
  rendering, and the resolver for the gRPC handler.
- **Docs**: update `aprilcam cameras` `--help` to mention offline cameras and
  the location-path identity limitation.

## Testing

- **Existing tests to run**: `uv run pytest tests -k "cameras or grpc or open"`,
  then full `uv run pytest`.
- **New tests to write**:
  - `OpenCamera` resolves a mocked index to a registry record; a second
    resolve of the same `unique_id` returns the same `cam_name`/dir (no new
    pipeline key).
  - CLI rendering: registry with one connected + one disconnected camera prints
    both with numbers; the disconnected one is marked offline/grayed; the
    connected one shows its OS index.
  - regression: `OpenCameraResponse` still carries `cam_name` + `camera_dir`.
  - Mark daemon-importing tests `needs_daemon`.
- **Verification command**: `uv run pytest`
