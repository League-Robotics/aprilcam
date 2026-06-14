"""Tests for camera_config helpers and Config.playfields_dir (Sprint 012, Ticket 001)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aprilcam.camera.camera_config import load_camera_config, save_camera_config
from aprilcam.config import Config


# ---------------------------------------------------------------------------
# load_camera_config
# ---------------------------------------------------------------------------


def test_load_missing(tmp_path):
    """load_camera_config returns None when config.json is absent."""
    result = load_camera_config(tmp_path)
    assert result is None


def test_load_returns_dict(tmp_path):
    """load_camera_config returns the parsed dict when config.json exists."""
    (tmp_path / "config.json").write_text('{"playfield": "main-playfield"}\n', encoding="utf-8")
    result = load_camera_config(tmp_path)
    assert result == {"playfield": "main-playfield"}


def test_load_malformed_returns_none(tmp_path):
    """load_camera_config returns None for malformed JSON."""
    (tmp_path / "config.json").write_text("this is not json", encoding="utf-8")
    result = load_camera_config(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# save_camera_config + round-trip
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path):
    """save then load returns the same dict."""
    data = {"playfield": "main-playfield"}
    camera_dir = tmp_path / "cam"
    camera_dir.mkdir()
    save_camera_config(camera_dir, data)
    result = load_camera_config(camera_dir)
    assert result == data


def test_atomic_write_no_tmp_left(tmp_path):
    """No .tmp file is left behind after a successful save."""
    camera_dir = tmp_path / "cam"
    camera_dir.mkdir()
    save_camera_config(camera_dir, {"playfield": "arena"})
    tmp_files = list(camera_dir.glob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"


def test_creates_dir(tmp_path):
    """save_camera_config creates camera_dir if it does not exist."""
    camera_dir = tmp_path / "nonexistent" / "nested"
    assert not camera_dir.exists()
    save_camera_config(camera_dir, {"playfield": "test"})
    assert camera_dir.is_dir()
    assert (camera_dir / "config.json").exists()


def test_save_returns_path(tmp_path):
    """save_camera_config returns the path of the written file."""
    camera_dir = tmp_path / "cam"
    camera_dir.mkdir()
    returned = save_camera_config(camera_dir, {"x": 1})
    assert returned == camera_dir / "config.json"


def test_save_accepts_str_path(tmp_path):
    """save_camera_config and load_camera_config accept str paths."""
    camera_dir = tmp_path / "cam"
    camera_dir.mkdir()
    save_camera_config(str(camera_dir), {"playfield": "field"})
    result = load_camera_config(str(camera_dir))
    assert result == {"playfield": "field"}


# ---------------------------------------------------------------------------
# Config.playfields_dir
# ---------------------------------------------------------------------------


def test_config_playfields_dir(tmp_path, monkeypatch):
    """Config.load().playfields_dir returns <data_dir>/playfields as an absolute Path."""
    # Remove any APRILCAM_ env interference
    for key in list(os.environ.keys()):
        if key.startswith("APRILCAM_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("APRILCAM_DATA_DIR", str(tmp_path / "data" / "aprilcam"))

    cfg = Config.load(start=tmp_path)

    expected = (tmp_path / "data" / "aprilcam" / "playfields").resolve()
    assert cfg.playfields_dir == expected
    assert cfg.playfields_dir.is_absolute()


def test_playfields_dir_is_under_data_dir(tmp_path, monkeypatch):
    """playfields_dir must be a child of data_dir."""
    for key in list(os.environ.keys()):
        if key.startswith("APRILCAM_"):
            monkeypatch.delenv(key, raising=False)

    cfg = Config.load(start=tmp_path)
    assert cfg.playfields_dir.parent == cfg.data_dir
    assert cfg.playfields_dir.name == "playfields"
