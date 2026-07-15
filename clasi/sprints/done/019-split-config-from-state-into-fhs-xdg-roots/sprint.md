---
id: 019
title: Split config from state into FHS XDG roots
status: done
branch: sprint/019-split-config-from-state-into-fhs-xdg-roots
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
issues:
- split-config-from-state-into-fhs-xdg-roots.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 019: Split config from state into FHS XDG roots

## Goals

Give AprilCam a stable, launch-independent, FHS/XDG-correct directory
layout by splitting the single `data_dir` tree into two roots: a new
**config root** for admin-authored files (per-camera `config.json`,
`playfields/*.json`) and the existing **state root** (`data_dir`) for
app-generated files (`calibration.json`, `registry.json`, `hosts.json`,
`info.json`, `paths.json`). One scheme covers both the Mac dev machine
(XDG: `~/.config/aprilcam` / `~/.local/share/aprilcam`) and the Pi
(FHS via systemd: `/etc/aprilcam` / `/var/lib/aprilcam`).

## Problem

`Config.load()` resolves relative `data_dir` paths against `Path.cwd()`
(485-498 in `config.py`), so **the daemon loads different config depending
on where it is launched** — a genuine fragility that has already bitten
this project. Compounding this, the single `data_dir` tree mixes three FHS
categories in one place: admin-authored config, app-generated state, and
(historically) logs. Sprint 013 already split logs out into a correct
`log_dir` (FHS `/var/log/aprilcam` / XDG `~/.local/state/aprilcam`); this
sprint applies the same discipline to the remaining config/state mixing.

The one real code coupling that makes this non-trivial:
`load_calibration_from_camera_dir(camera_dir, ...)` (`calibration.py`
375-508) reads `calibration.json` (state) AND overlays `config.json`
(config) from the *same* directory (453) unless a pre-loaded
`camera_config` dict is passed — the MCP path already does this, but five
daemon-local callers (`camera_pipeline.py`, `grpc_server.py` at two reload
sites, `stream.py`, `view_cli.py`) pass a bare directory and rely on the
sibling-file assumption. `core/playfield.py:_load_static_marker_ids` (933)
has the identical sibling-file assumption on the calibration file's parent
directory. Both must learn to read config from a *different* root than
calibration without breaking the MCP path or existing installs mid-migration.

## Solution

Add a config root parallel to `data_dir`, resolved by the same FHS/XDG
selection logic already in `_default_dirs()` (Sprint 013), extended from a
3-tuple to a 4-tuple. Thread an explicit, optional `config_dir` parameter
through the one coupled loader and its daemon-local callers so config and
state can live in different roots without breaking backward compatibility
(omitting the parameter preserves today's sibling-read behavior exactly).
Repoint the RPC boundary's config-touching endpoints
(`GetCameraConfig`/`SetCameraConfig`, `ListPlayfields`) to the config root
while leaving the state-touching endpoints
(`GetCalibration`/`SetCalibration`, `GetPaths`/`SetPaths`) on the state
root — the RPC boundary is already file-scoped (Sprint 015+), so this is a
repoint, not a redesign.

Migration is **explicit and operator-initiated only**: a new
`aprilcam migrate-dirs` CLI command (idempotent, `--dry-run`) moves config
files to the new root; the daemon never auto-moves anything, but logs one
WARNING per process lifetime when it detects a legacy single-tree layout,
and falls back to reading config from `data_dir` in that case so nothing
breaks pre-migration. The Mac dev `.env` drops its CWD-relative
`APRILCAM_DATA_DIR=./data/aprilcam` override so XDG defaults take effect;
the Pi continues through `deploy/aprilcamd.service`'s systemd
`*Directory=` directives.

Full design detail, verified code references, diagrams, and rationale are
in `architecture-update.md`.

## Success Criteria

- `Config.load()` resolves identical `data_dir` and `config_dir` values
  regardless of which directory the daemon is launched from (no
  `APRILCAM_*` overrides set).
- Non-root: `config_dir` defaults to `~/.config/aprilcam`
  (or `$XDG_CONFIG_HOME/aprilcam`). Root / `APRILCAM_SYSTEM=1`: `/etc/aprilcam`.
  `APRILCAM_CONFIG_DIR` overrides both.
- `aprilcam migrate-dirs --dry-run` reports the exact planned moves with no
  writes; a real run moves only config-category files and leaves all
  state-category files untouched in place; re-running after success is a
  no-op; a mid-run failure reports per-file errors without silent partial
  state.
- A legacy single-tree install keeps working with zero action: the daemon
  logs exactly one WARNING per process lifetime and falls back to reading
  config from `data_dir`.
- Recalibration (`calibrate_from_playfield_def`, MCP `calibrate_playfield`)
  never writes to the config root — only `calibration.json` under the
  state root changes.
- `GetCameraConfig`/`SetCameraConfig`/`ListPlayfields` resolve against the
  config root; `GetCalibration`/`SetCalibration`/`GetPaths` continue to
  resolve against the state root.
- `deploy/aprilcamd.service` and `deploy/README.md` are internally
  consistent with the new model; the repo `.env`'s CWD-relative
  `APRILCAM_DATA_DIR` override is removed.
- `uv run pytest` is green throughout, ticket by ticket.

## Scope

### In Scope

- `config.py`: `config_dir` field, `APRILCAM_CONFIG_DIR` env var, 4th
  `_default_dirs()` return element (FHS `/etc/aprilcam`, XDG
  `~/.config/aprilcam`), `CONFIG_VARS` entry, `config_cameras_dir` /
  `config_playfields_dir` / `effective_config_dir` properties, legacy
  single-tree fallback.
- `calibration/calibration.py`: optional `config_dir` parameter on
  `load_calibration_from_camera_dir`; `calibrate_from_playfield_def`'s
  daemon-local write branch split across the two roots.
- `core/playfield.py`: `_load_static_marker_ids` takes an explicit,
  optional config directory instead of assuming a sibling file.
- Threading `config_dir` through daemon-local callers:
  `daemon/camera_pipeline.py`, `daemon/grpc_server.py` (both reload
  sites), `stream.py`, `cli/view_cli.py`.
- RPC resolution repoint: `Get/SetCameraConfig` and `ListPlayfields` to
  the config root; `core/playfield_def.py`'s `load_all()` daemon-startup
  caller repointed to `config_playfields_dir`.
- New `aprilcam migrate-dirs` CLI command (`--dry-run`, idempotent) and a
  one-time daemon legacy-layout WARNING.
- `deploy/aprilcamd.service` reconciliation (existing
  `ConfigurationDirectory=aprilcam` directive confirmed; hardcoded
  `Environment=APRILCAM_DATA_DIR=/home/eric/aprilcam-data` override
  **removed** per stakeholder decision, so the Pi resolves proper FHS
  defaults) and `deploy/README.md` FHS table + docs update, including the
  one-time Pi-side `migrate-dirs` step to relocate
  `/home/eric/aprilcam-data` content into `/etc/aprilcam` + `/var/lib/aprilcam`.
- Repo `.env`: remove `APRILCAM_DATA_DIR=./data/aprilcam`.
- Updating all shared-dir-assuming tests listed in Test Strategy below.

### Out of Scope

- Any change to `calibration.json`, `registry.json`, `hosts.json`,
  `info.json`, or `paths.json` locations, formats, or ownership — these
  stay on the state root (`data_dir`/`cameras_dir`) unconditionally.
  `camera/registry.py` and `client/host_codes.py` are reviewed and
  confirmed out of scope, not silently touched.
- Automatic/implicit migration of any kind — locked decision, explicit
  `migrate-dirs` CLI only.
- Any change to the daemon wire protocol, gRPC message shapes, or MCP tool
  signatures — only file-resolution targets change server-side.
- Sprint 013's log_dir or socket_dir behavior — already correct, untouched.
- Renaming or changing the meaning of the existing `cameras_dir` /
  `playfields_dir` properties — they keep meaning "state root" throughout.

## Test Strategy

No hardware required. All new tests use tmp directories via env var
overrides (`APRILCAM_DATA_DIR`, `APRILCAM_CONFIG_DIR`,
`APRILCAM_SYSTEM`) — never touch real `/etc` or `/var/lib`.

- **Ticket 1** (`config.py`): `_default_dirs()` 4th element under FHS and
  XDG branches; `APRILCAM_CONFIG_DIR` override wins over both defaults;
  legacy fallback (`effective_config_dir` reads from `data_dir` when
  `config_dir` is empty and `data_dir` holds config files);
  `test_config_loader.py`/`test_config_cli.py` gain `config_dir` coverage.
- **Ticket 2** (loader decoupling): `load_calibration_from_camera_dir`
  reads config from `config_dir` when given, calibration from `camera_dir`
  always; omitting `config_dir` preserves exact current sibling-read
  behavior (regression guard); `camera_config` dict short-circuit remains
  unchanged (MCP path); `_load_static_marker_ids` reads from an explicit
  config dir when given, falls back to sibling lookup otherwise; update
  `test_config_calibration_split.py` (keep the canonical co-located
  fixture as the backward-compat case, add a split-root case),
  `test_calibrate_from_def.py`, `test_sprint_012_integration.py`,
  `test_calibration_parallax.py`, `test_parallax_integration.py`,
  `test_open_camera_rehydrate.py`, `test_calibration_geometry_persist.py`.
- **Ticket 3** (wiring): each daemon-local caller passes the correct
  `config_dir`; RPC resolution — `SetCameraConfig` writes land under
  `config_dir` never `data_dir`, `SetCalibration`/`SetPaths` writes land
  under `data_dir` never `config_dir`, `ListPlayfields` scans
  `config_playfields_dir`; update `test_file_proxy_rpcs.py`.
- **Ticket 4** (migration): `migrate-dirs` moves exactly the config-category
  files and leaves state-category files untouched; `--dry-run` performs no
  writes and reports the accurate plan; idempotent re-run is a no-op;
  daemon legacy-layout warning fires exactly once per process lifetime and
  performs no file operations.
- **Ticket 5** (deploy): systemd unit smoke-checks (directive presence,
  `Environment=APRILCAM_DATA_DIR` override absent, FHS resolution
  documented); Pi-side `migrate-dirs` run is a documented manual deployment
  step, not an automated test — no runtime test against a real Pi.
- Full `uv run pytest` green after every ticket.

## Architecture Notes

See `architecture-update.md` for the complete design: verified code
references (with line numbers spot-checked against current source),
component and dependency diagrams, and design rationale for three key
decisions — (1) `config_dir` as a 4th `_default_dirs()` tuple element
rather than a separate function, (2) `effective_config_dir` as a distinct
read-time accessor from the write-time `config_dir` target (so
`migrate-dirs --dry-run` reports an honest plan), and (3) the loader's
`config_dir` as an optional parameter rather than a new function or a
global-singleton flag, preserving backward compatibility and testability.

Open Question 1 (systemd `APRILCAM_DATA_DIR` override) is **resolved by
stakeholder decision**: remove `Environment=APRILCAM_DATA_DIR=/home/eric/
aprilcam-data` from `deploy/aprilcamd.service` so the Pi uses proper FHS
defaults (`/var/lib/aprilcam` state, `/etc/aprilcam` config) via the
existing `ConfigurationDirectory=`/`StateDirectory=` directives. Ticket 5
performs and documents the one-time Pi-side migration: run
`aprilcam migrate-dirs` on the Pi to relocate config out of
`/home/eric/aprilcam-data` into `/etc/aprilcam`, then relocate remaining
state content into `/var/lib/aprilcam`, before removing the override and
restarting the unit.

Two open questions remain, deferred to ticketing (not blocking sprint
execution): (2) write-ordering/failure-reporting for
`calibrate_from_playfield_def`'s now-two-root write, (3) `migrate-dirs`
conflict handling when a destination file already exists (default
preference: skip-and-warn, don't clobber).

## GitHub Issues

(none — tracked via `clasi/issues/split-config-from-state-into-fhs-xdg-roots.md`)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (APPROVE)
- [x] Stakeholder has approved the sprint plan (approved; gate recorded by team-lead; Open Question 1 resolved — remove the Pi's hardcoded `APRILCAM_DATA_DIR` override, migrate via `migrate-dirs`)

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Add config_dir root, APRILCAM_CONFIG_DIR, and legacy fallback to config.py | — |
| 002 | Decouple config reads from calibration reads in the loader and playfield marker-id lookup | 001 |
| 003 | Thread config_dir through daemon-local callers and repoint config-touching RPCs | 002 |
| 004 | Add aprilcam migrate-dirs CLI command and daemon legacy-layout warning | 003 |
| 005 | Reconcile systemd unit and deploy docs; remove repo .env CWD-relative override | 004 |

Tickets execute serially in the order listed.
