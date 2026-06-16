"""Tests for playfield_query.load_playfield_map — the loader the daemon's
WhereIs RPC and the MCP where tool use to find the active playfield map.

Regression: after sprint 012 migrated playfield.json into playfields/, the
daemon's WhereIs still loaded the old single-file path and failed. The loader
must prefer the registry and fall back to the legacy file.
"""
import json
import types

from aprilcam.core import playfield_query as pq
from aprilcam.config import Config


def test_load_playfield_map_prefers_registry_real_data():
    # The real data dir has playfields/main-playfield.json; the legacy
    # playfield.json was migrated away.
    cfg = Config.load()
    pf = pq.load_playfield_map(cfg)
    assert pf["playfield"]["width_cm"] == 134.3
    feats = pq.iter_features(pf)
    assert len(feats) >= 20
    # The whole point the daemon needs: 'red west' resolves through this map.
    r = pq.where("red west", feats)
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "rect-west-red"


def test_load_playfield_map_falls_back_to_legacy_file(tmp_path):
    # No playfields/ dir -> registry empty -> fall back to legacy playfield.json.
    (tmp_path / "playfield.json").write_text(
        json.dumps({
            "playfield": {"width_cm": 1.0, "height_cm": 2.0, "origin": "o"},
            "dots": [{"slug": "d", "type": "dot", "color": "blue", "x": 0, "y": 0}],
        }),
        encoding="utf-8",
    )
    cfg = types.SimpleNamespace(
        playfields_dir=tmp_path / "playfields",  # does not exist
        data_dir=tmp_path,
    )
    pf = pq.load_playfield_map(cfg)
    assert pf["playfield"]["width_cm"] == 1.0
    assert pf["dots"][0]["slug"] == "d"
