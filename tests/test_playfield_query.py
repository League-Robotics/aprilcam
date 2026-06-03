"""Tests for the natural-language "where is X" playfield lookup.

Covers the shared resolver (aprilcam.core.playfield_query), the daemon
WhereIs RPC (AprilCamServicer.WhereIs) and the MCP _handle_where handler.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import grpc
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.core import playfield_query as pq


# A compact playfield map exercising every feature category.
_PLAYFIELD = {
    "playfield": {"width_cm": 134.3, "height_cm": 89.3},
    "april_tags": [
        {"slug": "apriltag-center-a1", "type": "april_tag", "id": 1,
         "cardinal": "center", "x": 0, "y": 0},
    ],
    "aruco_tags": [
        {"slug": "aruco-northwest-u1", "type": "aruco_tag", "id": 1,
         "cardinal": "northwest", "x": -67, "y": 44.65},
        {"slug": "aruco-east-u4", "type": "aruco_tag", "id": 4,
         "cardinal": "east", "x": 67, "y": 0},
    ],
    "rectangles": [
        {"slug": "rect-east-red", "type": "rectangle", "color": "red",
         "cardinal": "east", "x": 35, "y": 0, "width_cm": 5, "height_cm": 4},
        {"slug": "rect-west-red", "type": "rectangle", "color": "red",
         "cardinal": "west", "x": -35, "y": 0, "width_cm": 5, "height_cm": 4},
    ],
    "dots": [
        {"slug": "dot-northwest-orange", "type": "dot", "color": "orange",
         "cardinal": "northwest", "size": "large", "x": -50, "y": 30},
        {"slug": "dot-west-blue", "type": "dot", "color": "blue",
         "cardinal": "west", "size": "small", "x": -50, "y": 0},
    ],
}


def _features():
    return pq.iter_features(_PLAYFIELD)


# ── Resolver: keyword search ──────────────────────────────────────────────────


def test_iter_features_flattens_all_categories():
    feats = _features()
    assert len(feats) == 7
    assert {f["category"] for f in feats} == {
        "april_tags", "aruco_tags", "rectangles", "dots"
    }


def test_unique_match_returns_ok_with_location():
    r = pq.where("where is the blue dot", _features())
    assert r["status"] == "ok"
    assert len(r["matches"]) == 1
    m = r["matches"][0]
    assert m["slug"] == "dot-west-blue"
    assert m["location"] == {"x": -50, "y": 0, "units": "cm", "frame": "a1-centred"}
    assert m["record"]["color"] == "blue"


def test_cardinal_plus_color_disambiguates_dot():
    r = pq.where("where is the northwest orange dot", _features())
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "dot-northwest-orange"


def test_square_synonym_and_eastern_synonym():
    """'eastern red square' -> east + red + rectangle."""
    r = pq.where("where is the eastern red square", _features())
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "rect-east-red"


def test_type_split_lets_user_say_aruco_tag():
    r = pq.where("where is the aruco tag one", _features())
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "aruco-northwest-u1"


def test_april_tag_one_distinct_from_aruco():
    r = pq.where("where is april tag one", _features())
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "apriltag-center-a1"


def test_number_word_maps_to_id():
    assert "1" in pq.tokenize_query("tag number one")


def test_bare_tag_prefers_april_over_aruco():
    """'tag number one' means the AprilTag, not ArUco tag 1."""
    r = pq.where("where is tag number one", _features())
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "apriltag-center-a1"


def test_explicit_aruco_still_returns_aruco():
    r = pq.where("where is aruco tag one", _features())
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "aruco-northwest-u1"


def test_ambiguous_when_multiple_features_match():
    r = pq.where("where is the red square", _features())
    assert r["status"] == "ambiguous"
    slugs = {m["slug"] for m in r["matches"]}
    assert slugs == {"rect-east-red", "rect-west-red"}


def test_not_found_returns_empty():
    r = pq.where("where is the unicorn", _features())
    assert r["status"] == "not_found"
    assert r["matches"] == []


def test_live_tags_merged_into_matched_tag():
    live = {1: {"world_xy": [0.1, -0.2], "in_playfield": True}}
    r = pq.where("where is april tag one", _features(), live_tags=live)
    m = r["matches"][0]
    assert m["live_detection"] == {"world_xy": [0.1, -0.2], "in_playfield": True}


def test_live_tags_not_attached_to_non_tag():
    live = {1: {"world_xy": [0.1, -0.2], "in_playfield": True}}
    r = pq.where("where is the blue dot", _features(), live_tags=live)
    assert "live_detection" not in r["matches"][0]


# ── Daemon RPC: WhereIs ───────────────────────────────────────────────────────


def _servicer_with_playfield(tmp_path: Path, cameras=None):
    from aprilcam.config import Config
    from aprilcam.daemon.grpc_server import AprilCamServicer

    data_dir = tmp_path / "d"
    sock_dir = tmp_path / "s"
    data_dir.mkdir()
    sock_dir.mkdir()
    (data_dir / "playfield.json").write_text(json.dumps(_PLAYFIELD))

    config = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )
    return AprilCamServicer(
        cameras=cameras if cameras is not None else {},
        cam_lock=threading.Lock(),
        config=config,
        shutdown_event=threading.Event(),
    )


def test_whereis_ok(tmp_path: Path):
    from aprilcam.proto import aprilcam_pb2

    servicer = _servicer_with_playfield(tmp_path)
    resp = servicer.WhereIs(
        aprilcam_pb2.WhereRequest(query="where is the blue dot"),
        MagicMock(spec=grpc.ServicerContext),
    )
    assert resp.status == "ok"
    assert len(resp.matches) == 1
    m = resp.matches[0]
    assert m.slug == "dot-west-blue"
    assert m.has_location and m.x == -50.0 and m.y == 0.0
    assert json.loads(m.record_json)["color"] == "blue"


def test_whereis_not_found_returns_playfield_json(tmp_path: Path):
    from aprilcam.proto import aprilcam_pb2

    servicer = _servicer_with_playfield(tmp_path)
    resp = servicer.WhereIs(
        aprilcam_pb2.WhereRequest(query="where is the unicorn"),
        MagicMock(spec=grpc.ServicerContext),
    )
    assert resp.status == "not_found"
    assert json.loads(resp.playfield_json)["dots"]


def test_whereis_merges_live_tags(tmp_path: Path):
    from aprilcam.proto import aprilcam_pb2

    # Mock a pipeline whose get_current_tags returns a live detection for id 1.
    tag = aprilcam_pb2.TagMsg(id=1, wx=0.5, wy=-0.5, in_playfield=True)
    frame = aprilcam_pb2.TagFrameResponse(tags=[tag])
    pipeline = MagicMock()
    pipeline.get_current_tags.return_value = frame

    servicer = _servicer_with_playfield(tmp_path, cameras={"cam-0": pipeline})
    resp = servicer.WhereIs(
        aprilcam_pb2.WhereRequest(query="where is april tag one", cam_name="cam-0"),
        MagicMock(spec=grpc.ServicerContext),
    )
    assert resp.status == "ok"
    m = resp.matches[0]
    assert m.has_live and m.live_x == 0.5 and m.live_y == -0.5
    assert m.in_playfield


# ── MCP handler: _handle_where ────────────────────────────────────────────────


def test_mcp_handle_where_needs_resolution(monkeypatch, tmp_path: Path):
    from aprilcam.config import Config
    import aprilcam.server.mcp_server as mcp

    data_dir = tmp_path / "d"
    data_dir.mkdir()
    (data_dir / "playfield.json").write_text(json.dumps(_PLAYFIELD))

    fake_cfg = Config(data_dir=data_dir, socket_dir=tmp_path / "s",
                      daemon_pidfile=tmp_path / "s" / "p.pid")
    (tmp_path / "s").mkdir()
    monkeypatch.setattr(mcp.Config, "load", classmethod(lambda cls, *a, **k: fake_cfg))

    r = mcp._handle_where("where is the unicorn")
    assert r["status"] == "needs_resolution"
    assert "playfield" in r and "hint" in r


def test_mcp_handle_where_ok(monkeypatch, tmp_path: Path):
    from aprilcam.config import Config
    import aprilcam.server.mcp_server as mcp

    data_dir = tmp_path / "d"
    data_dir.mkdir()
    (data_dir / "playfield.json").write_text(json.dumps(_PLAYFIELD))
    (tmp_path / "s").mkdir()
    fake_cfg = Config(data_dir=data_dir, socket_dir=tmp_path / "s",
                      daemon_pidfile=tmp_path / "s" / "p.pid")
    monkeypatch.setattr(mcp.Config, "load", classmethod(lambda cls, *a, **k: fake_cfg))

    r = mcp._handle_where("where is the eastern red square")
    assert r["status"] == "ok"
    assert r["matches"][0]["slug"] == "rect-east-red"
