---
id: '003'
title: CONFIG_VARS table in config.py and _print_help() expansion
status: done
use-cases:
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CONFIG_VARS table in config.py and _print_help() expansion

## Description

Create a single source of truth for all `APRILCAM_*` variable metadata and use
it to make `aprilcam --help` self-documenting.

**Part A — `CONFIG_VARS` in `src/aprilcam/config.py`:**

Add a module-level constant (after imports, before `_find_dotfile`):

```python
CONFIG_VARS: list[dict] = [
    {
        "key": "APRILCAM_DATA_DIR",
        "default": "(FHS: /var/lib/aprilcam · XDG: ~/.local/share/aprilcam)",
        "description": "Root directory for persistent state (cameras, calibrations, playfields).",
    },
    {
        "key": "APRILCAM_SOCKET_DIR",
        "default": "(FHS: /run/aprilcam · XDG: $XDG_RUNTIME_DIR/aprilcam)",
        "description": "Directory for the control socket, stream sockets, and pidfile.",
    },
    {
        "key": "APRILCAM_LOG_DIR",
        "default": "(FHS: /var/log/aprilcam · XDG: ~/.local/state/aprilcam)",
        "description": "Directory for aprilcamd.log.",
    },
    {
        "key": "APRILCAM_LOG_LEVEL",
        "default": "INFO",
        "description": "Python logging level for the daemon (DEBUG, INFO, WARNING, ERROR).",
    },
    {
        "key": "APRILCAM_DAEMON_PIDFILE",
        "default": "<socket_dir>/aprilcamd.pid",
        "description": "Pidfile path.",
    },
    {
        "key": "APRILCAM_DETECTION_FPS",
        "default": "10",
        "description": "Detection loop frame-rate cap in frames per second.",
    },
    {
        "key": "APRILCAM_STATIC_DESKEW",
        "default": "1",
        "description": "Enable homography-derived static-camera deskew (0 to disable).",
    },
    {
        "key": "APRILCAM_DESKEW_PX_PER_CM",
        "default": "0",
        "description": "Output resolution for the deskewed view in pixels/cm (0 = auto).",
    },
    {
        "key": "APRILCAM_UNDISTORT",
        "default": "0",
        "description": "Apply lens undistortion before deskew warp when intrinsics are present.",
    },
    {
        "key": "APRILCAM_MOVEMENT_THRESHOLD_PX",
        "default": "0",
        "description": "Movement-invalidation threshold in source pixels (0 = auto).",
    },
    {
        "key": "APRILCAM_SYSTEM",
        "default": "auto",
        "description": "Force FHS directory layout (1) or XDG (0); auto selects by euid.",
    },
]
```

**Part B — Expand `_print_help()` in `src/aprilcam/cli/__init__.py`:**

After the existing subcommand list and the closing blank line, append two
new sections:

```
Configuration:
  Source precedence (lowest wins first, highest last):
    /etc/aprilcam.env
    /etc/aprilcam/aprilcam.env
    ~/.aprilcam
    .aprilcam  (walk up from CWD)
    .env       (walk up from CWD, via dotenv)
    APRILCAM_* environment variables  (highest)

  Run 'aprilcam config' to see all resolved paths and current values.

Environment variables:
  VARIABLE                          DEFAULT                        DESCRIPTION
  APRILCAM_DATA_DIR                 (FHS/XDG auto)                 Root for persistent state ...
  ...
```

Generate the variable table by importing `CONFIG_VARS` from `aprilcam.config`
and formatting each entry. Use fixed column widths so the output aligns when
viewed in a terminal (e.g., key left-padded to 36 chars, default to 32 chars).

The `--agent` flag is listed in the help flags section in Ticket 004.

## Acceptance Criteria

- [x] `CONFIG_VARS` is defined at module scope in `config.py` as a list of
      dicts with `key`, `default`, and `description` fields.
- [x] Every `APRILCAM_*` variable currently handled in `Config.load()` has
      an entry in `CONFIG_VARS` (verified by Ticket 007 test).
- [x] `aprilcam --help` output includes a "Configuration:" section with the
      six-level precedence chain.
- [x] `aprilcam --help` output includes an "Environment variables:" section
      listing every entry from `CONFIG_VARS`.
- [x] `_print_help()` imports `CONFIG_VARS` from `aprilcam.config` — no
      duplication of variable metadata in `cli/__init__.py`.
- [x] `uv run pytest tests/test_cli_dispatch.py` passes.

## Implementation Plan

### Approach

Two files. Part A (`CONFIG_VARS`) must be done first because Part B imports it.

### Files to modify

- `src/aprilcam/config.py` — add `CONFIG_VARS` list after the imports block,
  before `_find_dotfile`.
- `src/aprilcam/cli/__init__.py` — expand `_print_help()`. Add
  `from ..config import CONFIG_VARS` import (lazy, inside `_print_help()` to
  avoid import cycles if any).

### Testing plan

Ticket 007 adds `test_config_vars_coverage` asserting every `Config` field that
corresponds to an `APRILCAM_*` key has a `CONFIG_VARS` entry.

After implementing, spot-check manually:
```
uv run aprilcam --help | grep -A 20 "Configuration:"
uv run aprilcam --help | grep -A 30 "Environment variables:"
```

### Documentation updates

`config_cli.py` consumes `CONFIG_VARS` in Ticket 005. The wiki and `.env.example`
are updated in Ticket 006.
