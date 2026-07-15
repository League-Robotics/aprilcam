---
id: '003'
title: Thread config_dir through daemon-local callers and repoint config-touching
  RPCs
status: done
use-cases:
- SUC-005
depends-on:
- '002'
github-issue: ''
issue: split-config-from-state-into-fhs-xdg-roots.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Thread config_dir through daemon-local callers and repoint config-touching RPCs

## Description

Ticket 002 made `load_calibration_from_camera_dir` and
`_load_static_marker_ids` accept an optional config-root parameter without
changing default behavior. This ticket wires real values through from
`Config` at every daemon-local call site, and repoints the RPCs that touch
`config.json`/`playfields/*.json` to the config root while leaving the
RPCs that touch state-category files exactly where they are.

**Daemon-local loader callers** (add
`config_dir=self.config.effective_config_dir` — or the local equivalent —
to each existing call):
- `daemon/camera_pipeline.py` (~272-273):
  `camera_dir = self.config.cameras_dir / self.cam_name` stays state-only;
  add the `config_dir` kwarg to the `load_calibration_from_camera_dir`
  call.
- `daemon/grpc_server.py` `ReloadCalibration` (~297) and the
  post-`SetCalibration` reload site (~878): same one-kwarg addition using
  `self._config.effective_config_dir`.
- `stream.py` (~69): same addition using `cfg.effective_config_dir`.
- `cli/view_cli.py` (~557): same addition using
  `config.effective_config_dir`.
- Any `_load_static_marker_ids` call site inside `core/playfield.py` (617,
  825) that wasn't already wired in ticket 002 gets the actual
  `config_dir` value threaded from the `Playfield`'s own config
  reference (however the class currently receives its `Config`/dir
  values — follow the existing pattern for how `data_dir` reaches this
  class today).

**RPC resolution** (`daemon/grpc_server.py`):
- `GetCameraConfig` (737) / `SetCameraConfig` (761): resolve
  `config_cameras_dir/<cam_name>/config.json` instead of
  `cameras_dir/<cam_name>/config.json`. This is a genuine behavior change
  for these two RPCs — verify no other RPC or daemon-internal caller reads
  `config.json` via `cameras_dir` after this change (grep to confirm).
- `GetCalibration` (791) / `SetCalibration` (816) / `GetPaths` (900) /
  `SetPaths` (923): **unchanged** — verify they still resolve against
  `cameras_dir` (state) after this ticket; do not touch these handlers
  beyond adding the `config_dir` kwarg to the calibration-reload call
  inside `SetCalibration`'s post-write reload (~878, listed above).
- `ListPlayfields` (969): scans `config_playfields_dir` instead of
  `data_dir / "playfields"`.
- Camera registry (`registry.json`, via `camera/registry.py`) is
  **out of scope** — stays on `cameras_dir` (state); do not touch.

**`core/playfield_def.py`**: `load_all(playfields_dir)`'s signature is
unchanged; its daemon-startup caller (the site instantiating/reloading the
playfield-definition registry) passes `cfg.config_playfields_dir` instead
of `cfg.playfields_dir`.

See `../architecture-update.md` §4 ("Threading the config root through
daemon-local callers + RPC resolution") for full design detail and the
verified line numbers this ticket touches.

## Acceptance Criteria

- [x] `camera_pipeline.py`, both `grpc_server.py` reload sites,
      `stream.py`, and `view_cli.py` each pass `config_dir` to their
      `load_calibration_from_camera_dir` call, resolved from their local
      `Config`/`config` instance's `effective_config_dir`.
- [x] `_load_static_marker_ids` call sites in `core/playfield.py` (617,
      825) pass a real config-dir value (not `None`) once the class has
      access to it.
- [x] `SetCameraConfig` writes land under `config_cameras_dir`, never
      under `cameras_dir`/`data_dir`.
- [x] `GetCameraConfig` reads resolve against `config_cameras_dir`.
- [x] `SetCalibration`/`GetCalibration`/`SetPaths`/`GetPaths` are
      unchanged — writes/reads still resolve against `cameras_dir` (state)
      exactly as before this ticket.
- [x] `ListPlayfields` returns definitions found under
      `config_playfields_dir`.
- [x] `core/playfield_def.py`'s `load_all()` daemon-startup caller passes
      `config_playfields_dir`.
- [x] `camera/registry.py` (`registry.json`) is untouched — confirmed by a
      grep/test that no new reference to `config_dir` was introduced there.
- [x] A camera with `config.json` under the config root and
      `calibration.json` under the state root (fully split layout, no
      legacy fallback needed) opens correctly end-to-end: daemon starts,
      `GetCameraConfig` returns the config-root content, `GetCalibration`
      returns the state-root content, detection loop starts and
      `get_tags`-equivalent (`test_file_proxy_rpcs.py` or integration
      test) returns world coordinates.
- [x] Full `uv run pytest` green.

## Testing

- **Existing tests to run**: `tests/test_file_proxy_rpcs.py`,
  `tests/test_open_camera_rehydrate.py`,
  `tests/test_calibration_geometry_persist.py`, any daemon/grpc_server
  integration tests, full `uv run pytest`.
- **New tests to write**:
  - Per-caller unit tests (or integration test doubles) confirming each of
    the five daemon-local call sites passes a non-`None` `config_dir`
    resolved from `effective_config_dir`.
  - `SetCameraConfig`/`GetCameraConfig` round-trip test against a tmp
    `config_dir` distinct from `data_dir` — write via RPC, confirm file
    lands under `config_cameras_dir`, read back via RPC, confirm content
    matches.
  - `SetCalibration`/`GetCalibration` and `SetPaths`/`GetPaths` round-trip
    tests confirming files land under `cameras_dir` (state) even when
    `config_dir` is a distinct tmp dir — proves no accidental cross-root
    leakage.
  - `ListPlayfields` test with playfield definitions under a tmp
    `config_playfields_dir` distinct from `data_dir/playfields` — confirms
    they're found; a definition placed only under the legacy
    `data_dir/playfields` location is still found via the ticket 001
    legacy-fallback path (`effective_config_dir`) until migrated.
  - End-to-end split-root test: config root and state root as two
    different tmp dirs, open camera, verify calibration + config both
    resolve, `get_tags`-equivalent returns world coordinates.
- **Verification command**: `uv run pytest`
