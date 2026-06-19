---
id: "005"
title: "config_cli.py: log_dir display and CONFIG_VARS reuse"
status: open
use-cases:
  - SUC-001
  - SUC-002
  - SUC-004
depends-on:
  - "003"
github-issue: ""
issue: ""
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# config_cli.py: log_dir display and CONFIG_VARS reuse

## Description

Update `aprilcam config` to show `log_dir` and, optionally, the full
`CONFIG_VARS` metadata so that `aprilcam config` and `aprilcam --help` draw
from the same source.

**Change A — add `log_dir` to `_collect()` in `src/aprilcam/cli/config_cli.py`:**

```python
def _collect(cfg: Config) -> dict:
    return {
        ...
        "log_dir": str(cfg.log_dir),           # new
        "socket_dir": str(cfg.socket_dir),
        ...
    }
```

Place `log_dir` after `playfields_dir` and before `socket_dir` (or in a
logical directory-layout grouping).

**Change B — update the Rich table description:**

The parser description in `main()` references the sources; update it to
mention `/etc/aprilcam.env` and `log_dir`.

**Change C — optional `--vars` flag:**

Add `--vars` to the argument parser. When passed, print a plain-text table of
all `CONFIG_VARS` entries (key, default, description) instead of the resolved
config. This is a developer utility for quickly listing all available variables.

```python
parser.add_argument(
    "--vars",
    action="store_true",
    help="List all APRILCAM_* variables with defaults and descriptions.",
)
```

In `main()`:
```python
if args.vars:
    from ..config import CONFIG_VARS
    for var in CONFIG_VARS:
        print(f"{var['key']:<40} {var['default']:<35} {var['description']}")
    return 0
```

## Acceptance Criteria

- [ ] `aprilcam config` output includes `log_dir` with the resolved path.
- [ ] `aprilcam config` output still includes `data_dir`, `socket_dir`, and
      all previously shown fields.
- [ ] `aprilcam config --vars` lists every entry from `CONFIG_VARS` with
      key, default, and description.
- [ ] `aprilcam config --json` includes `log_dir` in the JSON output.
- [ ] No duplication of variable metadata between `config_cli.py` and
      `config.py` (imports from `CONFIG_VARS`).
- [ ] `uv run pytest` passes.

## Implementation Plan

### Approach

Single file: `src/aprilcam/cli/config_cli.py`. Depends on Ticket 003
(needs `CONFIG_VARS` and `Config.log_dir`).

### Files to modify

- `src/aprilcam/cli/config_cli.py`
  - `_collect()`: add `"log_dir": str(cfg.log_dir)`.
  - `main()`: update description string; add `--vars` argument and handler.

### Testing plan

Ticket 007 adds CLI smoke tests. Manual check:
```
uv run aprilcam config
uv run aprilcam config --json | python3 -m json.tool
uv run aprilcam config --vars
```

### Documentation updates

None additional. The wiki table is updated in Ticket 006.
