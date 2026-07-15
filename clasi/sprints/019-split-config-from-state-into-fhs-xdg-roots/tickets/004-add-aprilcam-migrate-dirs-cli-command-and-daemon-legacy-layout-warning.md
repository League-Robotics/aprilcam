---
id: '004'
title: Add aprilcam migrate-dirs CLI command and daemon legacy-layout warning
status: open
use-cases: [SUC-002, SUC-003]
depends-on: ['003']
github-issue: ''
issue: split-config-from-state-into-fhs-xdg-roots.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add aprilcam migrate-dirs CLI command and daemon legacy-layout warning

## Description

Per the locked stakeholder decision, migration from the legacy single-tree
layout to the new config/state split is **explicit and operator-initiated
only** — the daemon must never move files on its own. This ticket adds:

1. **`aprilcam migrate-dirs` CLI command** (new `cli/migrate_dirs_cli.py`
   or added to an existing CLI module — implementer's discretion,
   following the project's existing CLI subcommand registration pattern)
   with a `--dry-run` flag:
   - Scans `data_dir/cameras/*/config.json` and
     `data_dir/playfields/*.json` — the legacy config-in-state-tree
     locations.
   - For each, computes the destination under `config_dir` (the true
     FHS/XDG target, NOT `effective_config_dir` — migration always targets
     the correct new root, creating parent directories as needed).
   - `--dry-run`: prints the planned moves (source → destination) and
     performs zero writes.
   - Real run: performs the move (`Path.rename()` when same filesystem;
     otherwise copy-then-delete) and prints a summary of what moved.
   - **Idempotent**: files already absent from the legacy location (i.e.
     already migrated) are skipped silently; a summary distinguishes
     "moved N files" from "nothing to migrate — already up to date."
   - **Never touches state-category files**
     (`calibration.json`, `registry.json`, `hosts.json`, `info.json`,
     `paths.json`) — these stay under `data_dir` unconditionally, in
     every run mode.
   - Conflict handling (Open Question 3 in `architecture-update.md`):
     default to skip-and-warn when a destination file already exists
     (report the conflict, do not silently overwrite admin-authored
     config) unless a future ticket specifies otherwise.
   - Exit code 0 on success (including the no-op case); nonzero on
     partial failure (e.g. a permission error mid-move), with per-file
     error reporting.

2. **Daemon one-time legacy-layout WARNING**: at daemon startup (in
   `Config.load()`'s call site or the first `CameraPipeline.start()` —
   implementer's discretion for exact hook point, but must run once per
   process, not per detection-loop iteration), detect: does `config_dir`
   (the true target, not `effective_config_dir`) have zero config files
   while `data_dir` has at least one? If so, log exactly one WARNING
   (e.g. `"legacy config layout detected under %s; run 'aprilcam
   migrate-dirs' to relocate config files — see docs"`) and proceed using
   the ticket-001 `effective_config_dir` fallback read path. A
   process-lifetime flag (module-level or on the `Config`/daemon instance)
   suppresses repeat warnings within the same process. The warning path
   performs **zero file operations** — detection only.

See `../architecture-update.md` §5 ("Migration") and Migration Concerns
("No automatic migration — explicit CLI only", "Legacy layout must keep
working until migrated") for full design detail and rationale.

## Acceptance Criteria

- [ ] `aprilcam migrate-dirs --dry-run` prints the exact planned moves
      (source path → destination path for each `config.json` and each
      `playfields/*.json` found under the legacy `data_dir` location) and
      performs zero filesystem writes.
- [ ] `aprilcam migrate-dirs` (no `--dry-run`) moves only
      `cameras/<slug>/config.json` → `<config_dir>/cameras/<slug>/
      config.json` and `playfields/*.json` → `<config_dir>/playfields/
      *.json`; all state-category files remain byte-identical and in
      place under `data_dir` after the run.
- [ ] Re-running `aprilcam migrate-dirs` after a successful migration
      exits 0 and reports "nothing to migrate" — no error, no duplicate
      writes, no file changes.
- [ ] A destination-already-exists conflict is reported (skip-and-warn)
      rather than silently overwritten.
- [ ] Post-migration, `load_calibration_from_camera_dir` (with an explicit
      `config_dir` per ticket 002) returns a fully-populated calibration
      (config overlay fields present) for a migrated camera.
- [ ] The daemon logs exactly one WARNING per process lifetime when
      started against a legacy single-tree layout (config files present
      under `data_dir`, none under `config_dir`); config reads still
      succeed via the fallback in this scenario (no functional
      regression).
- [ ] The daemon logs zero WARNINGs when `config_dir` already has config
      files (post-migration or fresh split-root install).
- [ ] Neither the migration command's `--dry-run` mode nor the daemon
      warning path creates, moves, or deletes any file.
- [ ] Full `uv run pytest` green.

## Testing

- **Existing tests to run**: full `uv run pytest` (regression check —
  this ticket should not touch any existing loader/RPC test paths from
  tickets 001-003).
- **New tests to write**:
  - `migrate-dirs --dry-run` against a tmp legacy-layout fixture (config +
    state co-located) — assert the printed plan matches expected
    source/destination pairs and no files moved on disk.
  - `migrate-dirs` real run against the same fixture — assert config files
    moved to the tmp config root, state files unchanged in place (compare
    mtimes/content before and after for state files).
  - Idempotency: run `migrate-dirs` twice in a row — second run reports
    "nothing to migrate," exits 0, no file changes between the two runs.
  - Conflict case: pre-create a destination `config.json` with different
    content, run `migrate-dirs`, assert the source file was NOT deleted
    and a conflict/warning was reported, destination content unchanged.
  - Playfield definitions: `playfields/*.json` under legacy `data_dir`
    migrate to `config_playfields_dir`.
  - Daemon warning: start a daemon (or the equivalent
    `Config.load()`/startup-hook unit under test) against a tmp legacy
    layout — assert exactly one WARNING log record; start again against a
    tmp split-root layout — assert zero WARNING log records.
  - Daemon warning path performs no I/O — assert no new file appears
    under either tmp `data_dir` or tmp `config_dir` as a result of
    triggering the warning.
- **Verification command**: `uv run pytest`
