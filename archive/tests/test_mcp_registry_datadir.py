"""Regression: the MCP playfield registry must load from the daemon's actual
data dir (derived from the absolute camera_dir), not a CWD-relative
Config.load().

Bug: when the MCP server's working directory differs from the daemon's, a
CWD-relative ``Config.load().playfields_dir`` points at an empty/nonexistent
directory, so ``list_playfields``/``get_playfield``/``where`` return nothing
even though the daemon serves the full map.  ``open_camera`` receives the
daemon's absolute ``camera_dir`` (``<data_dir>/cameras/<cam_name>``); deriving
``<data_dir>/playfields`` from it makes the registry CWD-independent.
"""
import json

from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry


def _write_layout(tmp_path):
    data = tmp_path / "data" / "aprilcam"
    camera_dir = data / "cameras" / "arducam-ov9782-usb-camera"
    camera_dir.mkdir(parents=True)
    pf_dir = data / "playfields"
    pf_dir.mkdir(parents=True)
    (pf_dir / "main-playfield.json").write_text(json.dumps({
        "name": "main-playfield",
        "display_name": "Main Playfield",
        "playfield": {"width_cm": 134.3, "height_cm": 89.3, "origin": "apriltag-center-a1"},
        "april_tags": [],
        "aruco_tags": [{"slug": "aruco-west-internal-u9", "type": "aruco_tag",
                        "id": 9, "cardinal": "west", "x": -35, "y": 0}],
        "rectangles": [],
        "dots": [],
    }), encoding="utf-8")
    return camera_dir, pf_dir


def test_registry_loads_from_daemon_derived_camera_dir(tmp_path):
    camera_dir, pf_dir = _write_layout(tmp_path)

    # The exact derivation open_camera performs on the daemon-returned path.
    derived = camera_dir.parent.parent / "playfields"
    assert derived == pf_dir

    reg = PlayfieldDefinitionRegistry()
    reg.load_all(derived)
    assert reg.list() == ["main-playfield"]
    assert [t["id"] for t in reg.get("main-playfield").aruco_tags] == [9]


def test_registry_empty_for_wrong_cwd_relative_dir(tmp_path):
    # A CWD-relative path that doesn't contain the map -> empty (the bug shape).
    _write_layout(tmp_path)
    wrong = tmp_path / "somewhere_else" / "data" / "aprilcam" / "playfields"
    reg = PlayfieldDefinitionRegistry()
    reg.load_all(wrong)  # nonexistent dir -> stays empty, no raise
    assert reg.list() == []
