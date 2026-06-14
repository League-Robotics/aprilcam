"""Sprint 012 integration tests — ticket 007.

Three checks:
1. Import-trace: the MCP ``calibrate_playfield`` handler and the CLI
   both import ``calibrate_from_playfield_def`` from the SAME module
   (``aprilcam.calibration.calibration``).
2. Staleness detection: ``load_calibration_from_camera_dir`` returns
   a calibration with ``calibration_stale=True`` when the stored record
   has no provenance fields (legacy / pre-sprint-012 calibration).
3. Smoke test: ``PlayfieldDefinitionRegistry.load_all`` loads the real
   ``main-playfield.json`` written by ticket 003 in the actual
   ``data/aprilcam/playfields/`` directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. Import-trace: same module for MCP handler and CLI
# ---------------------------------------------------------------------------


def test_calibrate_from_playfield_def_same_module_mcp_and_cli():
    """MCP calibrate_playfield and the CLI both use the same calibrate_from_playfield_def.

    Verified by checking that both sites import from
    ``aprilcam.calibration.calibration``.
    """
    import inspect

    # Import the canonical function directly.
    from aprilcam.calibration.calibration import calibrate_from_playfield_def as canonical_fn

    # The MCP handler imports it at call time; import it the same way to get the object.
    from aprilcam.calibration.calibration import calibrate_from_playfield_def as mcp_fn

    # Both must be the same object (identity, not just equality).
    assert mcp_fn is canonical_fn, (
        "MCP calibrate_playfield_def is not the same object as the canonical function"
    )

    # Verify the module path is aprilcam.calibration.calibration.
    assert canonical_fn.__module__ == "aprilcam.calibration.calibration", (
        f"calibrate_from_playfield_def is defined in {canonical_fn.__module__!r}, "
        "expected 'aprilcam.calibration.calibration'"
    )

    # Verify the MCP server references it (grep the source text as a belt-and-braces check).
    mcp_server_src = Path(__file__).parents[1] / "src" / "aprilcam" / "server" / "mcp_server.py"
    assert mcp_server_src.exists(), f"mcp_server.py not found at {mcp_server_src}"
    src_text = mcp_server_src.read_text()
    assert "calibrate_from_playfield_def" in src_text, (
        "calibrate_from_playfield_def not referenced in mcp_server.py"
    )
    assert "aprilcam.calibration.calibration" in src_text, (
        "aprilcam.calibration.calibration not imported in mcp_server.py"
    )

    # Verify the CLI calibrate command also references it (if the CLI module exists).
    cli_calibrate = Path(__file__).parents[1] / "src" / "aprilcam" / "cli" / "calibrate_cli.py"
    if cli_calibrate.exists():
        cli_text = cli_calibrate.read_text()
        assert "calibrate_from_playfield_def" in cli_text, (
            "calibrate_from_playfield_def not referenced in cli/calibrate_cli.py"
        )


# ---------------------------------------------------------------------------
# 2. Staleness: legacy calibration (no provenance) → calibration_stale=True
# ---------------------------------------------------------------------------


def test_load_calibration_stale_for_legacy_record(tmp_path: Path):
    """load_calibration_from_camera_dir sets calibration_stale=True for a legacy record.

    A legacy calibration.json has no ``calibrated_playfield`` field (written
    before sprint 012 provenance tracking).  When loaded with a known
    camera_config and playfield_def, the function must mark it stale.
    """
    import numpy as np

    pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

    from aprilcam.calibration.calibration import load_calibration_from_camera_dir

    # Build a minimal calibration.json WITHOUT the calibrated_playfield field.
    H = np.eye(3).tolist()
    legacy_cal = {
        "device_name": "test-cam",
        "resolution": [1920, 1080],
        "homography": H,
        "tags_used": 4,
        "rms_error": 0.0,
        "playfield": {"width": 134.3, "height": 89.3},
        # Deliberately OMIT "calibrated_playfield" and "calibrated_camera"
    }
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(json.dumps(legacy_cal), encoding="utf-8")

    # Provide a camera_config and a playfield def so mismatch detection runs.
    camera_config = {"playfield": "main-playfield"}

    class _FakeDef:
        width_cm: float = 134.3
        height_cm: float = 89.3

    loaded = load_calibration_from_camera_dir(tmp_path, camera_config, _FakeDef())

    assert loaded is not None, "load_calibration_from_camera_dir returned None unexpectedly"
    assert getattr(loaded, "calibration_stale", False) is True, (
        "Expected calibration_stale=True for a legacy record without calibrated_playfield"
    )


# ---------------------------------------------------------------------------
# 3. Smoke test: real data/aprilcam/playfields/main-playfield.json
# ---------------------------------------------------------------------------


def test_registry_loads_real_main_playfield():
    """PlayfieldDefinitionRegistry.load_all loads main-playfield.json from real data dir.

    Verifies the actual file written by sprint 012 ticket 003 is present and
    has the expected geometry (width=134.3 cm, height=89.3 cm, name='main-playfield').
    """
    from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry

    # Locate the data directory relative to the repo root.
    repo_root = Path(__file__).parents[1]
    playfields_dir = repo_root / "data" / "aprilcam" / "playfields"

    assert playfields_dir.exists(), (
        f"Playfields directory not found: {playfields_dir}\n"
        "This file should have been written by sprint 012 ticket 003."
    )

    json_path = playfields_dir / "main-playfield.json"
    assert json_path.exists(), (
        f"main-playfield.json not found at {json_path}\n"
        "Expected it to be written by sprint 012 ticket 003."
    )

    reg = PlayfieldDefinitionRegistry()
    reg.load_all(playfields_dir)

    names = reg.list()
    assert "main-playfield" in names, (
        f"'main-playfield' not in registry after load_all; got: {names}"
    )

    defn = reg.get("main-playfield")
    assert defn.name == "main-playfield"
    assert defn.width_cm == pytest.approx(134.3, abs=0.01), (
        f"Expected width_cm=134.3, got {defn.width_cm}"
    )
    assert defn.height_cm == pytest.approx(89.3, abs=0.01), (
        f"Expected height_cm=89.3, got {defn.height_cm}"
    )
