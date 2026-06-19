---
id: "007"
title: "Tests for FHS/XDG defaults, /etc precedence, log_dir, CONFIG_VARS coverage, and version bump"
status: open
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
  - SUC-005
depends-on:
  - "001"
  - "002"
  - "003"
  - "004"
  - "005"
  - "006"
github-issue: ""
issue: ""
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Tests for FHS/XDG defaults, /etc precedence, log_dir, CONFIG_VARS coverage, and version bump

## Description

Write all new tests for this sprint and update existing tests broken by the
default-path changes in Ticket 001. Run the full suite to confirm no
regressions. Then bump the version.

**Part A — Update `tests/test_config_loader.py`:**

`test_config_load_defaults` (line 74) asserts the old cwd-relative and `/tmp`
defaults. Replace these assertions with ones for XDG paths (monkeypatching
`os.geteuid` to return a non-zero uid and unsetting XDG env vars to get the
`~/.local/share/aprilcam` etc. fallbacks). Keep the test structure the same.

**Part B — New file `tests/test_config_fhs_xdg.py`:**

```python
"""Tests for FHS/XDG directory selection and /etc config sourcing (Sprint 013)."""
import os
from pathlib import Path
import pytest
from aprilcam.config import Config, _default_dirs


# --- _default_dirs() ---

def test_default_dirs_fhs_when_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    data, sock, log = _default_dirs()
    assert data == Path("/var/lib/aprilcam")
    assert sock == Path("/run/aprilcam")
    assert log == Path("/var/log/aprilcam")


def test_default_dirs_xdg_when_non_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    data, sock, log = _default_dirs()
    assert "local/share/aprilcam" in str(data)
    assert "run/user/1000/aprilcam" in str(sock)
    assert "local/state/aprilcam" in str(log)


def test_default_dirs_xdg_honours_env_vars(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/run")
    monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")
    data, sock, log = _default_dirs()
    assert data == Path("/custom/data/aprilcam")
    assert sock == Path("/custom/run/aprilcam")
    assert log == Path("/custom/state/aprilcam")


def test_aprilcam_system_1_forces_fhs_for_non_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setenv("APRILCAM_SYSTEM", "1")
    data, sock, log = _default_dirs()
    assert data == Path("/var/lib/aprilcam")


def test_aprilcam_system_0_forces_xdg_for_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setenv("APRILCAM_SYSTEM", "0")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    data, _, _ = _default_dirs()
    assert "/var/lib" not in str(data)


# --- /etc config sourcing ---

def test_etc_aprilcam_env_loaded_at_lowest_priority(tmp_path, monkeypatch):
    """A value in /etc/aprilcam.env is used when no higher source overrides it."""
    # Monkeypatch _parse_dotfile to intercept the /etc paths
    from aprilcam import config as config_mod
    original_parse = config_mod._parse_dotfile

    def fake_parse(path):
        if str(path) == "/etc/aprilcam.env":
            return {"APRILCAM_LOG_LEVEL": "WARNING"}
        return original_parse(path)

    monkeypatch.setattr(config_mod, "_parse_dotfile", fake_parse)
    monkeypatch.delenv("APRILCAM_LOG_LEVEL", raising=False)

    cfg = Config.load(start=tmp_path)
    assert cfg.log_level == "WARNING"


def test_user_dotfile_overrides_etc(tmp_path, monkeypatch):
    """~/.aprilcam overrides /etc/aprilcam.env."""
    from aprilcam import config as config_mod
    original_parse = config_mod._parse_dotfile

    def fake_parse(path):
        if str(path) == "/etc/aprilcam.env":
            return {"APRILCAM_LOG_LEVEL": "WARNING"}
        return original_parse(path)

    monkeypatch.setattr(config_mod, "_parse_dotfile", fake_parse)
    monkeypatch.delenv("APRILCAM_LOG_LEVEL", raising=False)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".aprilcam").write_text("APRILCAM_LOG_LEVEL=DEBUG\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    cfg = Config.load(start=tmp_path)
    assert cfg.log_level == "DEBUG"


def test_env_var_overrides_etc(tmp_path, monkeypatch):
    """Process env overrides /etc value."""
    from aprilcam import config as config_mod
    original_parse = config_mod._parse_dotfile

    def fake_parse(path):
        if str(path) == "/etc/aprilcam.env":
            return {"APRILCAM_LOG_LEVEL": "WARNING"}
        return original_parse(path)

    monkeypatch.setattr(config_mod, "_parse_dotfile", fake_parse)
    monkeypatch.setenv("APRILCAM_LOG_LEVEL", "ERROR")

    cfg = Config.load(start=tmp_path)
    assert cfg.log_level == "ERROR"


# --- log_dir ---

def test_config_has_log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    for k in list(os.environ):
        if k.startswith("APRILCAM_"):
            monkeypatch.delenv(k, raising=False)
    cfg = Config.load(start=tmp_path)
    assert hasattr(cfg, "log_dir")
    assert isinstance(cfg.log_dir, Path)


def test_aprilcam_log_dir_env_sets_log_dir(tmp_path, monkeypatch):
    log_path = tmp_path / "mylogs"
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(log_path))
    cfg = Config.load(start=tmp_path)
    assert cfg.log_dir == log_path


# --- CONFIG_VARS coverage ---

def test_config_vars_coverage():
    """Every APRILCAM_* variable handled in Config.load() has a CONFIG_VARS entry."""
    from aprilcam.config import CONFIG_VARS
    documented_keys = {v["key"] for v in CONFIG_VARS}
    expected_keys = {
        "APRILCAM_DATA_DIR",
        "APRILCAM_SOCKET_DIR",
        "APRILCAM_LOG_DIR",
        "APRILCAM_LOG_LEVEL",
        "APRILCAM_DAEMON_PIDFILE",
        "APRILCAM_DETECTION_FPS",
        "APRILCAM_STATIC_DESKEW",
        "APRILCAM_DESKEW_PX_PER_CM",
        "APRILCAM_UNDISTORT",
        "APRILCAM_MOVEMENT_THRESHOLD_PX",
        "APRILCAM_SYSTEM",
    }
    missing = expected_keys - documented_keys
    assert not missing, f"CONFIG_VARS missing entries for: {missing}"


def test_config_vars_have_required_fields():
    from aprilcam.config import CONFIG_VARS
    for entry in CONFIG_VARS:
        assert "key" in entry, f"Missing 'key' in {entry}"
        assert "default" in entry, f"Missing 'default' in {entry}"
        assert "description" in entry, f"Missing 'description' in {entry}"
        assert entry["description"].strip(), f"Empty description for {entry['key']}"
```

**Part C — CLI smoke tests (add to `tests/test_cli_dispatch.py` or a new
`tests/test_cli_agent.py`):**

```python
import subprocess, sys

def test_agent_flag_prints_content():
    result = subprocess.run(
        [sys.executable, "-m", "aprilcam", "--agent"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert len(result.stdout) > 100  # non-trivial content

def test_agent_robot_flag():
    result = subprocess.run(
        [sys.executable, "-m", "aprilcam", "--agent", "robot"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert len(result.stdout) > 100

def test_agent_unknown_exits_nonzero():
    result = subprocess.run(
        [sys.executable, "-m", "aprilcam", "--agent", "nosuchguide"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
```

**Part D — Version bump:**

After all tests pass, run:
```
dotconfig version bump
```
Then commit the version change with message:
```
chore: bump version
```

## Acceptance Criteria

- [ ] `tests/test_config_fhs_xdg.py` exists and all tests pass.
- [ ] `tests/test_config_loader.py::test_config_load_defaults` is updated
      to reflect XDG defaults and passes.
- [ ] CLI `--agent` smoke tests pass.
- [ ] `uv run pytest` (full suite) exits 0 with no errors or failures.
- [ ] Version bumped in `pyproject.toml` (format `0.YYYYMMDD.N`).
- [ ] Version bump committed as `chore: bump version`.

## Implementation Plan

### Approach

Tests only (plus the version bump). No production code changes.

Suggested order:
1. Update `test_config_load_defaults` in `tests/test_config_loader.py`.
2. Create `tests/test_config_fhs_xdg.py`.
3. Add CLI smoke tests.
4. Run `uv run pytest` — fix any failures.
5. `dotconfig version bump` + commit.

### Files to create/modify

- `tests/test_config_loader.py` — update `test_config_load_defaults` and
  `test_socket_dir_created_if_missing` (which may also need updating if the
  default socket_dir assertion changes).
- `tests/test_config_fhs_xdg.py` — new file.
- `tests/test_cli_dispatch.py` or `tests/test_cli_agent.py` — add
  `--agent` smoke tests.
- `pyproject.toml` — version bump (via `dotconfig version bump`).

### Testing plan

This ticket IS the testing. Run `uv run pytest -v` as final verification.

### Documentation updates

None.
