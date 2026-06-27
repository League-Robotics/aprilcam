"""Tests for the mobile-tag registry: persistence, pipeline apply, and RPC handlers.

No camera hardware or running daemon is needed — persistence uses a tmp dir, the
pipeline is constructed without start(), and the servicer is driven directly with
a mocked gRPC context.
"""

from __future__ import annotations

import threading
import types
from unittest.mock import MagicMock

from aprilcam.daemon import mobile_tags
from aprilcam.daemon.mobile_tags import MobileTag


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path):
    assert mobile_tags.load(tmp_path) == {}


def test_register_save_load_roundtrip(tmp_path):
    mobile_tags.register(tmp_path, MobileTag(100, x_mm=43.0, y_mm=-2.0, z_cm=11.8, yaw_deg=90.0, owner="r1"))
    reg = mobile_tags.load(tmp_path)
    assert set(reg) == {100}
    mt = reg[100]
    assert (mt.x_mm, mt.y_mm, mt.z_cm, mt.yaw_deg, mt.owner) == (43.0, -2.0, 11.8, 90.0, "r1")


def test_legacy_tag_heights_migrated(tmp_path):
    (tmp_path / "tags.json").write_text('{"tag_heights": {"7": 5.5}}')
    reg = mobile_tags.load(tmp_path)
    assert reg[7].z_cm == 5.5 and reg[7].x_mm == 0.0
    # Writing migrates it into the "tags" block.
    mobile_tags.save(tmp_path, reg)
    raw = (tmp_path / "tags.json").read_text()
    assert '"tags"' in raw and '"z_cm": 5.5' in raw


def test_tags_block_overrides_legacy(tmp_path):
    (tmp_path / "tags.json").write_text(
        '{"tag_heights": {"9": 3.0}, "tags": {"9": {"x_mm": 10, "z_cm": 4.0}}}'
    )
    reg = mobile_tags.load(tmp_path)
    assert reg[9].x_mm == 10.0 and reg[9].z_cm == 4.0


def test_clear_one_and_all(tmp_path):
    mobile_tags.register(tmp_path, MobileTag(1, z_cm=1.0))
    mobile_tags.register(tmp_path, MobileTag(2, z_cm=2.0))
    assert set(mobile_tags.clear(tmp_path, 1)) == {2}
    assert mobile_tags.clear(tmp_path, None) == {}


def test_corrupt_file_tolerated(tmp_path):
    (tmp_path / "tags.json").write_text("{ not json")
    assert mobile_tags.load(tmp_path) == {}


# ---------------------------------------------------------------------------
# Pipeline apply
# ---------------------------------------------------------------------------


def test_apply_mobile_registry_derives_dicts():
    from aprilcam.daemon.camera_pipeline import CameraPipeline

    p = CameraPipeline("cam", 0, types.SimpleNamespace())
    p.apply_mobile_registry({
        100: MobileTag(100, x_mm=43.0, y_mm=0.0, z_cm=11.8, yaw_deg=0.0),
        5: MobileTag(5, z_cm=0.0),  # no height -> excluded from _tag_heights
    })
    assert p._tag_heights == {100: 11.8}                       # z==0 dropped
    assert p._tag_offsets == {100: (43.0, 0.0, 0.0), 5: (0.0, 0.0, 0.0)}


# ---------------------------------------------------------------------------
# Servicer RPC handlers
# ---------------------------------------------------------------------------


def _servicer(tmp_path, cameras=None):
    from aprilcam.daemon.grpc_server import AprilCamServicer

    cfg = types.SimpleNamespace(data_dir=tmp_path)
    return AprilCamServicer(
        cameras=cameras if cameras is not None else {},
        cam_lock=threading.Lock(),
        config=cfg,
        shutdown_event=threading.Event(),
    )


def test_register_rpc_persists_and_applies(tmp_path):
    from aprilcam.proto import aprilcam_pb2

    fake_pipe = MagicMock()
    svc = _servicer(tmp_path, cameras={"cam": fake_pipe})
    req = aprilcam_pb2.RegisterMobileTagRequest(
        tag=aprilcam_pb2.MobileTagSpec(tag_id=100, x_mm=43.0, z_cm=11.8, owner="r1")
    )
    resp = svc.RegisterMobileTag(req, MagicMock())

    assert [t.tag_id for t in resp.tags] == [100]
    assert 100 in mobile_tags.load(tmp_path)            # persisted
    fake_pipe.apply_mobile_registry.assert_called_once()  # pushed to pipeline


def test_register_rpc_rejects_bad_id(tmp_path):
    from aprilcam.proto import aprilcam_pb2
    import grpc

    svc = _servicer(tmp_path)
    ctx = MagicMock()
    req = aprilcam_pb2.RegisterMobileTagRequest(tag=aprilcam_pb2.MobileTagSpec(tag_id=0))
    resp = svc.RegisterMobileTag(req, ctx)
    ctx.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)
    assert len(resp.tags) == 0


def test_list_and_clear_rpcs(tmp_path):
    from aprilcam.proto import aprilcam_pb2

    svc = _servicer(tmp_path)
    svc.RegisterMobileTag(
        aprilcam_pb2.RegisterMobileTagRequest(tag=aprilcam_pb2.MobileTagSpec(tag_id=1, z_cm=1.0)),
        MagicMock(),
    )
    svc.RegisterMobileTag(
        aprilcam_pb2.RegisterMobileTagRequest(tag=aprilcam_pb2.MobileTagSpec(tag_id=2, z_cm=2.0)),
        MagicMock(),
    )
    assert {t.tag_id for t in svc.ListMobileTags(aprilcam_pb2.Empty(), MagicMock()).tags} == {1, 2}

    r = svc.ClearMobileTags(aprilcam_pb2.ClearMobileTagsRequest(tag_id=1), MagicMock())
    assert {t.tag_id for t in r.tags} == {2}

    r = svc.ClearMobileTags(aprilcam_pb2.ClearMobileTagsRequest(all=True), MagicMock())
    assert len(r.tags) == 0


# ---------------------------------------------------------------------------
# MCP tool handlers (thin wrappers over DaemonControl)
# ---------------------------------------------------------------------------


def test_mcp_mobile_handlers(monkeypatch):
    import pytest

    pytest.importorskip("mcp")
    from aprilcam.server import mcp_server as m

    dc = MagicMock()
    dc.register_mobile_tag.return_value = [{"tag_id": 1}]
    dc.clear_mobile_tag.return_value = []
    dc.clear_mobile_tags.return_value = []
    dc.list_mobile_tags.return_value = [{"tag_id": 1}]
    monkeypatch.setattr(m, "_ensure_daemon_client", lambda: dc)

    assert m._handle_register_mobile_tag(1, 1.0, 0, 0, 0, "x")["status"] == "registered"
    assert m._handle_clear_mobile_tags(0)["status"] == "cleared_all"   # 0 -> all
    assert m._handle_clear_mobile_tags(5)["status"] == "cleared"        # one
    assert "mobile_tags" in m._handle_list_mobile_tags()

    dc.list_mobile_tags.side_effect = RuntimeError("boom")
    assert m._handle_list_mobile_tags()["error"] == "boom"
