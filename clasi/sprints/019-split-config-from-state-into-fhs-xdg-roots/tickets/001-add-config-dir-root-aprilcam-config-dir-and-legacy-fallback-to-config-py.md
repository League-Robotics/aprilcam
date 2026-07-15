---
id: '001'
title: Add config_dir root, APRILCAM_CONFIG_DIR, and legacy fallback to config.py
status: open
use-cases: [SUC-001, SUC-003]
depends-on: []
github-issue: ''
issue: split-config-from-state-into-fhs-xdg-roots.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add config_dir root, APRILCAM_CONFIG_DIR, and legacy fallback to config.py

## Description

Foundation ticket for the sprint. `config.py`'s `_default_dirs()` (318-358)
currently returns a 3-tuple `(data_dir, socket_dir, log_dir)`. This ticket
extends it to a 4-tuple by adding `config_dir`, resolved by the same
FHS-vs-XDG `use_fhs` branch already computed in the function — FHS
(`os.geteuid() == 0` or `APRILCAM_SYSTEM=1`) → `/etc/aprilcam`; XDG
(default) → `$XDG_CONFIG_HOME/aprilcam` or `~/.config/aprilcam`.

`Config` gains a `config_dir: Path` field, resolved in `load()` (450-...)
the same way `data_dir`/`socket_dir`/`log_dir` already are, via the
existing `_path("APRILCAM_CONFIG_DIR", _cd)` pattern where `_cd` is the
new 4th tuple element. A new `CONFIG_VARS` (200-278) entry documents
`APRILCAM_CONFIG_DIR`.

New properties: `config_cameras_dir` and `config_playfields_dir`, parallel
to the existing `cameras_dir`/`playfields_dir` properties (434-442) which
keep their current meaning (state root, i.e. `data_dir/cameras`,
`data_dir/playfields`) — **do not rename or repoint them in this ticket**.

**Legacy fallback**: add an `effective_config_dir` property (or
equivalent resolved accessor) that returns `config_dir` normally, but
falls back to `data_dir` when (a) `APRILCAM_CONFIG_DIR` was NOT explicitly
set, AND (b) `config_dir` (the new default) currently has no config files
under it, AND (c) `data_dir` DOES have config files under it (i.e.
`data_dir/cameras/*/config.json` exists). This keeps every existing
install — including this repo's own `data/aprilcam` tree — working with
zero action until `migrate-dirs` (ticket 004) is run. Read call sites use
`effective_config_dir`; write call sites (`SetCameraConfig` in ticket 003,
`migrate-dirs` in ticket 004) use the plain `config_dir` (the true FHS/XDG
target) so a dry-run plan is never confused by the fallback.

This ticket makes **no functional behavior change** beyond exposing the
new field/properties — nothing yet reads or writes through
`config_cameras_dir`/`config_playfields_dir`/`effective_config_dir` (that
wiring is tickets 002-003). The full tree must remain green.

See `../architecture-update.md` §1 ("`config.py` — new `config_dir` root,
4th `_default_dirs()` return, legacy fallback") and Design Rationale
("`config_dir` as a 4th `_default_dirs()` return" and "legacy fallback as
a separate `effective_config_dir` accessor") for full design detail and
rationale.

## Acceptance Criteria

- [ ] `_default_dirs()` returns a 4-tuple `(data_dir, socket_dir, log_dir,
      config_dir)`; FHS branch returns `Path("/etc/aprilcam")` as the 4th
      element; XDG branch returns `$XDG_CONFIG_HOME/aprilcam` (when set)
      or `~/.config/aprilcam` (fallback) as the 4th element.
- [ ] `Config` dataclass has a `config_dir: Path` field.
- [ ] `Config.load()` resolves `config_dir` from `APRILCAM_CONFIG_DIR` when
      set, else from `_default_dirs()`'s 4th element — mirroring the exact
      pattern used for `data_dir`/`socket_dir`/`log_dir`.
- [ ] `APRILCAM_SYSTEM=1` (or root euid) yields `config_dir ==
      Path("/etc/aprilcam")` when `APRILCAM_CONFIG_DIR` is unset.
- [ ] Non-root, `APRILCAM_SYSTEM` unset, yields `config_dir ==
      Path.home() / ".config" / "aprilcam"` when `XDG_CONFIG_HOME` and
      `APRILCAM_CONFIG_DIR` are both unset.
- [ ] `XDG_CONFIG_HOME=/tmp/xdgcfg` (non-root) yields `config_dir ==
      Path("/tmp/xdgcfg/aprilcam")`.
- [ ] `APRILCAM_CONFIG_DIR=/tmp/explicit` wins over both FHS and XDG
      defaults, for both root and non-root.
- [ ] `Config.load()` called with two different `start`/cwd values (no
      `APRILCAM_DATA_DIR`/`APRILCAM_CONFIG_DIR` set) returns an identical
      `config_dir` in both calls (proves launch-independence — SUC-001).
- [ ] `config_cameras_dir` returns `config_dir / "cameras"`;
      `config_playfields_dir` returns `config_dir / "playfields"`.
- [ ] `effective_config_dir` returns `config_dir` unchanged when
      `APRILCAM_CONFIG_DIR` was explicitly set, OR when `config_dir`
      already contains at least one `cameras/*/config.json`.
- [ ] `effective_config_dir` returns `data_dir` when `APRILCAM_CONFIG_DIR`
      is unset, `config_dir` has no config files, and `data_dir` has at
      least one `cameras/*/config.json` (legacy fallback case — SUC-003).
- [ ] `CONFIG_VARS` gains an `APRILCAM_CONFIG_DIR` entry with a non-empty
      default description and description string, following the existing
      entry format.
- [ ] No existing test regresses; no caller of `cameras_dir`/
      `playfields_dir` changes behavior (those properties are untouched).
- [ ] New tests use only tmp directories via env var overrides
      (`APRILCAM_DATA_DIR`, `APRILCAM_CONFIG_DIR`, `XDG_CONFIG_HOME`,
      `APRILCAM_SYSTEM`, monkeypatched `os.geteuid`) — never touch real
      `/etc` or `/var/lib`.

## Testing

- **Existing tests to run**: `tests/test_config_loader.py`,
  `tests/test_config_cli.py`, `tests/test_config_fhs_xdg.py` (Sprint 013's
  existing FHS/XDG test module — extend it, don't duplicate it), full
  `uv run pytest` for regressions.
- **New tests to write**:
  - `_default_dirs()` returns 4 elements; FHS branch 4th element is
    `/etc/aprilcam`; XDG branch 4th element respects `XDG_CONFIG_HOME` and
    falls back to `~/.config/aprilcam`.
  - `Config.load()` resolves `config_dir` from `APRILCAM_CONFIG_DIR` when
    set; from `_default_dirs()` otherwise, for both root
    (`monkeypatch.setattr(os, "geteuid", lambda: 0)`) and non-root cases.
  - Launch-independence: `Config.load(start=path_a)` and
    `Config.load(start=path_b)` (two different tmp dirs, no
    `APRILCAM_DATA_DIR`/`APRILCAM_CONFIG_DIR` set) yield identical
    `config_dir` and `data_dir`.
  - `config_cameras_dir`/`config_playfields_dir` property values.
  - `effective_config_dir`: explicit override case, config-dir-populated
    case, legacy-fallback case (populate a tmp `data_dir/cameras/x/
    config.json`, leave `config_dir` empty, confirm fallback), and the
    "neither populated" case (fresh install — should return `config_dir`,
    the correct target for a brand-new install, not `data_dir`).
  - `test_config_vars_coverage` (if it exists per architecture-013's
    design rationale) picks up the new `APRILCAM_CONFIG_DIR` entry
    automatically — verify it still passes.
- **Verification command**: `uv run pytest`
