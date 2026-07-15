"""Tests for ticket 019-005 — reconciling the systemd unit and repo .env with
the config/state FHS split.

Plain file-content assertions only; no real /etc, /var/lib, or systemd
interaction. These guard against the hardcoded, launch-location-dependent
overrides (`APRILCAM_DATA_DIR=/home/eric/aprilcam-data` in the unit,
`APRILCAM_DATA_DIR=./data/aprilcam` in the repo .env) regressing back in.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    path = _REPO_ROOT / relative_path
    assert path.is_file(), f"expected {path} to exist"
    return path.read_text()


def test_systemd_unit_has_no_hardcoded_data_dir_override():
    """The unit must not pin the old non-FHS /home/eric/aprilcam-data path."""
    unit = _read("deploy/aprilcamd.service")
    assert "Environment=APRILCAM_DATA_DIR=/home/eric/aprilcam-data" not in unit
    assert "/home/eric/aprilcam-data" not in unit


def test_systemd_unit_sets_aprilcam_system_1():
    """User=eric is a static non-root user (no DynamicUser=), so the unit
    must force FHS selection explicitly — Config._default_dirs() only
    auto-selects FHS when euid == 0 or APRILCAM_SYSTEM == '1'."""
    unit = _read("deploy/aprilcamd.service")
    assert "Environment=APRILCAM_SYSTEM=1" in unit


def test_systemd_unit_still_declares_configuration_and_state_directory():
    """ConfigurationDirectory=/StateDirectory= provision the FHS dirs the
    daemon now resolves automatically; must not be removed."""
    unit = _read("deploy/aprilcamd.service")
    assert "ConfigurationDirectory=aprilcam" in unit
    assert "StateDirectory=aprilcam" in unit


def test_systemd_unit_user_is_non_root_no_dynamic_user():
    """Sanity check on the premise behind the APRILCAM_SYSTEM=1 requirement:
    the unit runs as a static, non-root user without DynamicUser=."""
    unit = _read("deploy/aprilcamd.service")
    assert "User=eric" in unit
    assert "DynamicUser=" not in unit


def test_repo_env_has_no_active_data_dir_cwd_override():
    """A fresh Mac dev checkout must not pin a CWD-relative APRILCAM_DATA_DIR
    — that's exactly the launch-location-dependent override this sprint
    removes so the daemon falls through to XDG defaults."""
    env_text = _read(".env")
    for line in env_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("APRILCAM_DATA_DIR="), (
            f"found active APRILCAM_DATA_DIR override: {line!r}"
        )
    assert "APRILCAM_DATA_DIR=./data/aprilcam" not in env_text


def test_env_example_documents_aprilcam_config_dir():
    """.env.example should mention the new APRILCAM_CONFIG_DIR variable for
    developer discoverability, matching the existing APRILCAM_DATA_DIR
    documentation pattern."""
    example_text = _read(".env.example")
    assert "APRILCAM_CONFIG_DIR" in example_text
