---
id: '001'
title: 'Config plumbing: playfields_dir + per-camera config.json helpers'
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-005
depends-on: []
github-issue: ''
issue: plan-named-persistent-playfields-as-the-single-source-of-truth.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Config plumbing: playfields_dir + per-camera config.json helpers

## Description

Foundation ticket. Introduces the two lowest-level building blocks that all
higher tickets depend on:

1. `Config.playfields_dir` property on `src/aprilcam/config.py` — returns
   `self.data_dir / "playfields"`. No env var needed.

2. New module `src/aprilcam/camera/camera_config.py` with two functions:
   - `load_camera_config(camera_dir: Path) -> dict | None` — reads
     `<camera_dir>/config.json` and returns the parsed dict, or `None` if the
     file is absent or malformed.
   - `save_camera_config(camera_dir: Path, config_dict: dict) -> Path` — writes
     `config.json` atomically via `.tmp` + `os.replace`. Returns the path written.

The daemon (`camera_pipeline.py`, `grpc_server.py`) must never import this module.
The module itself has no imports from the daemon layer.

## Acceptance Criteria

- [x] `Config.load().playfields_dir` returns `<data_dir>/playfields` as an
      absolute `Path`. Confirmed by adding a one-line assertion to an existing
      config test or by reading the property in a test.
- [x] `load_camera_config(camera_dir)` returns `None` when `config.json` is
      absent; returns the parsed dict when it exists.
- [x] `save_camera_config(camera_dir, {"playfield": "main-playfield"})` creates
      `config.json` atomically; a subsequent `load_camera_config` round-trips the
      same dict.
- [x] `save_camera_config` creates `camera_dir` if it does not exist.
- [x] Neither function is imported by `daemon/camera_pipeline.py` or
      `daemon/grpc_server.py` (confirm by grep).

## Implementation Plan

### Approach

Pure additions; no existing code changed beyond `config.py`.

### Files to create

- `src/aprilcam/camera/camera_config.py`
  - `load_camera_config(camera_dir: Path | str) -> dict | None`
  - `save_camera_config(camera_dir: Path | str, config_dict: dict) -> Path`
  - Use `json.loads` / `json.dumps`; atomic write pattern from `paths.py`:
    write to `.tmp` then `os.replace`.

### Files to modify

- `src/aprilcam/config.py`
  - On `Config` dataclass, add:
    ```python
    @property
    def playfields_dir(self) -> Path:
        return self.data_dir / "playfields"
    ```

### Testing plan

- Add `tests/test_camera_config.py` (new file):
  - `test_load_missing()` — missing file returns None.
  - `test_round_trip(tmp_path)` — save then load returns same dict.
  - `test_atomic_write(tmp_path)` — no `.tmp` file left after save.
  - `test_creates_dir(tmp_path)` — dir created if absent.
- Add one assertion to existing config tests or a new
  `test_config_playfields_dir(tmp_path)`.

### Documentation updates

None required for this ticket.
