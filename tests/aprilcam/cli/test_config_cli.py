"""Tests for ``aprilcam.cli.config`` (OOP change, 2026-08-22).

**UC-01's "no daemon running anywhere" is proven two ways here**, the
same pattern ``test_guide_cli.py`` established for ``cli/guide.py``:

* :func:`test_config_module_imports_nothing_daemon_or_network_shaped` --
  a static AST scan of ``cli/config.py``'s own import statements.
* :func:`test_config_still_works_even_if_discovery_construction_is_poisoned`
  -- a dynamic, mechanical proof: ``client.discovery.Discovery.__init__``
  is replaced with something that unconditionally raises, and
  ``aprilcam config`` still succeeds.

Everything else is headless and file-only: every test drives
:func:`aprilcam.cli.config.cmd_config` against a real
``config.directories.Directories`` pointed at ``tmp_path`` (the
project's existing convention, e.g. ``test_locate_cli.py``,
``test_loader.py``) -- no daemon, no camera, no network.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import pytest

from aprilcam.cli import config as config_cli
from aprilcam.cli import main as cli_main
from aprilcam.cli._common import EXIT_OK
from aprilcam.config import directories as directories_module
from aprilcam.config.directories import Directories
from aprilcam.config.loader import ConfigLoader

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_CLI_MODULE_PATH = REPO_ROOT / "src" / "aprilcam" / "cli" / "config.py"

#: Same forbidden-name list ``test_guide_cli.py`` polices ``cli/guide.py``
#: with -- none of these belong in this module's own import list either,
#: since ``cmd_config`` must never construct a ``Discovery``/``Daemon``
#: or open a socket (this command's whole point is working when no
#: daemon can even start).
_FORBIDDEN_IMPORT_NAMES = (
    "grpc",
    "aprilcam.client.discovery",
    "aprilcam.client.daemon_client",
    "aprilcam.client.lifecycle",
    "aprilcam.daemon.lifecycle",
    "aprilcam.daemon.service",
    "aprilcam.daemon.rpc",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stray env vars from the host leak into any test."""
    for name in config_cli._ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _args() -> argparse.Namespace:
    return argparse.Namespace()


def _directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Directories:
    """A real ``Directories`` with ``config_dir``/``state_dir`` pinned
    (flag tier) and ``socket_dir`` forced to the "default" tier by
    pointing ``SYSTEM_SOCKET_DIR`` at a writable ``tmp_path``
    subdirectory -- the same technique ``test_directories.py`` uses --
    so every root's reported source is deterministic."""
    socket_dir = tmp_path / "run" / "aprilcam"
    socket_dir.mkdir(parents=True)
    monkeypatch.setattr(directories_module, "SYSTEM_SOCKET_DIR", socket_dir)
    return Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")


# ---------------------------------------------------------------------------
# "works with no daemon running" -- no Discovery/Daemon object constructed
# ---------------------------------------------------------------------------


def test_config_module_imports_nothing_daemon_or_network_shaped() -> None:
    """Static proof: ``cli/config.py``'s own import statements name none
    of the modules that could construct a ``Daemon`` or a network
    channel."""
    tree = ast.parse(CONFIG_CLI_MODULE_PATH.read_text(), filename=str(CONFIG_CLI_MODULE_PATH))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [name for name in imported if name in _FORBIDDEN_IMPORT_NAMES]
    assert not offenders, (
        f"cli/config.py must not import daemon/network-construction modules, "
        f"but imports: {offenders}"
    )


def test_config_still_works_even_if_discovery_construction_is_poisoned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dynamic, mechanical proof: with ``client.discovery.Discovery.__init__``
    replaced by something that unconditionally raises, ``aprilcam config``
    (via the real top-level ``main``) still succeeds -- possible only if
    ``cmd_config``'s actual runtime path never constructs a ``Discovery``."""
    import aprilcam.client.discovery as discovery_module

    def _poisoned_init(self, *args, **kwargs):
        raise AssertionError(
            "aprilcam config must never construct a Discovery -- it has to "
            "work with no daemon running anywhere"
        )

    monkeypatch.setattr(discovery_module.Discovery, "__init__", _poisoned_init)
    monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))

    exit_code = cli_main(["config"])

    assert exit_code == EXIT_OK
    assert "roots:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# add_subparser -- structural: no -d/--daemon, no --all
# ---------------------------------------------------------------------------


def test_add_subparser_wires_no_daemon_or_all_option() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    config_cli.add_subparser(subparsers)

    config_parser = None
    for action in subparsers.choices.values():
        if action.prog.endswith("config"):
            config_parser = action
            break
    assert config_parser is not None

    dests = {a.dest for a in config_parser._actions}
    assert "daemon" not in dests
    assert "all" not in dests


# ---------------------------------------------------------------------------
# Roots + provenance
# ---------------------------------------------------------------------------


def test_reports_roots_with_their_cascade_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)

    exit_code = config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert f"config_dir: {directories.config_dir}  (flag)" in out
    assert f"state_dir:  {directories.state_dir}  (flag)" in out
    assert f"socket_dir: {directories.socket_dir}  (default)" in out


# ---------------------------------------------------------------------------
# Environment variables -- set, unset, and blank-treated-as-unset
# ---------------------------------------------------------------------------


def test_reports_every_cascade_env_var_unset_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    for name in config_cli._ENV_VARS:
        assert f"{name}" in out
    assert out.count("unset") >= len(config_cli._ENV_VARS)


def test_reports_a_set_env_var_with_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)
    monkeypatch.setenv("APRILCAM_DAEMON_HOST", "pi-field.local")
    monkeypatch.setenv("APRILCAM_LOG_LEVEL", "DEBUG")

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "APRILCAM_DAEMON_HOST" in out and "pi-field.local" in out
    assert "APRILCAM_LOG_LEVEL" in out and "DEBUG" in out


def test_reports_a_blank_env_var_as_treated_like_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)
    monkeypatch.setenv("APRILCAM_STATE_DIR", "   ")

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "treated as unset" in out


# ---------------------------------------------------------------------------
# Playfields -- enumerated, not just the pattern
# ---------------------------------------------------------------------------


def test_reports_no_playfields_when_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "playfields (" in out
    assert "(none)" in out


def test_enumerates_every_playfield_file_actually_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)
    playfields_dir = directories.config_dir / "playfields"
    playfields_dir.mkdir(parents=True)
    (playfields_dir / "main-playfield.json").write_text("{}", encoding="utf-8")
    (playfields_dir / "second-field.json").write_text("{}", encoding="utf-8")

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "main-playfield.json  exists" in out
    assert "second-field.json  exists" in out


# ---------------------------------------------------------------------------
# Cameras -- config_dir-only, state_dir-only, registry-only, all present
# ---------------------------------------------------------------------------


def test_reports_no_known_cameras_when_nothing_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "(no known cameras)" in out
    assert "registry:" in out and "missing" in out


def test_reports_a_config_only_camera_with_config_present_calibration_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)
    cam_dir = directories.config_dir / "cameras" / "logitech-c920"
    cam_dir.mkdir(parents=True)
    (cam_dir / "config.json").write_text(json.dumps({"name": "logitech-c920"}), encoding="utf-8")

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "logitech-c920:" in out
    config_line = [line for line in out.splitlines() if "cameras/logitech-c920/config.json" in line][0]
    calibration_line = [
        line for line in out.splitlines() if "cameras/logitech-c920/calibration.json" in line
    ][0]
    assert "exists" in config_line
    assert "missing" in calibration_line


def test_reports_a_state_only_camera_with_calibration_present_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)
    cam_dir = directories.state_dir / "cameras" / "orphaned-cam"
    cam_dir.mkdir(parents=True)
    (cam_dir / "calibration.json").write_text("{}", encoding="utf-8")

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "orphaned-cam:" in out
    config_line = [line for line in out.splitlines() if "cameras/orphaned-cam/config.json" in line][0]
    calibration_line = [
        line for line in out.splitlines() if "cameras/orphaned-cam/calibration.json" in line
    ][0]
    assert "missing" in config_line
    assert "exists" in calibration_line


def test_reports_a_registry_only_camera_with_both_files_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)
    registry_dir = directories.state_dir / "cameras"
    registry_dir.mkdir(parents=True)
    registry_path = registry_dir / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "cameras": {
                    "vidpid:1234:5678@1-1": {
                        "hardware_key": "vidpid:1234:5678@1-1",
                        "slug": "registry-only-cam",
                        "number": 1,
                        "device_name": "Some Webcam",
                        "last_seen": "2026-08-22T00:00:00+00:00",
                    }
                },
                "next_number": 2,
            }
        ),
        encoding="utf-8",
    )

    config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    out = capsys.readouterr().out
    assert "registry-only-cam:" in out
    assert "registry:" in out and "exists" in out


def test_a_corrupt_registry_contributes_no_slugs_and_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)
    registry_dir = directories.state_dir / "cameras"
    registry_dir.mkdir(parents=True)
    (registry_dir / "registry.json").write_text("not valid json{{{", encoding="utf-8")

    exit_code = config_cli.cmd_config(_args(), directories_factory=lambda: directories)

    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "(no known cameras)" in out
    assert "registry:" in out and "exists" in out


# ---------------------------------------------------------------------------
# Load errors -- the issue's "highest-value part"
# ---------------------------------------------------------------------------


def test_reports_no_load_errors_when_everything_is_well_formed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directories = _directories(tmp_path, monkeypatch)

    config_cli.cmd_config(
        _args(), directories_factory=lambda: directories, loader_factory=ConfigLoader
    )

    out = capsys.readouterr().out
    assert "load errors:" in out
    assert "  (none)" in out


def test_surfaces_a_real_load_error_from_a_malformed_camera_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact motivating case from the issue's Motivation section: a
    v1-shaped ``config.json`` missing a ``name`` field. Uses the real
    ``ConfigLoader`` (no fake) so this proves the actual
    ``MalformedDocument`` message reaches stdout, not a stand-in."""
    directories = _directories(tmp_path, monkeypatch)
    broken_cam_dir = directories.config_dir / "cameras" / "broken-cam"
    broken_cam_dir.mkdir(parents=True)
    broken_config_path = broken_cam_dir / "config.json"
    broken_config_path.write_text(json.dumps({"settings": {}}), encoding="utf-8")

    exit_code = config_cli.cmd_config(
        _args(), directories_factory=lambda: directories, loader_factory=ConfigLoader
    )

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert str(broken_config_path) in out
    assert "no 'name' field" in out


# ---------------------------------------------------------------------------
# End-to-end through the real top-level `main` -- no daemon started
# ---------------------------------------------------------------------------


def test_config_through_main_reports_every_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))

    exit_code = cli_main(["config"])

    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "roots:" in out
    assert "environment variables:" in out
    assert "playfields (" in out
    assert "cameras:" in out
    assert "load errors:" in out
