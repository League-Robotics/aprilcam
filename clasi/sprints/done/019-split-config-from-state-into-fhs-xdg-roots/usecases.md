---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 019 Use Cases

## SUC-001: Daemon resolves the same config regardless of launch directory

- **Actor**: Developer or operator launching `aprilcamd` (directly, via a
  shell alias, or via systemd) from an arbitrary working directory.
- **Preconditions**: No `APRILCAM_DATA_DIR` override set. Camera has an
  existing `config.json` and `calibration.json` from a prior session.
- **Main Flow**:
  1. Operator launches the daemon from `/home/eric` on Monday. `Config.load()`
     resolves `data_dir` via `_default_dirs()` (XDG or FHS per euid), NOT
     relative to `Path.cwd()`.
  2. Operator launches the daemon from `/tmp` on Tuesday (a different CWD).
  3. `Config.load()` resolves to the identical `data_dir` and new
     `config_dir` in both cases — launch directory has no effect on
     resolution.
  4. The daemon opens the same camera and loads the same calibration and
     config in both sessions.
- **Postconditions**: Config resolution is a pure function of environment
  (euid, XDG/FHS env vars, `APRILCAM_*` overrides) — never of `Path.cwd()`.
- **Acceptance Criteria**:
  - [ ] `Config.load()` invoked with two different `start` / cwd values (no
        `APRILCAM_DATA_DIR`/`APRILCAM_CONFIG_DIR` set) returns identical
        `data_dir` and `config_dir`.
  - [ ] Non-root: `config_dir` resolves to `~/.config/aprilcam`
        (or `$XDG_CONFIG_HOME/aprilcam`).
  - [ ] Root / `APRILCAM_SYSTEM=1`: `config_dir` resolves to `/etc/aprilcam`.
  - [ ] `APRILCAM_CONFIG_DIR` explicit override wins over both defaults.

---

## SUC-002: Operator migrates an existing single-tree install to config/state roots

- **Actor**: Developer or operator with an existing AprilCam install using
  the legacy single `data_dir` tree (config and state files co-located).
- **Preconditions**: `data_dir` contains `cameras/<slug>/config.json`,
  `cameras/<slug>/calibration.json`, and (optionally) `playfields/*.json`,
  all in one tree. `APRILCAM_CONFIG_DIR` is unset.
- **Main Flow**:
  1. Operator runs `aprilcam migrate-dirs --dry-run` and reviews the planned
     moves: each `config.json` under `cameras/<slug>/` →
     `<config_dir>/cameras/<slug>/config.json`; each `playfields/*.json` →
     `<config_dir>/playfields/*.json`. State files (`calibration.json`,
     `registry.json`, `hosts.json`, `info.json`, `paths.json`) are listed as
     "stay" (not moved).
  2. Operator runs `aprilcam migrate-dirs` (no `--dry-run`) to execute.
  3. Config files move to the config root; state stays under `data_dir`.
  4. Operator runs `aprilcam migrate-dirs` again — it reports "nothing to
     migrate" (idempotent, no error, no duplicate work).
  5. Daemon starts, opens a previously calibrated camera, and `get_tags`
     still returns world coordinates (calibration + config both resolve
     correctly post-migration).
- **Postconditions**: Config and state live in separate FHS/XDG-correct
  roots. No data loss. Command is safe to re-run.
- **Acceptance Criteria**:
  - [ ] `--dry-run` reports the exact planned moves and performs no writes.
  - [ ] A real run moves only config-category files
        (`config.json`, `playfields/*.json`); state-category files are
        untouched in place.
  - [ ] Re-running after a successful migration is a no-op (idempotent) and
        exits 0.
  - [ ] Post-migration, `load_calibration_from_camera_dir` with an explicit
        `config_dir` still returns a fully-populated calibration (config
        overlay fields present).
  - [ ] The daemon never performs this move automatically — only the
        explicit CLI command writes to the config root.

---

## SUC-003: Daemon warns once on a legacy layout without touching files

- **Actor**: Operator who has not yet run `migrate-dirs`.
- **Preconditions**: `APRILCAM_CONFIG_DIR` unset. `data_dir` still holds
  `config.json` files alongside state files (legacy single-tree layout).
- **Main Flow**:
  1. Operator starts the daemon without migrating.
  2. On startup, the daemon detects that `config_dir` (the new default root)
     has no config files but `data_dir` does — a legacy layout.
  3. The daemon logs one WARNING pointing at `aprilcam migrate-dirs`, then
     proceeds using `data_dir` as the config-read fallback (per-camera
     `config.json` and `playfields/*.json` are still read successfully from
     the legacy location).
  4. The daemon does not create, move, or delete any files as a result of
     this detection.
  5. Subsequent detection loops / reloads within the same process do not
     repeat the warning.
- **Postconditions**: Operator is informed of the legacy layout and pointed
  at the fix, but nothing breaks and nothing moves without explicit consent.
- **Acceptance Criteria**:
  - [ ] Legacy layout (config files only under `data_dir`, none under
        `config_dir`) triggers exactly one WARNING log line per daemon
        process lifetime.
  - [ ] Config reads still succeed against `data_dir` in this fallback case
        — no functional regression pre-migration.
  - [ ] No file under `data_dir` or `config_dir` is created, moved, or
        deleted by the warning path.
  - [ ] Once `config_dir` has config files (post-migration), the warning
        does not fire.

---

## SUC-004: Recalibration never modifies config-root files

- **Actor**: Operator or automated calibration flow
  (`calibrate_from_playfield_def` / MCP `calibrate_playfield`).
- **Preconditions**: Camera has a config root entry (`config.json`) and a
  state root entry (`calibration.json`) in their respective roots
  (post-migration or fresh XDG/FHS install).
- **Main Flow**:
  1. Operator triggers recalibration (CLI `aprilcam calibrate` or MCP
     `calibrate_playfield`).
  2. The calibration routine computes new homography/geometry and writes
     only `calibration.json` under the state root
     (`<data_dir>/cameras/<slug>/calibration.json`).
  3. `config.json` under the config root is not written, even though the
     loader reads static fields (`device_name`, `resolution`, `settings`,
     `static_marker_ids`, `camera_position`) from it during the overlay
     step.
  4. A subsequent `load_calibration_from_camera_dir(state_dir,
     config_dir=config_dir)` call returns a calibration with fresh geometry
     AND the unchanged static config overlay.
- **Postconditions**: Config root is administrator-owned and immutable by
  any app-triggered calibration flow; only `migrate-dirs` and manual editing
  touch it.
- **Acceptance Criteria**:
  - [ ] Recalibration via `calibrate_from_playfield_def` writes
        `calibration.json` under the state root only.
  - [ ] `config.json`'s mtime and contents under the config root are
        unchanged after recalibration.
  - [ ] MCP `skip_save=True` calibration path (already decoupled) continues
        to write neither file.
  - [ ] Post-recalibration `get_tags`/`where` results reflect the new
        geometry while device/settings/marker-id fields remain the
        pre-recalibration config-root values.

---

## SUC-005: RPC clients read/write config and state through the correct root

- **Actor**: MCP server / CLI acting as a gRPC client of `aprilcamd`.
- **Preconditions**: Daemon running with distinct `config_dir` and
  `data_dir`.
- **Main Flow**:
  1. Client calls `GetCameraConfig` / `SetCameraConfig` — daemon resolves
     `config_cameras_dir/<slug>/config.json` (config root).
  2. Client calls `GetCalibration` / `SetCalibration` — daemon resolves
     `cameras_dir/<slug>/calibration.json` (state root, unchanged).
  3. Client calls `GetPaths` / `SetPaths` — daemon resolves
     `cameras_dir/<slug>/paths.json` (state root, unchanged).
  4. Client calls `ListPlayfields` — daemon scans `config_playfields_dir`
     (config root) instead of the old `playfields_dir` off `data_dir`.
- **Postconditions**: Every RPC's file target matches its FHS category
  (config vs. state); no RPC crosses roots.
- **Acceptance Criteria**:
  - [ ] `SetCameraConfig` writes land under `config_dir`, never `data_dir`.
  - [ ] `SetCalibration`/`SetPaths` writes land under `data_dir`, never
        `config_dir`.
  - [ ] `ListPlayfields` returns definitions found under `config_dir`, and a
        definition placed only under the legacy `data_dir/playfields` is
        still found via the legacy-fallback path (SUC-003) until migrated.

---

## SUC-006: systemd deployment provisions both roots correctly

- **Actor**: System operator deploying `aprilcamd` via the systemd unit on
  the Pi.
- **Preconditions**: `deploy/aprilcamd.service` in use; operator has not
  hand-edited directory paths.
- **Main Flow**:
  1. systemd starts the unit; `ConfigurationDirectory=aprilcam` provisions
     `/etc/aprilcam` and `StateDirectory=aprilcam` provisions
     `/var/lib/aprilcam`, both owned by the service user, before
     `ExecStart` runs.
  2. The daemon starts under `APRILCAM_SYSTEM=1`-equivalent conditions (or
     explicit `Environment=` override) and resolves `config_dir` to
     `/etc/aprilcam`, `data_dir` to `/var/lib/aprilcam` — not the
     hardcoded `/home/eric/aprilcam-data` path currently set in the unit's
     `Environment=` line.
  3. Operator consults `deploy/README.md`'s FHS table to confirm which
     directory holds which file category.
- **Postconditions**: Config and state directories exist with correct
  ownership at first boot, without manual `mkdir`.
- **Acceptance Criteria**:
  - [ ] `deploy/aprilcamd.service` declares both `ConfigurationDirectory=`
        and `StateDirectory=` (already present for state; config directive
        confirmed/kept consistent with the new config root).
  - [ ] The unit's `Environment=` lines are updated to stop hardcoding a
        non-FHS `APRILCAM_DATA_DIR` path, or documented as an intentional
        override.
  - [ ] `deploy/README.md` documents `APRILCAM_CONFIG_DIR`, the FHS config
        row, and references `migrate-dirs` for existing Pi installs.
