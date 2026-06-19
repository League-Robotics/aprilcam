---
id: '013'
title: FHS/XDG Directories, /etc Config Sourcing, --help & --agent
status: done
branch: sprint/013-fhs-xdg-directories-etc-config-sourcing-help-agent
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
issues:
- plan-fhs-xdg-directory-layout-etc-config-sourcing-self-documenting-help-and-agent.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 013: FHS/XDG Directories, /etc Config Sourcing, --help & --agent

## Goals

Move AprilCam to an FHS-correct, reboot-safe directory layout with XDG
fallbacks for non-root/development use. Source a system-wide
`/etc/aprilcam.env` at the lowest config precedence. Make `aprilcam --help`
self-document all configuration variables. Add `aprilcam --agent` to print
complete AI-agent instructions to stdout. Synchronize all deployment
artifacts (systemd unit, `.env.example`, docs) with the new layout.

## Problem

The current default directories are unsafe for production daemon use:

- `data_dir` defaults to `./data/aprilcam/` (cwd-relative) — causes
  state to scatter across invocation directories.
- `socket_dir` defaults to `/tmp/aprilcam/` which is wiped on reboot,
  destroying the pidfile alongside truly ephemeral data.
- No system-wide config sourcing from `/etc/` — operators cannot set
  defaults without touching per-user dotfiles.
- `aprilcam --help` lists only subcommands; it does not document the
  `APRILCAM_*` variables or configuration precedence, so operators must
  read source code to configure the daemon.
- AI-agent instructions are accessible only via the MCP protocol; no CLI
  path exists for humans or agents without an MCP session.
- The daemon log (`aprilcamd.log`) lives under `data_dir` (persistent
  state) rather than a dedicated log directory.

## Solution

1. Add `_default_dirs()` to `config.py` that selects FHS paths when
   `euid == 0` or `APRILCAM_SYSTEM=1`, and XDG paths otherwise.
2. Insert `/etc/aprilcam.env` and `/etc/aprilcam/aprilcam.env` as the
   lowest-priority config sources in `Config.load()`.
3. Add a `log_dir` field (`APRILCAM_LOG_DIR`) and route `aprilcamd.log`
   there; update both spawner sites in `client/control.py` and
   `daemon/client.py`.
4. Define `CONFIG_VARS` in `config.py` as the single source of truth for
   all `APRILCAM_*` variable metadata; reuse in `_print_help()` and
   `config_cli.py`.
5. Expand `_print_help()` with a Configuration section and `APRILCAM_*`
   variable table.
6. Add `--agent [robot]` flag to `cli/__init__.py`; factor a shared
   `read_guide(name)` helper used by both the CLI and MCP server.
7. Update `AGENT_GUIDE.md` to document the new config/dir model.
8. Create `deploy/aprilcamd.service` using systemd `*Directory=`
   directives; update `.env.example`, `.aprilcam`, and
   `docs/wiki/daemon-interface.md`.

## Success Criteria

- `aprilcam --help` shows a Configuration section with source-precedence
  chain and a table of every `APRILCAM_*` variable.
- `aprilcam config` shows resolved dirs: XDG paths when run as user,
  FHS paths when `sudo aprilcam config` (euid 0).
- `aprilcam --agent` prints `AGENT_GUIDE.md`; `--agent robot` prints
  `ROBOT_API_GUIDE.md`.
- Setting `APRILCAM_DATA_DIR` in `/etc/aprilcam.env` is honoured; a
  process `APRILCAM_*` env var still overrides it.
- `aprilcamd.log` lands in `log_dir`, not `data_dir`.
- All unit tests pass including new tests for `/etc` precedence, euid/XDG
  default selection, `log_dir` resolution, and `CONFIG_VARS` coverage.
- Version bumped per project rules.

## Scope

### In Scope

- `Config._default_dirs()` — FHS vs XDG automatic selection.
- `/etc/aprilcam.env` and `/etc/aprilcam/aprilcam.env` config sourcing.
- New `log_dir` field and `APRILCAM_LOG_DIR`.
- Log path migration in `client/control.py` and `daemon/client.py`.
- `CONFIG_VARS` structure in `config.py`.
- `_print_help()` Configuration section and env-var table.
- `config_cli.py` reuse of `CONFIG_VARS`.
- `--agent [robot]` CLI flag and `read_guide()` helper.
- `server/mcp_server.py` guide tools/resources refactored to use
  `read_guide()`.
- `AGENT_GUIDE.md` refresh (new config/dir model, `--agent` mention).
- `deploy/aprilcamd.service` systemd unit.
- `.env.example`, `.aprilcam`, `docs/wiki/daemon-interface.md` updates.
- Unit tests for all new behaviors.
- Version bump.

### Out of Scope

- `APRILCAM_DAEMON_HOST`/`APRILCAM_DAEMON_PORT` TCP networking (Sprint 014).
  Fields may be stubbed as documented-but-unused if convenient.
- Streamable HTTP transport.
- Any daemon networking changes.
- MCP tool protocol additions.

## Test Strategy

Unit tests in `tests/test_config_loader.py` (extend existing) and a new
`tests/test_config_fhs_xdg.py`:
- `/etc` precedence: monkeypatch `_parse_dotfile` to simulate
  `/etc/aprilcam.env` content; verify it beats no-source defaults but
  loses to `~/.aprilcam`.
- euid/XDG default selection: monkeypatch `os.geteuid` (return 0 vs 1000)
  and XDG env vars; assert `_default_dirs()` returns expected paths.
- `log_dir` resolution: verify default for both root and non-root modes.
- `CONFIG_VARS` coverage: assert every `APRILCAM_*` field defined in
  `Config` has an entry in `CONFIG_VARS` with a non-empty description.

CLI smoke tests extend `tests/test_cli_dispatch.py`:
- `--agent` and `--agent robot` exit 0 and write content to stdout.

Integration: `uv run pytest` full suite must pass without regressions.

## Architecture Notes

- `_default_dirs()` is a pure function; it does not call `Config.load()`.
  This avoids circular initialization.
- Permission errors when creating `data_dir`/`log_dir` under FHS paths
  (e.g. `/var/lib/aprilcam`) are caught and re-raised with a clear
  actionable message pointing to the systemd `*Directory=` directives.
- `read_guide(name)` lives in a new small module
  `src/aprilcam/guides.py` imported by both `cli/__init__.py` and
  `server/mcp_server.py`. This avoids a CLI→server import.
- `CONFIG_VARS` is a list of dicts (or a simple named tuple list) defined
  at module scope in `config.py`. Format:
  `{"key": "APRILCAM_LOG_LEVEL", "default": "INFO", "description": "..."}`.
  Both `_print_help()` and `config_cli.py` import and iterate it.
- The existing `socket_dir` mkdir at load is preserved; `data_dir` and
  `log_dir` are also created at load, but with a guarded `except
  PermissionError` that emits a clear message rather than crashing.

## GitHub Issues

(None — this sprint is driven by the internal issue file.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (APPROVE — no significant issues)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | `/etc` config sourcing and `_default_dirs()` in `config.py` | — |
| 002 | `log_dir` field and log-path migration in `control.py` and `daemon/client.py` | 001 |
| 003 | `CONFIG_VARS` table in `config.py` and `_print_help()` expansion | 001 |
| 004 | `--agent` flag in CLI and `read_guide()` helper in `guides.py` | — |
| 005 | `config_cli.py`: `log_dir` display and `CONFIG_VARS` reuse | 003 |
| 006 | Deploy artifacts and docs update | 001, 002, 003 |
| 007 | Tests for FHS/XDG defaults, `/etc` precedence, `log_dir`, `CONFIG_VARS` coverage, and version bump | 001, 002, 003, 004, 005, 006 |

Tickets execute serially in the order listed.
