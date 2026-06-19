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
    # Ensure dirs are writable
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))
    # Use a fake home so ~/.aprilcam is not picked up
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

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
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

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
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))

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
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    cfg = Config.load(start=tmp_path)
    assert hasattr(cfg, "log_dir")
    assert isinstance(cfg.log_dir, Path)


def test_aprilcam_log_dir_env_sets_log_dir(tmp_path, monkeypatch):
    log_path = tmp_path / "mylogs"
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(log_path))
    monkeypatch.setenv("APRILCAM_SOCKET_DIR", str(tmp_path / "sock"))
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
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
