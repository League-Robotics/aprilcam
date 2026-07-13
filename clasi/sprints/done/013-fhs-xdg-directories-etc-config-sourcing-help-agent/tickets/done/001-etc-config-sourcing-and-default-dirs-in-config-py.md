---
id: '001'
title: /etc config sourcing and _default_dirs() in config.py
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on: []
github-issue: ''
issue: plan-fhs-xdg-directory-layout-etc-config-sourcing-self-documenting-help-and-agent.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# /etc config sourcing and _default_dirs() in config.py

## Description

Lay the foundation for FHS/XDG directory support in `src/aprilcam/config.py`.
All changes are confined to this single file.

**Change A — `/etc` config sourcing:**
Insert two new lowest-priority sources at the very start of the `sources` dict
build in `Config.load()`, before the `~/.aprilcam` read. Use the existing
`_parse_dotfile()` helper (which already silences `OSError` for missing files):

```python
# 0a. System-wide lowest priority
for etc_path in (Path("/etc/aprilcam.env"), Path("/etc/aprilcam/aprilcam.env")):
    sources.update(_parse_dotfile(etc_path))
```

Updated precedence (highest wins):
`APRILCAM_* env > .env > .aprilcam > ~/.aprilcam > /etc/aprilcam/aprilcam.env > /etc/aprilcam.env`

Update the `Config` class docstring (lines 236-240) to list all six levels.

**Change B — `_default_dirs()` pure function:**
Add a module-level function just above the `Config` class:

```python
def _default_dirs() -> tuple[Path, Path, Path]:
    """Return (data_dir, socket_dir, log_dir) for FHS or XDG mode.

    FHS mode triggers when os.geteuid() == 0 OR APRILCAM_SYSTEM env var == '1'.
    XDG mode is used otherwise (APRILCAM_SYSTEM=0 forces XDG even for root).
    This function performs no I/O.
    """
    import os as _os
    system_env = _os.environ.get("APRILCAM_SYSTEM", "").strip()
    is_root = _os.geteuid() == 0
    use_fhs = (system_env == "1") or (is_root and system_env != "0")

    if use_fhs:
        return (
            Path("/var/lib/aprilcam"),
            Path("/run/aprilcam"),
            Path("/var/log/aprilcam"),
        )
    # XDG paths with fallbacks
    uid = _os.getuid()
    data = Path(_os.environ.get("XDG_DATA_HOME", "") or Path.home() / ".local/share") / "aprilcam"
    run  = Path(_os.environ.get("XDG_RUNTIME_DIR", "") or f"/run/user/{uid}") / "aprilcam"
    log  = Path(_os.environ.get("XDG_STATE_HOME", "") or Path.home() / ".local/state") / "aprilcam"
    return data, run, log
```

**Change C — wire `_default_dirs()` into `Config.load()`:**
Replace the hardcoded `Path("/tmp/aprilcam/")` and `Path("./data/aprilcam/")`
defaults in the `_path()` calls:

```python
_dd, _sd, _ld = _default_dirs()
data_dir   = _path("APRILCAM_DATA_DIR",   _dd)
socket_dir = _path("APRILCAM_SOCKET_DIR", _sd)
log_dir    = _path("APRILCAM_LOG_DIR",    _ld)
```

**Change D — `log_dir` field on `Config` dataclass:**
Add after `calibration_dir`:

```python
log_dir: Path = field(default_factory=lambda: Path("~/.local/state/aprilcam").expanduser())
```

Pass `log_dir=log_dir` to the `cls(...)` constructor call.

**Change E — directory creation at load with guarded permission errors:**
Replace the single `cfg.socket_dir.mkdir(...)` call with:

```python
_dir_labels = [
    (cfg.socket_dir, "RuntimeDirectory=aprilcam"),
    (cfg.data_dir,   "StateDirectory=aprilcam"),
    (cfg.log_dir,    "LogsDirectory=aprilcam"),
]
for _dir, _label in _dir_labels:
    try:
        _dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        import sys as _sys
        print(
            f"aprilcam: cannot create {_dir} (permission denied).\n"
            f"  For system installs, add to the systemd unit:\n"
            f"    {_label}",
            file=_sys.stderr,
        )
        raise
```

## Acceptance Criteria

- [x] `/etc/aprilcam.env` values appear in `Config.load()` when no
      higher-priority source overrides them.
- [x] `/etc/aprilcam/aprilcam.env` is also loaded (same precedence; later
      entry wins if both files define the same key).
- [x] Missing `/etc` files do not raise any error.
- [x] A key in `~/.aprilcam` overrides the same key from `/etc/aprilcam.env`.
- [x] `APRILCAM_*` process env overrides all dotfile sources including `/etc`.
- [x] `Config.load()` with `os.geteuid() == 0` returns
      `data_dir=Path("/var/lib/aprilcam")`, `socket_dir=Path("/run/aprilcam")`,
      `log_dir=Path("/var/log/aprilcam")`.
- [x] `Config.load()` with `os.geteuid() != 0` returns XDG-derived paths.
- [x] `$XDG_DATA_HOME`, `$XDG_RUNTIME_DIR`, `$XDG_STATE_HOME` are respected
      when set.
- [x] `APRILCAM_SYSTEM=0` forces XDG even when euid == 0.
- [x] `APRILCAM_SYSTEM=1` forces FHS even when euid != 0.
- [x] `Config` dataclass has a `log_dir: Path` field.
- [x] `APRILCAM_LOG_DIR` env var sets `log_dir` (overrides `_default_dirs()`).
- [x] Permission error on directory creation prints a systemd hint to stderr
      then re-raises.
- [x] `Config` docstring lists the new six-level precedence chain.
- [x] `_default_dirs()` is pure: no filesystem I/O, no `Config.load()` calls.

## Implementation Plan

### Approach

Single file: `src/aprilcam/config.py`. No other files changed in this ticket.
Changes are additive except replacing the two hardcoded defaults and the single
`mkdir` call.

### Files to modify

- `src/aprilcam/config.py`
  - Add `_default_dirs()` above the `Config` class (around line 232).
  - Add `log_dir: Path` field to the `Config` dataclass (after `calibration_dir`,
    before the static-deskew block, around line 259).
  - In `Config.load()` (~line 303): insert `/etc` parsing at the start of the
    sources-build block, before the `~/.aprilcam` read.
  - In `Config.load()`: call `_default_dirs()` and use results as defaults for
    `data_dir`, `socket_dir`, and the new `log_dir` `_path()` calls.
  - In `Config.load()`: add `log_dir=log_dir` to the `cls(...)` constructor.
  - Replace the single `cfg.socket_dir.mkdir(...)` with the guarded
    three-directory creation block.
  - Update the `Config` class docstring precedence list.

### Testing plan

Ticket 007 writes all new tests. After this ticket, run:
```
uv run pytest tests/test_config_loader.py -x
```
Note: `test_config_load_defaults` at line 74 will fail because it asserts
the old `./data/aprilcam` and `/tmp/aprilcam` defaults. That is expected;
Ticket 007 updates those assertions. All other existing tests should pass.

### Documentation updates

None in this ticket. `CONFIG_VARS` and `.env.example` are Tickets 003 and 006.
