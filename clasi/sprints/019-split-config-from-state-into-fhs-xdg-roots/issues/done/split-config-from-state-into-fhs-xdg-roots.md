---
status: done
sprint: 019
tickets:
- 019-001
- 019-002
- 019-003
- 019-004
- 019-005
---

# Split AprilCam config from state into FHS/XDG roots

## Context

AprilCam's per-machine data lives under a **single** `data_dir` (default
`./data/aprilcam/`), and because `Config.load()` resolves relative paths against
`Path.cwd()`, **the daemon loads different config depending on where it is
launched** — a real fragility. That one tree also mixes three FHS categories:
admin-authored **config** (per-camera `config.json` UVC/mount/link settings,
`playfields/*.json` definitions), app-generated **state** (`calibration.json`,
`registry.json`, `hosts.json`, `info.json`, `paths.json`), and logs.

**Goal:** a stable, launch-independent, FHS/XDG-correct layout — ONE scheme for
both the Mac (non-root → XDG) and the Pi (root/systemd → FHS):

| Category | FHS (root / `APRILCAM_SYSTEM=1`) | XDG (non-root) | Contents |
|----------|----------------------------------|----------------|----------|
| **config** | `/etc/aprilcam` | `~/.config/aprilcam` | per-camera `config.json`, `playfields/*.json` |
| **state** (data_dir) | `/var/lib/aprilcam` | `~/.local/share/aprilcam` | `calibration.json`, `registry.json`, `hosts.json`, `info.json`, `paths.json` |
| runtime | `/run/aprilcam` | `$XDG_RUNTIME_DIR`/tmp | socket, pidfile (already correct) |
| logs | `/var/log/aprilcam` | `~/.local/state/aprilcam` | logs (already correct) |

Runtime + log roots already exist and are correct (Sprint 013). This work adds
the **config root** and moves config files into it; state stays under `data_dir`.

## Locked decisions

- **Mac dev: XDG home dirs.** config → `~/.config/aprilcam`, state →
  `~/.local/share/aprilcam`. **Remove** the CWD-relative
  `APRILCAM_DATA_DIR=./data/aprilcam` override from the repo `.env` so XDG
  defaults apply. Operator runs `aprilcam migrate-dirs` once to move current
  `data/aprilcam` contents into the XDG roots. Pi uses `/etc` + `/var/lib` via
  systemd.
- **Migration: explicit `aprilcam migrate-dirs` CLI only** (idempotent,
  `--dry-run`); the daemon logs a one-time WARNING on a legacy single-tree
  layout but never auto-moves.

## Key facts from code investigation

- `config.py`: `data_dir` field (~388) default `./data/aprilcam/`; `load()` (450)
  resolves relative paths vs `Path.cwd()` (485-498). `_default_dirs()` (318-358)
  returns exactly **3** dirs (data, socket, log). `cameras_dir`
  (=`data_dir/cameras`, 437) and `playfields_dir` (=`data_dir/playfields`, 442)
  hang off `data_dir`. `/etc/aprilcam*.env` is read only as **dotenv sources**
  (458-459), NOT a data root — the config root is a new concept.
- **RPC boundary is already file-scoped:** `Get/SetCameraConfig` touch only
  `config.json`; `Get/SetCalibration` only `calibration.json`; `Get/SetPaths`
  only `paths.json` (grpc_server.py 737-953).
- **The one real coupling:** `load_calibration_from_camera_dir(camera_dir, ...)`
  (calibration.py:375-508) reads `calibration.json` (436, STATE) AND overlays
  `config.json` (453, CONFIG) off the same dir — unless a pre-loaded
  `camera_config` dict is passed (param at 377 short-circuits the read; MCP path
  already passes it). Daemon-local callers that pass a bare dir:
  `camera_pipeline.py:272-273`, `grpc_server.py:297` & `878`, `stream.py:69`,
  `view_cli.py:557`.
- **Secondary coupling:** `core/playfield.py:_load_static_marker_ids` (933-961)
  reads `config.json` as a sibling of the calibration path.
- **Tertiary:** `calibrate_from_playfield_def` daemon-local branch
  (calibration.py:1348-1353) writes both files to one dir (CLI path; MCP
  `skip_save=True` already decoupled).
- `playfields/*.json` has **no in-app writer** (pure config). No migration helper
  exists — net-new.

## Approach

Add a **config root** parallel to `data_dir`; resolve per-camera and playfield
config from it; keep state under `data_dir`; make the loader take an explicit
config path (defaulting to the state dir for backward-compat) so nothing breaks
mid-migration.

1. **`config.py`** — `_default_dirs()` returns a 4th `config_dir` (FHS
   `/etc/aprilcam`, XDG `~/.config/aprilcam`); new field + `APRILCAM_CONFIG_DIR`
   env + `CONFIG_VARS` entry; properties `config_cameras_dir`,
   `config_playfields_dir`; repoint playfield-def consumers to
   `config_playfields_dir`. **Legacy fallback:** if `APRILCAM_CONFIG_DIR` unset
   and a legacy single-tree `data_dir` already holds config files, read config
   from `data_dir` (keeps existing installs working until migrated).
2. **`calibration.py`** — add optional `config_dir` param to
   `load_calibration_from_camera_dir` (read `config.json` from it when given,
   else `camera_dir`); `calibrate_from_playfield_def` daemon-local branch writes
   config to the config root, calibration to the state root.
3. **`core/playfield.py`** — `_load_static_marker_ids` takes the config dir
   explicitly.
4. **Thread the config root** through the daemon-local callers
   (`camera_pipeline`, `grpc_server` reload sites, `stream`, `view_cli`) and make
   `Get/SetCameraConfig` resolve to `config_cameras_dir/<cam>`; calibration/
   paths/registry stay on `cameras_dir` (state); `ListPlayfields` scans
   `config_playfields_dir`.
5. **Migration** — `aprilcam migrate-dirs` (explicit, idempotent, `--dry-run`):
   move `cameras/<slug>/config.json` → config root, `playfields/*.json` → config
   root; leave state under `data_dir`. Daemon warns (once) on legacy layout, no
   auto-move.
6. **systemd/deploy** — `deploy/aprilcamd.service` gains
   `ConfigurationDirectory=aprilcam` (→ `/etc/aprilcam`) beside the existing
   `StateDirectory=aprilcam` (→ `/var/lib/aprilcam`); update `deploy/README.md`
   + FHS table; document `APRILCAM_CONFIG_DIR`. Remove the CWD-relative override
   from the repo `.env`.
7. **Tests** — update shared-dir assumers
   (`test_config_calibration_split.py` (canonical fixture holds both files in one
   dir), `test_calibrate_from_def.py`, `test_sprint_012_integration.py`,
   `test_calibration_parallax.py`, `test_parallax_integration.py`,
   `test_open_camera_rehydrate.py`, `test_calibration_geometry_persist.py`,
   `test_file_proxy_rpcs.py`); add tests for the 4th config dir (FHS + XDG),
   `APRILCAM_CONFIG_DIR` override, loader reading config vs calibration from
   different roots, migration correctness + idempotency, legacy fallback. Tests
   must not touch real `/etc` or `/var/lib` — use tmp dirs via env overrides.

## Suggested ticket split

1. `config_dir` in config.py + `_default_dirs` + `CONFIG_VARS` + legacy fallback.
2. Loader decoupling (calibration.py + playfield.py `_load_static_marker_ids`).
3. Thread config root through daemon-local callers + RPC resolution.
4. `aprilcam migrate-dirs` command + daemon legacy-layout warning.
5. systemd/deploy + `.env` + docs.

Each ticket should leave the tree green.

## Verification

- `uv run pytest` green.
- `Config.load()` → config under `~/.config/aprilcam` (Mac) / `/etc/aprilcam`
  (root), state under `~/.local/share` / `/var/lib`.
- Launch daemon from TWO different CWDs → identical resolution.
- Run `migrate-dirs` on this repo's `data/aprilcam` → config.json + playfields
  move to the config root, calibration/registry/hosts stay in state; daemon
  opens a calibrated camera, `get_tags` returns world coords.
- Recalibrate → only `calibration.json` (state) changes; config root untouched.
