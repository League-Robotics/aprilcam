---
id: '003'
title: Daemon boot-time config load from disk
status: done
use-cases:
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: config-service-completion-and-annotations.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon boot-time config load from disk

## Description

The daemon never reads `config_dir` at startup — `daemon/__main__.py`'s
`build()` constructs `DaemonService` with empty `_camera_configs`/
`_playfields` dicts, populated only by RPC pushes. This is the restart
trap sprint 022 hit live (UC-01.S-4). Fix: `config.loader` gains a
directory-scan function; `daemon/__main__.py build()` calls it and
feeds every successfully-loaded document into `DaemonService` via its
**already-existing** `set_playfield`/`set_camera_config` methods — no
new `DaemonService` method for the loading step itself (sprint.md
Architecture, "Modules" point 1 and Design Rationale Decision 1: this
split is deliberate — `DaemonService` stays ignorant of disk,
`daemon.__main__` — the existing assembly module — owns "where
documents come from").

A malformed individual file must not prevent the daemon from starting
or block loading the others — matches the existing precedent in
`DaemonService._load_calibrations` ("a corrupt file must never crash the
daemon"). Log each skip at WARNING.

For a camera whose on-disk config names a playfield, and whose hardware
is physically present: when that camera is later opened
(`DaemonService._camera_for`), apply the loaded config's settings via
ticket 001's `Camera.apply_config` — this is what makes `aprilcam
playfield image` work immediately after a fresh start with no manual
push (UC-01.S-4's full round trip, including the previously-broken
playfield-link half).

## Acceptance Criteria

- [x] `config.loader` has a directory-scan function reading
      `config_dir/playfields/*.json` then
      `config_dir/cameras/*/config.json`, returning successes plus
      named per-file errors (never raising for one bad file).
- [x] `daemon/__main__.py build()` calls the scan, logs each error, and
      calls `service.set_playfield(...)`/`service.set_camera_config(...)`
      for every successfully-loaded document.
- [x] A malformed file among otherwise-valid ones does not prevent the
      daemon from starting, and every other file still loads (headless
      test with a `tmp_path` config tree containing one bad JSON file
      and several good ones).
- [x] UC-01.S-4 verified live: install a playfield + a camera config
      (naming camera 3) in a real `config_dir`, start the daemon fresh,
      run `aprilcam playfield image` immediately — no manual push, it
      works.
- [x] `DaemonService._camera_for` applies a previously-loaded (boot or
      pushed) `CameraConfig`'s settings via `Camera.apply_config` when
      opening the camera for the first time.
- [x] `uv run mypy` clean; baseline 629 passed / 1 skipped stays green;
      zero `typing.Protocol`.

## Implementation Plan

**Files to modify:**
- `src/aprilcam/config/loader.py` — add the directory-scan function
  (e.g. `load_config_dir(directories) -> ScanResult` with successes +
  errors); reuses `load_camera_config`/`load_playfield` per file.
- `src/aprilcam/daemon/__main__.py` — `build()`: call the scan after
  constructing `service`, before returning the `DaemonLifecycle`; log
  errors.
- `src/aprilcam/daemon/service.py` — `_camera_for`: after `cam.open()`,
  if `self._camera_configs` holds a config for `record.slug`, call
  `cam.apply_config(config)`.

**Archive-reuse pointers:** none — this is new v2-only wiring; v1's
directory resolution (`config.py`'s FHS/XDG cascade) is a different,
superseded mechanism per `docs/design/configuration.md`.

## Testing

- **Existing tests to run**: `tests/aprilcam/config/test_loader.py`,
  `tests/aprilcam/daemon/test_lifecycle_integration.py`, full `uv run
  pytest` for the 629/1 baseline.
- **New tests to write**: headless — `config.loader` scan against a
  `tmp_path` tree (valid playfield + valid camera config + one
  malformed file) asserts successes returned and the bad file named in
  errors, scan does not raise; `daemon/__main__.py build()` against a
  `tmp_path` `Directories` with a populated config tree asserts
  `DaemonService.get_playfield`/`get_camera_config` return the loaded
  documents with no RPC push. `@pytest.mark.camera` — UC-01.S-4 end to
  end against camera 3.
- **Verification command**: `uv run pytest`; `uv run pytest -m camera`;
  `uv run mypy`.
