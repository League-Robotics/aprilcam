---
id: '006'
title: Deploy artifacts and docs update
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
- '002'
- '003'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Deploy artifacts and docs update

## Description

Synchronize all deployment and documentation artifacts with the new directory
layout, `/etc` config sourcing, and `log_dir` field introduced in Tickets
001-003.

**File A — `deploy/aprilcamd.service` (new file):**

Create the directory `deploy/` if it does not exist, then create
`deploy/aprilcamd.service`:

```ini
[Unit]
Description=AprilCam tag-detection daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/aprilcam daemon start --foreground
Restart=on-failure
RestartSec=5

# systemd creates and owns these directories before ExecStart runs.
# When using DynamicUser=yes, also set APRILCAM_SYSTEM=1 so that
# the config loader selects FHS paths (DynamicUser does not use euid 0).
ConfigurationDirectory=aprilcam
StateDirectory=aprilcam
RuntimeDirectory=aprilcam
LogsDirectory=aprilcam
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Note: The service file deliberately does not set `User=` — operators choose
between `DynamicUser=yes` (recommended) or a dedicated `aprilcam` system user.
Add a comment block above `[Service]` explaining this choice and the
`APRILCAM_SYSTEM=1` requirement for `DynamicUser`.

**File B — `.env.example` (update):**

Add entries for `APRILCAM_LOG_DIR` and `APRILCAM_SYSTEM`. Update the
`APRILCAM_DATA_DIR` and `APRILCAM_SOCKET_DIR` comments to describe the
FHS/XDG auto-selection and note that these are only needed to override the
automatic defaults. Example additions:

```bash
# Log directory for aprilcamd.log (default: FHS /var/log/aprilcam or XDG ~/.local/state/aprilcam).
# APRILCAM_LOG_DIR=/var/log/aprilcam

# Force FHS (1) or XDG (0) directory layout regardless of euid.
# APRILCAM_SYSTEM=
```

**File C — `.aprilcam` (update):**

Update the loading-priority comment to include the two `/etc` sources at
priority 0:

```
#   0. /etc/aprilcam.env, /etc/aprilcam/aprilcam.env  (system-wide, lowest)
#   1. ~/.aprilcam          (user-global)
#   2. .aprilcam            (this file, project-local)
#   3. .env                 (via python-dotenv)
#   4. APRILCAM_* env vars  (process environment, highest)
```

Also add a commented-out `APRILCAM_LOG_DIR` entry with a description.

**File D — `docs/wiki/daemon-interface.md` (update):**

Update the precedence table (around line 265) to include the `/etc` rows:

| Priority | Source |
|----------|--------|
| 0 (lowest) | `/etc/aprilcam.env`, `/etc/aprilcam/aprilcam.env` |
| 1 | `~/.aprilcam` user dotfile |
| 2 | `.aprilcam` project dotfile (walk up from CWD) |
| 3 | `.env` file (walk up from CWD) |
| 4 (highest) | `APRILCAM_*` environment variables |

Update the variable table (lines 272-277) to include all `CONFIG_VARS`
entries — at minimum add `APRILCAM_LOG_DIR` and `APRILCAM_SYSTEM`. Use
`CONFIG_VARS` from `config.py` as the reference for defaults and descriptions.
Also add a "Directory Layout" subsection with the FHS vs XDG table from the
issue.

## Acceptance Criteria

- [x] `deploy/aprilcamd.service` exists with `ConfigurationDirectory=aprilcam`,
      `StateDirectory=aprilcam`, `RuntimeDirectory=aprilcam`,
      `LogsDirectory=aprilcam`, and `StandardOutput=journal`.
- [x] The service file includes a comment explaining `DynamicUser` + `APRILCAM_SYSTEM=1`.
- [x] `.env.example` has commented entries for `APRILCAM_LOG_DIR` and
      `APRILCAM_SYSTEM`.
- [x] `.env.example` comments for `APRILCAM_DATA_DIR` and `APRILCAM_SOCKET_DIR`
      describe auto-selection.
- [x] `.aprilcam` loading-priority comment includes the `/etc` sources.
- [x] `docs/wiki/daemon-interface.md` precedence table includes the `/etc` row.
- [x] `docs/wiki/daemon-interface.md` variable table includes `APRILCAM_LOG_DIR`
      and `APRILCAM_SYSTEM`.
- [x] `uv run pytest` passes (no code changes; this ticket is docs/config only).

## Implementation Plan

### Approach

Four files; no Python source changes. Read each file before editing.

### Files to create/modify

- `deploy/aprilcamd.service` — new file (create `deploy/` directory first
  if it does not exist).
- `.env.example` — add two new commented entries; update two existing comments.
- `.aprilcam` — update priority comment block; add `APRILCAM_LOG_DIR` entry.
- `docs/wiki/daemon-interface.md` — update precedence table and variable table.

### Testing plan

No new unit tests for this ticket. Run `uv run pytest` to confirm no
regressions from doc edits (there should be none).

### Documentation updates

This ticket IS the documentation update for the sprint.
