"""Tests for `aprilcam migrate-dirs` (019-004) and the daemon's one-time
legacy-layout warning.

All tests build fully synthetic Config instances rooted at tmp_path — no
real /etc, /var/lib, ~/.config, or camera hardware is ever touched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from aprilcam.cli import migrate_dirs_cli
from aprilcam.config import Config


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _legacy_config(tmp_path: Path, *, config_dir_name: str = "config") -> Config:
    """Return a Config with a legacy single-tree layout already populated
    under data_dir: cameras/<slug>/{config.json,calibration.json}, and
    playfields/<name>.json. config_dir points at a SEPARATE, currently-empty
    directory (the true migration target).
    """
    data_dir = tmp_path / "data"
    config_dir = tmp_path / config_dir_name

    cam_dir = data_dir / "cameras" / "cam1"
    cam_dir.mkdir(parents=True)
    (cam_dir / "config.json").write_text(json.dumps({"exposure": 10}))
    (cam_dir / "calibration.json").write_text(json.dumps({"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}))
    (cam_dir / "info.json").write_text(json.dumps({"name": "cam1"}))
    (cam_dir / "paths.json").write_text(json.dumps({"paths": []}))

    playfields_dir = data_dir / "playfields"
    playfields_dir.mkdir(parents=True)
    (playfields_dir / "field1.json").write_text(json.dumps({"name": "field1"}))

    (data_dir / "registry.json").write_text(json.dumps({}))
    (data_dir / "hosts.json").write_text(json.dumps({}))

    return Config(
        data_dir=data_dir,
        config_dir=config_dir,
        socket_dir=tmp_path / "sock",
        calibration_dir=data_dir / "calibration",
    )


def _state_snapshot(config: Config) -> dict:
    """Capture mtimes + content of every state file for before/after comparison."""
    snap = {}
    for name in ("calibration.json", "info.json", "paths.json"):
        p = config.data_dir / "cameras" / "cam1" / name
        snap[str(p)] = (p.stat().st_mtime_ns, p.read_bytes())
    for name in ("registry.json", "hosts.json"):
        p = config.data_dir / name
        snap[str(p)] = (p.stat().st_mtime_ns, p.read_bytes())
    return snap


# ---------------------------------------------------------------------------
# run_migration() — dry-run
# ---------------------------------------------------------------------------


def test_dry_run_prints_plan_and_writes_nothing(tmp_path, capsys):
    config = _legacy_config(tmp_path)

    rc = migrate_dirs_cli.run_migration(config, dry_run=True)

    assert rc == 0
    out = capsys.readouterr().out
    src_cfg = config.data_dir / "cameras" / "cam1" / "config.json"
    dst_cfg = config.config_dir / "cameras" / "cam1" / "config.json"
    src_pf = config.data_dir / "playfields" / "field1.json"
    dst_pf = config.config_dir / "playfields" / "field1.json"
    assert f"would move: {src_cfg} -> {dst_cfg}" in out
    assert f"would move: {src_pf} -> {dst_pf}" in out

    # Nothing moved.
    assert src_cfg.exists()
    assert src_pf.exists()
    assert not dst_cfg.exists()
    assert not dst_pf.exists()


def test_dry_run_does_not_touch_state_files(tmp_path):
    config = _legacy_config(tmp_path)
    before = _state_snapshot(config)

    migrate_dirs_cli.run_migration(config, dry_run=True)

    after = _state_snapshot(config)
    assert before == after


# ---------------------------------------------------------------------------
# run_migration() — real run
# ---------------------------------------------------------------------------


def test_real_run_moves_config_files_only(tmp_path):
    config = _legacy_config(tmp_path)

    rc = migrate_dirs_cli.run_migration(config)

    assert rc == 0
    dst_cfg = config.config_dir / "cameras" / "cam1" / "config.json"
    dst_pf = config.config_dir / "playfields" / "field1.json"
    assert dst_cfg.exists()
    assert dst_pf.exists()
    assert json.loads(dst_cfg.read_text()) == {"exposure": 10}
    assert json.loads(dst_pf.read_text()) == {"name": "field1"}

    # Source config files gone.
    assert not (config.data_dir / "cameras" / "cam1" / "config.json").exists()
    assert not (config.data_dir / "playfields" / "field1.json").exists()


def test_real_run_leaves_state_files_byte_identical_in_place(tmp_path):
    config = _legacy_config(tmp_path)
    before = _state_snapshot(config)

    migrate_dirs_cli.run_migration(config)

    after = _state_snapshot(config)
    assert before == after
    # All still physically present under data_dir.
    for path_str in before:
        assert Path(path_str).exists()


def test_real_run_prints_summary_with_config_dir(tmp_path, capsys):
    config = _legacy_config(tmp_path)
    migrate_dirs_cli.run_migration(config)
    out = capsys.readouterr().out
    assert f"config now lives under: {config.config_dir}" in out
    assert "moved 2 file(s)." in out


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_second_run_reports_nothing_to_migrate(tmp_path, capsys):
    config = _legacy_config(tmp_path)
    migrate_dirs_cli.run_migration(config)
    capsys.readouterr()  # discard first run's output

    dst_cfg = config.config_dir / "cameras" / "cam1" / "config.json"
    dst_pf = config.config_dir / "playfields" / "field1.json"
    before_cfg = (dst_cfg.stat().st_mtime_ns, dst_cfg.read_bytes())
    before_pf = (dst_pf.stat().st_mtime_ns, dst_pf.read_bytes())

    rc = migrate_dirs_cli.run_migration(config)

    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to migrate" in out

    after_cfg = (dst_cfg.stat().st_mtime_ns, dst_cfg.read_bytes())
    after_pf = (dst_pf.stat().st_mtime_ns, dst_pf.read_bytes())
    assert before_cfg == after_cfg
    assert before_pf == after_pf


def test_dry_run_twice_in_a_row_is_stable(tmp_path, capsys):
    config = _legacy_config(tmp_path)
    migrate_dirs_cli.run_migration(config, dry_run=True)
    capsys.readouterr()
    rc = migrate_dirs_cli.run_migration(config, dry_run=True)
    assert rc == 0
    # Nothing was ever moved by a dry run.
    assert (config.data_dir / "cameras" / "cam1" / "config.json").exists()


# ---------------------------------------------------------------------------
# config_dir == data_dir -> no-op
# ---------------------------------------------------------------------------


def test_config_dir_equals_data_dir_is_noop(tmp_path, capsys):
    data_dir = tmp_path / "shared"
    cam_dir = data_dir / "cameras" / "cam1"
    cam_dir.mkdir(parents=True)
    (cam_dir / "config.json").write_text("{}")

    config = Config(
        data_dir=data_dir,
        config_dir=data_dir,
        socket_dir=tmp_path / "sock",
        calibration_dir=data_dir / "calibration",
    )

    rc = migrate_dirs_cli.run_migration(config)

    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to migrate" in out
    # File never moved (there's nowhere to move it to).
    assert (cam_dir / "config.json").exists()


# ---------------------------------------------------------------------------
# Conflict handling
# ---------------------------------------------------------------------------


def test_conflict_skips_and_reports_leaves_source_and_dest_untouched(tmp_path, capsys):
    config = _legacy_config(tmp_path)

    dst_cfg = config.config_dir / "cameras" / "cam1" / "config.json"
    dst_cfg.parent.mkdir(parents=True)
    dst_cfg.write_text(json.dumps({"exposure": 999, "note": "admin-authored"}))
    dest_before = dst_cfg.read_bytes()

    rc = migrate_dirs_cli.run_migration(config)

    assert rc == 1  # partial failure due to unresolved conflict
    out = capsys.readouterr().out
    assert "conflict" in out.lower()

    src_cfg = config.data_dir / "cameras" / "cam1" / "config.json"
    assert src_cfg.exists(), "source must NOT be deleted on conflict"
    assert dst_cfg.read_bytes() == dest_before, "destination must be unchanged"

    # The unrelated playfield move should still have succeeded.
    assert (config.config_dir / "playfields" / "field1.json").exists()


def test_conflict_with_force_overwrites_destination(tmp_path):
    config = _legacy_config(tmp_path)

    dst_cfg = config.config_dir / "cameras" / "cam1" / "config.json"
    dst_cfg.parent.mkdir(parents=True)
    dst_cfg.write_text(json.dumps({"exposure": 999}))

    rc = migrate_dirs_cli.run_migration(config, force=True)

    assert rc == 0
    assert json.loads(dst_cfg.read_text()) == {"exposure": 10}
    assert not (config.data_dir / "cameras" / "cam1" / "config.json").exists()


# ---------------------------------------------------------------------------
# Playfield-only / camera-only scans
# ---------------------------------------------------------------------------


def test_playfields_migrate_to_config_playfields_dir(tmp_path):
    config = _legacy_config(tmp_path)
    migrate_dirs_cli.run_migration(config)
    assert (config.config_playfields_dir / "field1.json").exists()


# ---------------------------------------------------------------------------
# Post-migration: load_calibration_from_camera_dir resolves the moved config
# ---------------------------------------------------------------------------


def test_post_migration_loader_returns_fully_populated_calibration(tmp_path):
    """After a real migration, load_calibration_from_camera_dir(camera_dir,
    config_dir=<config_cameras_dir's parent>) still overlays config.json
    fields (device_name, settings, etc.) even though config.json no longer
    lives next to calibration.json.
    """
    from aprilcam.calibration.calibration import load_calibration_from_camera_dir

    config = _legacy_config(tmp_path)
    cam_dir = config.data_dir / "cameras" / "cam1"
    (cam_dir / "config.json").write_text(
        json.dumps({"device_name": "OV9782", "resolution": [1280, 720], "settings": {"gain": "3"}})
    )

    rc = migrate_dirs_cli.run_migration(config)
    assert rc == 0
    # config.json is gone from the legacy location now.
    assert not (cam_dir / "config.json").exists()

    cal = load_calibration_from_camera_dir(
        cam_dir, config_dir=config.config_cameras_dir
    )

    assert cal is not None
    assert cal.device_name == "OV9782"
    assert cal.settings == {"gain": "3"}


# ---------------------------------------------------------------------------
# CLI entry point (argparse wiring)
# ---------------------------------------------------------------------------


def test_cli_main_dry_run_flag(tmp_path, monkeypatch, capsys):
    config = _legacy_config(tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))

    rc = migrate_dirs_cli.main(["--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "would move" in out
    assert (config.data_dir / "cameras" / "cam1" / "config.json").exists()


def test_cli_main_real_run(tmp_path, monkeypatch):
    config = _legacy_config(tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))

    rc = migrate_dirs_cli.main([])

    assert rc == 0
    assert (config.config_dir / "cameras" / "cam1" / "config.json").exists()


def test_cli_registered_in_subcommand_table():
    from aprilcam import cli as cli_pkg

    assert "migrate-dirs" in cli_pkg.SUBCOMMANDS
    assert cli_pkg.SUBCOMMANDS["migrate-dirs"]["module"] == "aprilcam.cli.migrate_dirs_cli"
    # Not gated behind the daemon/OpenCV extra — Config has no cv2 dependency.
    assert "migrate-dirs" not in cli_pkg.DAEMON_COMMANDS


# ---------------------------------------------------------------------------
# Daemon one-time legacy-layout warning
# ---------------------------------------------------------------------------


pytest.importorskip("aprilcam.daemon.server", reason="requires aprilcam[daemon]")


def _reset_warned_flag(monkeypatch):
    import aprilcam.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_legacy_layout_warned", False)


def _bare_server(config: Config):
    """Construct a DaemonServer without running it (we only exercise the
    warning helper, which performs no I/O and needs no gRPC/camera setup).
    """
    from aprilcam.daemon.server import DaemonServer

    return DaemonServer(
        config,
        unix_path=str(config.socket_dir / "control.sock"),
    )


def test_daemon_warns_once_on_legacy_layout(tmp_path, monkeypatch, caplog):
    _reset_warned_flag(monkeypatch)
    config = _legacy_config(tmp_path)
    server = _bare_server(config)

    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.server"):
        server._warn_if_legacy_layout()
        server._warn_if_legacy_layout()  # second call — must not double-warn

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "migrate-dirs" in warnings[0].message


def test_daemon_warning_performs_no_file_operations(tmp_path, monkeypatch):
    _reset_warned_flag(monkeypatch)
    config = _legacy_config(tmp_path)
    server = _bare_server(config)

    def _list_all(root: Path) -> set:
        return {str(p) for p in root.rglob("*")} if root.exists() else set()

    before_data = _list_all(config.data_dir)
    before_config = _list_all(config.config_dir)

    server._warn_if_legacy_layout()

    after_data = _list_all(config.data_dir)
    after_config = _list_all(config.config_dir)
    assert before_data == after_data
    assert before_config == after_config


def test_daemon_no_warning_when_config_dir_already_populated(tmp_path, monkeypatch, caplog):
    """Post-migration (or a fresh split-root install with config already in
    place): config_dir has files, so no warning fires.
    """
    _reset_warned_flag(monkeypatch)
    config = _legacy_config(tmp_path)
    migrate_dirs_cli.run_migration(config)  # populate config_dir for real

    server = _bare_server(config)
    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.server"):
        server._warn_if_legacy_layout()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0


def test_daemon_no_warning_when_config_dir_explicitly_set(tmp_path, monkeypatch, caplog):
    """A proper split-root install (APRILCAM_CONFIG_DIR explicitly set) with
    an empty (not-yet-populated) config_dir must NOT warn — this is a fresh
    install, not a legacy layout.
    """
    _reset_warned_flag(monkeypatch)
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    cam_dir = data_dir / "cameras" / "cam1"
    cam_dir.mkdir(parents=True)
    (cam_dir / "config.json").write_text("{}")

    config = Config(
        data_dir=data_dir,
        config_dir=config_dir,
        socket_dir=tmp_path / "sock",
        calibration_dir=data_dir / "calibration",
        _config_dir_explicit=True,
    )
    server = _bare_server(config)

    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.server"):
        server._warn_if_legacy_layout()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0


def test_daemon_no_warning_when_config_dir_equals_data_dir(tmp_path, monkeypatch, caplog):
    _reset_warned_flag(monkeypatch)
    data_dir = tmp_path / "shared"
    cam_dir = data_dir / "cameras" / "cam1"
    cam_dir.mkdir(parents=True)
    (cam_dir / "config.json").write_text("{}")

    config = Config(
        data_dir=data_dir,
        config_dir=data_dir,
        socket_dir=tmp_path / "sock",
        calibration_dir=data_dir / "calibration",
    )
    server = _bare_server(config)

    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.server"):
        server._warn_if_legacy_layout()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0
