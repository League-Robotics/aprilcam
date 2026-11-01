---
id: '004'
title: HOST-N in CLI camera subcommands and HOST-N row addresses
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '002'
- '003'
github-issue: ''
issue: host-qualified-camera-names-donald-4.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# HOST-N in CLI camera subcommands and HOST-N row addresses

## Description

Wire the resolver (003) into every CLI command that takes a camera, and show
each camera's copyable `HOST-N` address in `--all` listings.

## Implementation Plan

- Add one helper in `cli/_common.py` (e.g. `connect_for_camera(discovery,
  args) -> (Daemon, camera)`) that calls `client.addressing`, maps
  `UnknownHost` / `ConflictingTarget` through the existing `report_error`
  path, and replaces `discovery.connect(args.daemon)` in: `camera image`,
  `tags` (incl. `--follow`), `view`, `close` (`cli/camera.py`), `calibrate`,
  `flatfield`, `locate`, `intrinsics`. `camera config` takes a file argument,
  so it is unchanged unless it accepts a camera name (check; resolve if so).
- `--all` variants (`image --all`, `tags --all`) keep per-daemon fan-out;
  HOST-N is not resolved there (the camera argument applies to every daemon).
- `intrinsics` takes several cameras: all must resolve to the same daemon,
  else `ConflictingTarget` (see sprint.md open question 3).
- `camera list --all`: each row also shows `HOST-N` (daemon name from the fan-out
  label, N = `CameraState.number`; omit for rows with no number). Single-daemon
  `camera list` is unchanged apart from ticket 002.
- README: new "Camera addressing" section (slug, number, `HOST-N`, resolution
  order, collision rule, `-d` conflict). Update the `-d` examples.
- `src/aprilcam/cli/DESIGN.md` and `docs/design/use-cases/cli.md`: fan-out
  label text and HOST-N (mirror the sprint design overlay edits).

## Acceptance Criteria

- [x] `aprilcam camera image donald-4 -o f.jpg` targets Donald's camera 4 with no `-d`
- [x] Same for `tags`, `view`, `close`, `calibrate`, `flatfield`, `locate`, `intrinsics`
- [x] Slug and number arguments behave exactly as before
- [x] Conflicting `-d` and unknown host print clean errors, nonzero exit, no traceback
- [x] `camera list --all` rows show `HOST-N`
- [x] README and CLI design docs updated

## Testing

- **Existing tests to run**: `tests/aprilcam/cli`
- **New tests to write**: per-subcommand tests with fake discovery asserting which daemon is connected and which camera string reaches the RPC; error-path tests; `--all` row address test
- **Verification command**: `uv run pytest tests/aprilcam/cli`
