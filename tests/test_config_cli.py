"""Tests for aprilcam.cli.config_cli — log_dir display and CONFIG_VARS reuse (013-005).

All tests monkeypatch directory paths to writable tmp locations so no
real APRILCAM_* environment or camera hardware is required.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aprilcam.cli import config_cli
from aprilcam.config import CONFIG_VARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _xdg_env(monkeypatch, tmp_path):
    """Set XDG dirs to writable tmp locations and force XDG (non-root) mode."""
    monkeypatch.setenv("APRILCAM_SYSTEM", "0")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))
    # Remove any APRILCAM_ overrides that would mask defaults
    for key in ("APRILCAM_DATA_DIR", "APRILCAM_SOCKET_DIR", "APRILCAM_LOG_DIR"):
        monkeypatch.delenv(key, raising=False)
    # Use a clean home so ~/.aprilcam isn't picked up
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# _collect() — log_dir included
# ---------------------------------------------------------------------------


def test_collect_includes_log_dir(tmp_path, monkeypatch):
    """_collect() must include log_dir as a string field."""
    _xdg_env(monkeypatch, tmp_path)
    from aprilcam.config import Config
    cfg = Config.load(start=tmp_path)
    result = config_cli._collect(cfg)
    assert "log_dir" in result
    assert isinstance(result["log_dir"], str)
    assert len(result["log_dir"]) > 0


def test_collect_log_dir_position(tmp_path, monkeypatch):
    """log_dir appears after playfields_dir and before socket_dir."""
    _xdg_env(monkeypatch, tmp_path)
    from aprilcam.config import Config
    cfg = Config.load(start=tmp_path)
    result = config_cli._collect(cfg)
    keys = list(result.keys())
    assert "playfields_dir" in keys
    assert "log_dir" in keys
    assert "socket_dir" in keys
    assert keys.index("playfields_dir") < keys.index("log_dir") < keys.index("socket_dir")


def test_collect_log_dir_resolves_to_xdg_state(tmp_path, monkeypatch):
    """log_dir resolves to the XDG state home aprilcam subdirectory."""
    _xdg_env(monkeypatch, tmp_path)
    from aprilcam.config import Config
    cfg = Config.load(start=tmp_path)
    result = config_cli._collect(cfg)
    assert "xdg_state" in result["log_dir"]
    assert "aprilcam" in result["log_dir"]


def test_collect_preserves_existing_fields(tmp_path, monkeypatch):
    """All previously existing fields are still present after adding log_dir."""
    _xdg_env(monkeypatch, tmp_path)
    from aprilcam.config import Config
    cfg = Config.load(start=tmp_path)
    result = config_cli._collect(cfg)
    expected_keys = {
        "version",
        "data_dir",
        "cameras_dir",
        "playfields_dir",
        "calibration_dir",
        "log_dir",
        "socket_dir",
        "daemon_pidfile",
        "env_dir",
        "log_level",
        "detection_fps",
        "static_deskew",
        "deskew_px_per_cm",
        "undistort",
        "movement_threshold_px",
    }
    assert expected_keys <= set(result.keys())


# ---------------------------------------------------------------------------
# main() — default table output includes log_dir
# ---------------------------------------------------------------------------


def test_main_table_output_includes_log_dir(tmp_path, monkeypatch, capsys):
    """aprilcam config (table mode) prints log_dir."""
    _xdg_env(monkeypatch, tmp_path)
    ret = config_cli.main([])
    assert ret == 0
    out = capsys.readouterr().out
    assert "log_dir" in out


def test_main_table_output_includes_data_dir(tmp_path, monkeypatch, capsys):
    """aprilcam config (table mode) still prints data_dir."""
    _xdg_env(monkeypatch, tmp_path)
    ret = config_cli.main([])
    assert ret == 0
    out = capsys.readouterr().out
    assert "data_dir" in out


def test_main_table_output_includes_socket_dir(tmp_path, monkeypatch, capsys):
    """aprilcam config (table mode) still prints socket_dir."""
    _xdg_env(monkeypatch, tmp_path)
    ret = config_cli.main([])
    assert ret == 0
    out = capsys.readouterr().out
    assert "socket_dir" in out


# ---------------------------------------------------------------------------
# main() --json — log_dir in JSON output
# ---------------------------------------------------------------------------


def test_main_json_includes_log_dir(tmp_path, monkeypatch, capsys):
    """aprilcam config --json includes log_dir in the JSON object."""
    _xdg_env(monkeypatch, tmp_path)
    ret = config_cli.main(["--json"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "log_dir" in data
    assert "xdg_state" in data["log_dir"]


def test_main_json_still_has_existing_keys(tmp_path, monkeypatch, capsys):
    """aprilcam config --json still contains all previously present keys."""
    _xdg_env(monkeypatch, tmp_path)
    ret = config_cli.main(["--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    for key in ("data_dir", "socket_dir", "daemon_pidfile", "log_level"):
        assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# main() --vars — CONFIG_VARS rendering
# ---------------------------------------------------------------------------


def test_main_vars_exits_zero(tmp_path, monkeypatch, capsys):
    """aprilcam config --vars exits with code 0."""
    monkeypatch.chdir(tmp_path)
    ret = config_cli.main(["--vars"])
    assert ret == 0


def test_main_vars_lists_all_config_var_keys(tmp_path, monkeypatch, capsys):
    """aprilcam config --vars prints every key from CONFIG_VARS."""
    monkeypatch.chdir(tmp_path)
    config_cli.main(["--vars"])
    out = capsys.readouterr().out
    for var in CONFIG_VARS:
        assert var["key"] in out, f"Missing var key in --vars output: {var['key']}"


def test_main_vars_includes_descriptions(tmp_path, monkeypatch, capsys):
    """aprilcam config --vars includes the description for each variable."""
    monkeypatch.chdir(tmp_path)
    config_cli.main(["--vars"])
    out = capsys.readouterr().out
    for var in CONFIG_VARS:
        assert var["description"] in out, (
            f"Missing description for {var['key']}: {var['description']!r}"
        )


def test_main_vars_includes_defaults(tmp_path, monkeypatch, capsys):
    """aprilcam config --vars includes the default value for each variable."""
    monkeypatch.chdir(tmp_path)
    config_cli.main(["--vars"])
    out = capsys.readouterr().out
    for var in CONFIG_VARS:
        assert var["default"] in out, (
            f"Missing default for {var['key']}: {var['default']!r}"
        )


def test_main_vars_does_not_load_config(tmp_path, monkeypatch, capsys):
    """--vars must not call Config.load() (it exits before touching the filesystem)."""
    monkeypatch.chdir(tmp_path)
    load_called = []

    from aprilcam import config as _cfg_mod
    original_load = _cfg_mod.Config.load

    def mock_load(*args, **kwargs):
        load_called.append(True)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(_cfg_mod.Config, "load", mock_load)
    config_cli.main(["--vars"])
    assert not load_called, "Config.load() should not be called when --vars is passed"


def test_main_vars_line_count_matches_config_vars(tmp_path, monkeypatch, capsys):
    """--vars prints exactly len(CONFIG_VARS) non-empty lines."""
    monkeypatch.chdir(tmp_path)
    config_cli.main(["--vars"])
    out = capsys.readouterr().out
    non_empty = [line for line in out.splitlines() if line.strip()]
    assert len(non_empty) == len(CONFIG_VARS)


# ---------------------------------------------------------------------------
# No duplication — config_cli.py imports from CONFIG_VARS, not hard-coded list
# ---------------------------------------------------------------------------


def test_config_vars_imported_from_config(tmp_path, monkeypatch, capsys):
    """The --vars output matches CONFIG_VARS from config.py (no parallel list)."""
    monkeypatch.chdir(tmp_path)
    config_cli.main(["--vars"])
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    # Each line must contain the key from CONFIG_VARS (in order)
    for i, var in enumerate(CONFIG_VARS):
        assert var["key"] in lines[i], (
            f"Line {i} expected key {var['key']!r}, got: {lines[i]!r}"
        )


# ---------------------------------------------------------------------------
# `aprilcam config --help` carries the APRILCAM_* variable table
# (moved here from the main `aprilcam --help`)
# ---------------------------------------------------------------------------


def test_config_help_lists_env_vars(capsys):
    """`aprilcam config --help` shows the APRILCAM_* variable table in its epilog."""
    with pytest.raises(SystemExit):
        config_cli.main(["--help"])
    out = capsys.readouterr().out
    assert "environment variables" in out
    for var in CONFIG_VARS:
        assert var["key"] in out
