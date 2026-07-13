---
id: '002'
title: log_dir field and log-path migration in control.py and daemon/client.py
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# log_dir field and log-path migration in control.py and daemon/client.py

## Description

Route `aprilcamd.log` to `config.log_dir` instead of `config.data_dir`.
Currently both daemon spawner sites open the log file under `data_dir`; after
Ticket 001 adds `log_dir`, these two sites need a one-line fix each.

The daemon log belongs in the log directory (FHS: `/var/log/aprilcam/`, XDG:
`~/.local/state/aprilcam/`), not alongside persistent calibration/registry data.
When the systemd unit is used, `LogsDirectory=aprilcam` creates
`/var/log/aprilcam/` with correct ownership, so `mkdir` there will succeed
before the spawner runs.

**`src/aprilcam/client/control.py` line 128:**

```python
# Before
config.data_dir.mkdir(parents=True, exist_ok=True)
log_file = open(config.data_dir / "aprilcamd.log", "a")

# After
config.log_dir.mkdir(parents=True, exist_ok=True)
log_file = open(config.log_dir / "aprilcamd.log", "a")
```

**`src/aprilcam/daemon/client.py` line 153:**

```python
# Before
config.data_dir.mkdir(parents=True, exist_ok=True)
log_file = open(config.data_dir / "aprilcamd.log", "a")

# After
config.log_dir.mkdir(parents=True, exist_ok=True)
log_file = open(config.log_dir / "aprilcamd.log", "a")
```

Note: `Config.load()` already creates `log_dir` (Ticket 001), so the
`mkdir` call in the spawner is a safety net for the case where someone
constructs a `Config` object directly. Keep it.

## Acceptance Criteria

- [x] `client/control.py` opens `aprilcamd.log` under `config.log_dir`, not
      `config.data_dir`.
- [x] `daemon/client.py` opens `aprilcamd.log` under `config.log_dir`, not
      `config.data_dir`.
- [x] No other references to `config.data_dir / "aprilcamd.log"` remain in
      the codebase.
- [x] `uv run pytest` passes (no regressions).

## Implementation Plan

### Approach

Two files, two identical two-line changes. Confirm with:
```
grep -rn "aprilcamd.log" src/
```
before and after to catch any other occurrences.

### Files to modify

- `src/aprilcam/client/control.py` — line 127-128: change `data_dir` to
  `log_dir` in both the `mkdir` and the `open()` call.
- `src/aprilcam/daemon/client.py` — line 152-153: same change.

### Testing plan

Ticket 007 adds a test verifying that the spawner log path uses `log_dir`.
For now, run `uv run pytest` to check for regressions.

### Documentation updates

None. The log path is documented in Ticket 006 (daemon-interface.md and
`.env.example`).
