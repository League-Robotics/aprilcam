---
id: '001'
title: Extend config.py with Config dataclass and multi-source loader
status: done
use-cases:
- SUC-007
depends-on: []
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Extend config.py with Config dataclass and multi-source loader

## Description

The current `src/aprilcam/config.py` has an `AppConfig` class that only reads
from a `.env` file discovered by walking up from cwd. The daemon and all clients
need a richer, priority-ordered config chain:

1. `~/.aprilcam` (user-global dotfile)
2. `./.aprilcam` walking up from cwd (project-local; first match wins)
3. `./.env` (via existing python-dotenv)
4. Process environment variables (highest priority)

Add a new `Config` dataclass alongside the existing `AppConfig` (do not modify
or remove `AppConfig`). All daemon and client entry points will call
`Config.load()` at startup.

## Acceptance Criteria

- [x] `Config` dataclass defined in `src/aprilcam/config.py` with fields:
  - `data_dir: Path` (from `APRILCAM_DATA_DIR`, default `./data/runtime/`)
  - `socket_dir: Path` (from `APRILCAM_SOCKET_DIR`, default `/tmp/aprilcam/`)
  - `calibration_source: Path` (from `APRILCAM_CALIBRATION_SOURCE`, default `./data/calibration.json`)
  - `calibration_save_path: Path` (from `APRILCAM_CALIBRATION_SAVE_PATH`, default same as `calibration_source`)
  - `log_level: str` (from `APRILCAM_LOG_LEVEL`, default `"INFO"`)
  - `daemon_pidfile: Path` (from `APRILCAM_DAEMON_PIDFILE`, default `<socket_dir>/aprilcamd.pid`)
- [x] `Config.load(start: Optional[Path] = None) -> Config` classmethod implements
  the four-source priority chain (env wins over all files; project dotfile wins
  over user dotfile).
- [x] `_find_dotfile(name, start) -> Optional[Path]` helper walks up from `start`
  looking for a file named `name`; returns the first match or `None`.
- [x] Existing `AppConfig` class is completely unchanged; all existing callers
  continue to work.
- [x] `Config.load()` does not raise if no config files exist (uses defaults).
- [x] `socket_dir` is created by `Config.load()` if it does not exist (daemon
  needs it writable before binding sockets).

## Implementation Plan

### Approach

Add the new `Config` dataclass and `Config.load()` classmethod to the bottom of
`src/aprilcam/config.py` after all existing `AppConfig` code. Keep the existing
imports; add `import os` if not already present.

### Files to Create

None.

### Files to Modify

- `src/aprilcam/config.py` — add `Config` dataclass and `Config.load()` after
  existing `AppConfig`. Add `_find_dotfile()` as a module-level helper. Add
  `import os` if not present (needed for `os.environ`).

### Config Loading Logic

```
sources = {}
# 1. User-global dotfile
user_dot = Path.home() / ".aprilcam"
if user_dot.exists():
    sources.update(_parse_dotfile(user_dot))
# 2. Project-local dotfile (walk up from start or cwd)
proj_dot = _find_dotfile(".aprilcam", start or Path.cwd())
if proj_dot:
    sources.update(_parse_dotfile(proj_dot))
# 3. .env via dotenv_values
env_file = _find_dotfile(".env", start or Path.cwd())
if env_file:
    sources.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
# 4. Process environment (highest priority)
sources.update({k: v for k, v in os.environ.items() if k.startswith("APRILCAM_")})
```

### Testing Plan

New test file `tests/test_config_loader.py`:
- Test default values when no config sources exist.
- Test env var overrides file value (monkeypatch `os.environ`).
- Test project-local `.aprilcam` overrides `~/.aprilcam` by writing temp files.
- Test `Config.load()` does not raise with no files present.
- Test `socket_dir` is created if it does not exist.
- Verify `AppConfig.load()` still works (no regression).

### Documentation Updates

None required. The `Config` class docstring is sufficient documentation.
