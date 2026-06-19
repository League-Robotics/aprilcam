"""Tests for FHS/XDG directory selection and /etc config sourcing (Sprint 013)."""
import os
import tempfile
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


def test_default_dirs_xdg_when_non_root_linux(monkeypatch):
    """On Linux where /run/user/<uid> exists, the XDG runtime dir is used."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    # Simulate Linux: /run/user/1000 exists — intercept only that path.
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir", lambda self: str(self) == "/run/user/1000" or real_is_dir(self)
    )
    data, sock, log = _default_dirs()
    assert "local/share/aprilcam" in str(data)
    assert "run/user/1000/aprilcam" in str(sock)
    assert "local/state/aprilcam" in str(log)


def test_default_dirs_xdg_when_non_root(monkeypatch):
    """XDG runtime dir is under /run/user/<uid> when that dir exists (Linux)."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    # Use XDG_RUNTIME_DIR explicitly so we get the expected path on any OS.
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
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


# ---------------------------------------------------------------------------
# Regression: macOS / headless — XDG_RUNTIME_DIR unset, /run/user/<uid> absent
# Defect A: on macOS (and any system without systemd-logind), Config.load()
# used to crash with "[Errno 30] Read-only file system: '/run'" because it
# tried to create /run/user/<uid>/aprilcam unconditionally.
# ---------------------------------------------------------------------------


def test_default_dirs_macos_fallback_when_run_user_absent(monkeypatch, tmp_path):
    """When XDG_RUNTIME_DIR is unset and /run/user/<uid> does not exist,
    _default_dirs() must return a writable temp-based path instead of /run/...

    This simulates macOS (or any headless Linux without systemd-logind).
    The test explicitly removes XDG_RUNTIME_DIR and makes Path.is_dir()
    return False for /run/user/<uid> so the conftest fixture cannot hide
    the defect.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 1234)
    monkeypatch.setattr(os, "getuid", lambda: 1234)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    # Simulate absence of /run/user/1234 (macOS, bare Linux, headless Pi, …).
    # Only intercept the specific path, leave the rest of the filesystem intact.
    real_is_dir = Path.is_dir

    def _fake_is_dir(self: Path) -> bool:
        if str(self) == "/run/user/1234":
            return False
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _fake_is_dir)

    _, sock, _ = _default_dirs()

    # Must NOT be under /run
    assert not str(sock).startswith("/run"), (
        f"Expected a temp-dir fallback, got: {sock}"
    )
    # Must include the uid so multiple users don't collide.
    assert "1234" in str(sock), f"Expected uid in runtime dir path, got: {sock}"
    # Must be the temp dir
    assert str(sock).startswith(tempfile.gettempdir()), (
        f"Expected path under tempdir ({tempfile.gettempdir()}), got: {sock}"
    )


def test_config_load_does_not_crash_when_run_user_absent(monkeypatch, tmp_path):
    """Config.load() must not crash on macOS / systems where /run/user/<uid>
    is absent and XDG_RUNTIME_DIR is not set (regression for Defect A).

    The conftest _safe_xdg_runtime_dir fixture is intentionally bypassed
    here via an explicit monkeypatch.delenv() so this test actually exercises
    the fallback path.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 5678)
    monkeypatch.setattr(os, "getuid", lambda: 5678)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("APRILCAM_SYSTEM", raising=False)
    # Redirect APRILCAM_DATA_DIR / APRILCAM_LOG_DIR to tmp_path so we don't
    # actually write to ~/.local during the test.
    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APRILCAM_LOG_DIR", str(tmp_path / "log"))
    # Simulate macOS: /run/user/5678 does not exist — only intercept that
    # specific path, so the rest of the filesystem behaves normally.
    real_is_dir = Path.is_dir

    def _fake_is_dir(self: Path) -> bool:
        if str(self) == "/run/user/5678":
            return False
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _fake_is_dir)

    # Must not raise.
    cfg = Config.load(start=tmp_path)

    # socket_dir must be writable (under temp, not /run).
    assert not str(cfg.socket_dir).startswith("/run"), (
        f"socket_dir must not be under /run on macOS, got: {cfg.socket_dir}"
    )
    assert cfg.socket_dir.exists(), f"socket_dir must be created: {cfg.socket_dir}"
