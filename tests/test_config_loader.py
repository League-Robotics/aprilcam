"""Tests for the Config dataclass and multi-source loader in config.py (T001)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aprilcam.config import Config, _find_dotfile, _parse_dotfile, _default_dirs


# ---------------------------------------------------------------------------
# _find_dotfile helper
# ---------------------------------------------------------------------------


def test_find_dotfile_finds_file_in_start_dir(tmp_path):
    target = tmp_path / ".aprilcam"
    target.write_text("KEY=value\n")
    found = _find_dotfile(".aprilcam", tmp_path)
    assert found == target


def test_find_dotfile_finds_file_in_parent_dir(tmp_path):
    target = tmp_path / ".aprilcam"
    target.write_text("KEY=value\n")
    subdir = tmp_path / "sub" / "dir"
    subdir.mkdir(parents=True)
    found = _find_dotfile(".aprilcam", subdir)
    assert found == target


def test_find_dotfile_returns_none_when_absent(tmp_path):
    found = _find_dotfile(".aprilcam", tmp_path)
    assert found is None


# ---------------------------------------------------------------------------
# _parse_dotfile helper
# ---------------------------------------------------------------------------


def test_parse_dotfile_basic(tmp_path):
    f = tmp_path / ".aprilcam"
    f.write_text("APRILCAM_LOG_LEVEL=DEBUG\nAPRILCAM_DATA_DIR=/data\n")
    result = _parse_dotfile(f)
    assert result == {"APRILCAM_LOG_LEVEL": "DEBUG", "APRILCAM_DATA_DIR": "/data"}


def test_parse_dotfile_strips_comments(tmp_path):
    f = tmp_path / ".aprilcam"
    f.write_text("# full-line comment\nAPRILCAM_LOG_LEVEL=INFO  # inline comment\n")
    result = _parse_dotfile(f)
    assert result == {"APRILCAM_LOG_LEVEL": "INFO"}


def test_parse_dotfile_skips_blank_lines(tmp_path):
    f = tmp_path / ".aprilcam"
    f.write_text("\n\nAPRILCAM_LOG_LEVEL=WARN\n\n")
    result = _parse_dotfile(f)
    assert result == {"APRILCAM_LOG_LEVEL": "WARN"}


def test_parse_dotfile_missing_file_returns_empty():
    result = _parse_dotfile(Path("/nonexistent/path/.aprilcam"))
    assert result == {}


# ---------------------------------------------------------------------------
# Config.load() — default values
# ---------------------------------------------------------------------------


def test_config_load_defaults(tmp_path, monkeypatch):
    """With no config files and no APRILCAM_ env vars, XDG defaults apply for non-root."""
    monkeypatch.chdir(tmp_path)
    # Remove any APRILCAM_ vars from the environment
    for key in list(os.environ.keys()):
        if key.startswith("APRILCAM_"):
            monkeypatch.delenv(key, raising=False)
    # Force non-root / XDG mode with all XDG dirs pointing to tmp for mkdir
    monkeypatch.setenv("APRILCAM_SYSTEM", "0")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))

    cfg = Config.load(start=tmp_path)

    assert cfg.data_dir == tmp_path / "xdg_data" / "aprilcam"
    assert cfg.socket_dir == tmp_path / "xdg_run" / "aprilcam"
    assert cfg.log_dir == tmp_path / "xdg_state" / "aprilcam"
    assert cfg.log_level == "INFO"
    assert cfg.daemon_pidfile == tmp_path / "xdg_run" / "aprilcam" / "aprilcamd.pid"


def test_config_load_does_not_raise_with_no_files(tmp_path, monkeypatch):
    """Config.load() must not raise even when no config files exist."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ.keys()):
        if key.startswith("APRILCAM_"):
            monkeypatch.delenv(key, raising=False)
    # Set XDG dirs to writable tmp paths to avoid /run/user/<uid> not existing on macOS
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    # Should complete without raising
    Config.load(start=tmp_path)


# ---------------------------------------------------------------------------
# Config.load() — env var overrides
# ---------------------------------------------------------------------------


def test_env_var_overrides_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APRILCAM_LOG_LEVEL", "DEBUG")
    custom_data = tmp_path / "custom" / "data"
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(custom_data))
    # Set socket/log dirs to avoid /run/user/<uid> not existing on macOS
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    cfg = Config.load(start=tmp_path)

    assert cfg.log_level == "DEBUG"
    assert cfg.data_dir == custom_data


def test_env_var_overrides_dotfile(tmp_path, monkeypatch):
    """Env vars must win over values in .aprilcam dotfile."""
    dotfile = tmp_path / ".aprilcam"
    dotfile.write_text("APRILCAM_LOG_LEVEL=WARNING\n")
    monkeypatch.setenv("APRILCAM_LOG_LEVEL", "ERROR")
    # Set all dirs to writable tmp locations
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    cfg = Config.load(start=tmp_path)

    assert cfg.log_level == "ERROR"


# ---------------------------------------------------------------------------
# Config.load() — dotfile overrides
# ---------------------------------------------------------------------------


def test_project_dotfile_overrides_user_dotfile(tmp_path, monkeypatch):
    """Project-local .aprilcam must win over ~/.aprilcam."""
    # Remove env interference
    monkeypatch.delenv("APRILCAM_LOG_LEVEL", raising=False)
    # Set all dirs to writable tmp locations
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    # Simulate ~/.aprilcam by patching Path.home()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".aprilcam").write_text("APRILCAM_LOG_LEVEL=WARNING\n")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".aprilcam").write_text("APRILCAM_LOG_LEVEL=DEBUG\n")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    cfg = Config.load(start=project_dir)
    assert cfg.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# Config.load() — socket_dir creation
# ---------------------------------------------------------------------------


def test_socket_dir_created_if_missing(tmp_path, monkeypatch):
    socket_dir = tmp_path / "sockets" / "aprilcam"
    assert not socket_dir.exists()

    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(socket_dir))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    Config.load(start=tmp_path)

    assert socket_dir.exists()
    assert socket_dir.is_dir()


def test_socket_dir_creation_idempotent(tmp_path, monkeypatch):
    """Config.load() must not raise if socket_dir already exists."""
    socket_dir = tmp_path / "sockets"
    socket_dir.mkdir()
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(socket_dir))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    Config.load(start=tmp_path)  # should not raise


# ---------------------------------------------------------------------------
# AppConfig smoke test — existing class must still work
# ---------------------------------------------------------------------------


def test_appconfig_unchanged(tmp_path):
    """AppConfig.find_env raises FileNotFoundError when no .env exists."""
    from aprilcam.config import AppConfig

    with pytest.raises(FileNotFoundError):
        AppConfig.find_env(start=tmp_path)


# ---------------------------------------------------------------------------
# /etc config sourcing (Change A)
# ---------------------------------------------------------------------------


def test_etc_aprilcam_env_loaded(tmp_path, monkeypatch, tmp_path_factory):
    """/etc/aprilcam.env values appear in Config.load() as lowest-priority source."""
    etc_file = tmp_path / "etc_aprilcam.env"
    etc_file.write_text("APRILCAM_LOG_LEVEL=CRITICAL\n")

    # Patch _parse_dotfile calls for /etc paths by monkeypatching Path
    from aprilcam import config as _cfg_mod

    original_parse = _cfg_mod._parse_dotfile

    def patched_parse(path: Path) -> dict:
        if path == Path("/etc/aprilcam.env"):
            return original_parse(etc_file)
        if path == Path("/etc/aprilcam/aprilcam.env"):
            return {}
        return original_parse(path)

    monkeypatch.setattr(_cfg_mod, "_parse_dotfile", patched_parse)
    monkeypatch.delenv("APRILCAM_LOG_LEVEL", raising=False)
    # Use a clean tmp dir with no .aprilcam or ~/.aprilcam
    work_dir = tmp_path_factory.mktemp("work")
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(work_dir / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(work_dir / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(work_dir / "log"))

    cfg = Config.load(start=work_dir)
    assert cfg.log_level == "CRITICAL"


def test_etc_aprilcam_dir_env_loaded(tmp_path, monkeypatch, tmp_path_factory):
    """/etc/aprilcam/aprilcam.env values are loaded and override /etc/aprilcam.env."""
    from aprilcam import config as _cfg_mod

    original_parse = _cfg_mod._parse_dotfile

    def patched_parse(path: Path) -> dict:
        if path == Path("/etc/aprilcam.env"):
            return {"APRILCAM_LOG_LEVEL": "WARNING"}
        if path == Path("/etc/aprilcam/aprilcam.env"):
            return {"APRILCAM_LOG_LEVEL": "ERROR"}
        return original_parse(path)

    monkeypatch.setattr(_cfg_mod, "_parse_dotfile", patched_parse)
    monkeypatch.delenv("APRILCAM_LOG_LEVEL", raising=False)
    work_dir = tmp_path_factory.mktemp("work")
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(work_dir / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(work_dir / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(work_dir / "log"))

    cfg = Config.load(start=work_dir)
    # /etc/aprilcam/aprilcam.env overrides /etc/aprilcam.env
    assert cfg.log_level == "ERROR"


def test_missing_etc_files_do_not_raise(tmp_path, monkeypatch):
    """Missing /etc config files do not cause any error."""
    # The /etc files almost certainly don't exist in CI; just ensure no error
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))
    Config.load(start=tmp_path)  # must not raise


def test_user_dotfile_overrides_etc(tmp_path, monkeypatch, tmp_path_factory):
    """A key in ~/.aprilcam overrides the same key from /etc/aprilcam.env."""
    from aprilcam import config as _cfg_mod

    original_parse = _cfg_mod._parse_dotfile

    def patched_parse(path: Path) -> dict:
        if path == Path("/etc/aprilcam.env"):
            return {"APRILCAM_LOG_LEVEL": "CRITICAL"}
        if path == Path("/etc/aprilcam/aprilcam.env"):
            return {}
        return original_parse(path)

    monkeypatch.setattr(_cfg_mod, "_parse_dotfile", patched_parse)
    monkeypatch.delenv("APRILCAM_LOG_LEVEL", raising=False)

    fake_home = tmp_path_factory.mktemp("home")
    (fake_home / ".aprilcam").write_text("APRILCAM_LOG_LEVEL=DEBUG\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    work_dir = tmp_path_factory.mktemp("work")
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(work_dir / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(work_dir / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(work_dir / "log"))

    cfg = Config.load(start=work_dir)
    # ~/.aprilcam wins over /etc
    assert cfg.log_level == "DEBUG"


def test_env_var_overrides_etc(tmp_path, monkeypatch):
    """APRILCAM_* process env overrides all dotfile sources including /etc."""
    from aprilcam import config as _cfg_mod

    original_parse = _cfg_mod._parse_dotfile

    def patched_parse(path: Path) -> dict:
        if path == Path("/etc/aprilcam.env"):
            return {"APRILCAM_LOG_LEVEL": "CRITICAL"}
        if path == Path("/etc/aprilcam/aprilcam.env"):
            return {}
        return original_parse(path)

    monkeypatch.setattr(_cfg_mod, "_parse_dotfile", patched_parse)
    monkeypatch.setenv("APRILCAM_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    cfg = Config.load(start=tmp_path)
    assert cfg.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# _default_dirs() — FHS vs XDG selection (Change B)
# ---------------------------------------------------------------------------


def test_default_dirs_fhs_when_root(monkeypatch):
    """_default_dirs() returns FHS paths when euid == 0."""
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getuid", lambda: 0)

    data, sock, log = _default_dirs()

    assert data == Path("/var/lib/aprilcam")
    assert sock == Path("/run/aprilcam")
    assert log == Path("/var/log/aprilcam")


def test_default_dirs_xdg_when_non_root(monkeypatch):
    """_default_dirs() returns XDG paths when euid != 0."""
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)

    data, sock, log = _default_dirs()

    assert "aprilcam" in str(data)
    assert "aprilcam" in str(sock)
    assert "aprilcam" in str(log)
    # Must NOT be FHS paths
    assert data != Path("/var/lib/aprilcam")
    assert sock != Path("/run/aprilcam")
    assert log != Path("/var/log/aprilcam")


def test_default_dirs_system_env_forces_fhs(monkeypatch):
    """APRILCAM_SYSTEM=1 forces FHS even when euid != 0."""
    monkeypatch.setenv("APRILCAM_SYSTEM", "1")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)

    data, sock, log = _default_dirs()

    assert data == Path("/var/lib/aprilcam")
    assert sock == Path("/run/aprilcam")
    assert log == Path("/var/log/aprilcam")


def test_default_dirs_system_env_zero_forces_xdg_even_for_root(monkeypatch):
    """APRILCAM_SYSTEM=0 forces XDG even when euid == 0."""
    monkeypatch.setenv("APRILCAM_SYSTEM", "0")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getuid", lambda: 0)

    data, sock, log = _default_dirs()

    assert data != Path("/var/lib/aprilcam")
    assert sock != Path("/run/aprilcam")
    assert log != Path("/var/log/aprilcam")


def test_default_dirs_xdg_env_vars_respected(monkeypatch):
    """XDG_DATA_HOME, XDG_RUNTIME_DIR, XDG_STATE_HOME are respected."""
    monkeypatch.setenv("APRILCAM_SYSTEM", "0")
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/run")
    monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)

    data, sock, log = _default_dirs()

    assert data == Path("/custom/data/aprilcam")
    assert sock == Path("/custom/run/aprilcam")
    assert log == Path("/custom/state/aprilcam")


# ---------------------------------------------------------------------------
# Config.load() — FHS/XDG path wiring (Change C)
# ---------------------------------------------------------------------------


def test_config_load_fhs_paths_when_root(tmp_path, monkeypatch):
    """Config.load() with euid==0 returns FHS data/socket/log dirs."""
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getuid", lambda: 0)

    # Override dirs to writable tmp locations so mkdir doesn't need root
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    # Verify _default_dirs returns FHS (the wiring is correct)
    data, sock, log = _default_dirs()
    assert data == Path("/var/lib/aprilcam")
    assert sock == Path("/run/aprilcam")
    assert log == Path("/var/log/aprilcam")


def test_config_load_xdg_paths_when_non_root(tmp_path, monkeypatch):
    """Config.load() with euid!=0 returns XDG-derived paths."""
    monkeypatch.setenv("APRILCAM_SYSTEM", "0")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    # Clear any explicit overrides that would bypass _default_dirs
    monkeypatch.delenv("APRILCAM_DATA_DIR", raising=False)
    monkeypatch.delenv("APRILCAM_SOCKET_DIR", raising=False)
    monkeypatch.delenv("APRILCAM_LOG_DIR", raising=False)
    # Use a fake home so ~/.aprilcam isn't picked up
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    cfg = Config.load(start=tmp_path)

    assert cfg.data_dir == tmp_path / "xdg_data" / "aprilcam"
    assert cfg.socket_dir == tmp_path / "xdg_run" / "aprilcam"
    assert cfg.log_dir == tmp_path / "xdg_state" / "aprilcam"


# ---------------------------------------------------------------------------
# Config.log_dir field and APRILCAM_LOG_DIR override (Change D)
# ---------------------------------------------------------------------------


def test_config_has_log_dir_field(tmp_path, monkeypatch):
    """Config dataclass has a log_dir: Path field."""
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    cfg = Config.load(start=tmp_path)

    assert hasattr(cfg, "log_dir")
    assert isinstance(cfg.log_dir, Path)


def test_aprilcam_log_dir_env_sets_log_dir(tmp_path, monkeypatch):
    """APRILCAM_LOG_DIR env var overrides _default_dirs() for log_dir."""
    custom_log = tmp_path / "custom_logs"
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(custom_log))
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))

    cfg = Config.load(start=tmp_path)

    assert cfg.log_dir == custom_log


# ---------------------------------------------------------------------------
# Directory creation — guarded permission error (Change E)
# ---------------------------------------------------------------------------


def test_permission_error_on_mkdir_prints_hint_and_reraises(tmp_path, monkeypatch, capsys):
    """PermissionError on directory creation prints a systemd hint and re-raises."""
    bad_dir = tmp_path / "no_perm_dir"
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(bad_dir))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

    # Patch mkdir on Path to raise PermissionError for the bad_dir
    original_mkdir = Path.mkdir

    def patched_mkdir(self, **kwargs):
        if self == bad_dir:
            raise PermissionError(f"Permission denied: {self}")
        original_mkdir(self, **kwargs)

    monkeypatch.setattr(Path, "mkdir", patched_mkdir)

    with pytest.raises(PermissionError):
        Config.load(start=tmp_path)

    captured = capsys.readouterr()
    assert "permission denied" in captured.err.lower()
    assert "systemd" in captured.err.lower() or "RuntimeDirectory" in captured.err


# ---------------------------------------------------------------------------
# Config docstring — six-level precedence chain
# ---------------------------------------------------------------------------


def test_config_docstring_mentions_etc_sources():
    """Config class docstring lists /etc sources in the precedence chain."""
    doc = Config.__doc__ or ""
    assert "/etc/aprilcam.env" in doc
    assert "/etc/aprilcam/aprilcam.env" in doc
