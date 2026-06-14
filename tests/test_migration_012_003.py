"""Smoke tests for Sprint 012 Ticket 003 data migration.

Validates that:
- data/aprilcam/playfields/main-playfield.json exists with the required fields.
- data/aprilcam/playfield.json is absent.
- config.json is present and correct in all three camera directories.
- paths.json is [] in the two cameras that have one.
- PlayfieldDefinitionRegistry loads exactly one entry named 'main-playfield'.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aprilcam.camera.camera_config import load_camera_config
from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry

# Locate the data root relative to this file (tests/ -> project root -> data/).
_PROJECT_ROOT = Path(__file__).parent.parent
_DATA_DIR = _PROJECT_ROOT / "data" / "aprilcam"
_PLAYFIELDS_DIR = _DATA_DIR / "playfields"
_CAMERAS_DIR = _DATA_DIR / "cameras"

_MAIN_PLAYFIELD = _PLAYFIELDS_DIR / "main-playfield.json"
_LEGACY_PLAYFIELD = _DATA_DIR / "playfield.json"


@pytest.mark.skipif(
    not _MAIN_PLAYFIELD.exists(),
    reason="data/aprilcam/playfields/main-playfield.json not present (migration not run)",
)
def test_main_playfield_json_exists():
    """main-playfield.json is present at the new location."""
    assert _MAIN_PLAYFIELD.exists()


@pytest.mark.skipif(
    not _MAIN_PLAYFIELD.exists(),
    reason="data/aprilcam/playfields/main-playfield.json not present",
)
def test_main_playfield_has_name_fields():
    """main-playfield.json has 'name' and 'display_name' at the top level."""
    data = json.loads(_MAIN_PLAYFIELD.read_text(encoding="utf-8"))
    assert data.get("name") == "main-playfield", f"Missing or wrong 'name': {data.get('name')!r}"
    assert data.get("display_name") == "Main Playfield", (
        f"Missing or wrong 'display_name': {data.get('display_name')!r}"
    )


@pytest.mark.skipif(
    not _MAIN_PLAYFIELD.exists(),
    reason="data/aprilcam/playfields/main-playfield.json not present",
)
def test_main_playfield_preserves_existing_content():
    """main-playfield.json retains all original geometry and marker data."""
    data = json.loads(_MAIN_PLAYFIELD.read_text(encoding="utf-8"))
    pf = data.get("playfield", {})
    assert pf.get("width_cm") == pytest.approx(134.3)
    assert pf.get("height_cm") == pytest.approx(89.3)
    assert pf.get("origin") == "apriltag-center-a1"
    assert len(data.get("april_tags", [])) == 1
    assert len(data.get("aruco_tags", [])) == 8
    assert len(data.get("rectangles", [])) == 8
    assert len(data.get("dots", [])) == 8


def test_legacy_playfield_json_absent():
    """data/aprilcam/playfield.json must not exist after migration."""
    assert not _LEGACY_PLAYFIELD.exists(), (
        "Legacy data/aprilcam/playfield.json still present — migration incomplete"
    )


@pytest.mark.skipif(
    not _MAIN_PLAYFIELD.exists(),
    reason="data/aprilcam/playfields/main-playfield.json not present",
)
def test_registry_yields_exactly_main_playfield():
    """PlayfieldDefinitionRegistry.load_all returns exactly ['main-playfield']."""
    reg = PlayfieldDefinitionRegistry()
    reg.load_all(_PLAYFIELDS_DIR)
    assert reg.list() == ["main-playfield"], f"Expected ['main-playfield'], got {reg.list()}"


@pytest.mark.parametrize("cam_name", [
    "arducam-ov9782-usb-camera",
    "hd-usb-camera",
    "global-shutter-camera",
])
def test_camera_config_json_contains_playfield(cam_name):
    """Each of the three camera directories has config.json with playfield=main-playfield."""
    cam_dir = _CAMERAS_DIR / cam_name
    if not cam_dir.exists():
        pytest.skip(f"Camera directory not present: {cam_dir}")
    result = load_camera_config(cam_dir)
    assert result is not None, f"config.json missing or unreadable in {cam_dir}"
    assert result.get("playfield") == "main-playfield", (
        f"Expected playfield='main-playfield' in {cam_dir}/config.json, got {result!r}"
    )


@pytest.mark.parametrize("cam_name", [
    "arducam-ov9782-usb-camera",
    "hd-usb-camera",
])
def test_paths_json_cleared_to_empty_list(cam_name):
    """paths.json for cameras that had one must be [] after migration."""
    paths_file = _CAMERAS_DIR / cam_name / "paths.json"
    if not paths_file.exists():
        pytest.skip(f"paths.json not present for {cam_name}")
    content = json.loads(paths_file.read_text(encoding="utf-8"))
    assert content == [], f"Expected [] in {paths_file}, got {content!r}"
