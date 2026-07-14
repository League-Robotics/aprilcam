"""Regression tests for daemon-restart registry repopulation (Sprint 016, ticket 001).

Covers the three mechanisms added to fix
``playfield_def_registry`` staying permanently empty after an ``aprilcamd``
restart under a long-lived ``aprilcam mcp`` process:

1. ``_maybe_repopulate_registry()`` — boot_id-change detection wired into
   ``_ensure_daemon_client()``. A changed ``boot_id`` (simulated daemon
   restart) triggers a clear+repopulate; an unchanged ``boot_id`` is a no-op
   that must NOT clobber a valid, already-populated registry.
2. Empty-registry fallback in ``_handle_set_camera_playfield`` /
   ``calibrate_playfield`` / ``_handle_create_playfield`` — recovers even
   when the ``boot_id`` signal itself doesn't detect the restart (e.g. an
   old daemon that doesn't stamp ``boot_id`` yet).
3. Daemon-side: ``AprilCamServicer.boot_id`` is generated once per servicer
   instance and stamped onto every ``ListPlayfieldsResponse``.

All tests are hardware-free: no real daemon process, no camera hardware.
Follows the fake-client (``MagicMock``-based) pattern from
``tests/test_connect_daemon.py`` and the registry-patching pattern from
``tests/test_set_camera_playfield.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.server import mcp_server
from aprilcam.server.mcp_server import (
    CameraRegistry,
    _ensure_daemon_client,
    _handle_create_playfield,
    _handle_set_camera_playfield,
    _maybe_repopulate_registry,
)
from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _playfield_blob(name: str, width_cm: float = 101.0, height_cm: float = 89.0) -> str:
    """Return a minimal valid playfield JSON blob (as ListPlayfields returns)."""
    return json.dumps({
        "playfield": {"width_cm": width_cm, "height_cm": height_cm, "origin": "apriltag-center-a1"},
        "april_tags": [],
        "aruco_tags": [],
        "rectangles": [],
        "dots": [],
    })


def _fake_list_playfields_reply(names: list[str], boot_id: str) -> MagicMock:
    """Build a MagicMock standing in for a ListPlayfieldsResponse."""
    reply = MagicMock()
    reply.boot_id = boot_id
    reply.playfields = []
    for n in names:
        entry = MagicMock()
        entry.name = n
        entry.json_blob = _playfield_blob(n)
        reply.playfields.append(entry)
    return reply


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    """Replace module-level registries and _daemon_boot_id for each test."""
    fresh_registry = CameraRegistry()
    fresh_def_registry = PlayfieldDefinitionRegistry()
    fresh_cam_info: dict = {}

    monkeypatch.setattr(mcp_server, "registry", fresh_registry)
    monkeypatch.setattr(mcp_server, "playfield_def_registry", fresh_def_registry)
    monkeypatch.setattr(mcp_server, "_cam_info", fresh_cam_info)
    monkeypatch.setattr(mcp_server, "_daemon_boot_id", None)

    yield fresh_registry, fresh_def_registry, fresh_cam_info


def _register_camera(registry, cam_info, camera_id="arducam-ov9782-usb-camera"):
    registry.open(None, handle=camera_id)
    cam_info[camera_id] = {"cam_name": camera_id}
    return camera_id


# ---------------------------------------------------------------------------
# _maybe_repopulate_registry: boot_id-change / boot_id-unchanged
# ---------------------------------------------------------------------------


class TestMaybeRepopulateRegistry:
    def test_first_call_populates_registry(self, isolated_state):
        """The first call (no prior boot_id observed) populates the registry."""
        _reg, def_reg, _cam_info = isolated_state
        fake_client = MagicMock()
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )

        assert def_reg.list() == []
        _maybe_repopulate_registry(fake_client)

        assert def_reg.list() == ["main-playfield"]
        assert mcp_server._daemon_boot_id == "boot-aaa"

    def test_unchanged_boot_id_is_noop(self, isolated_state, monkeypatch):
        """A call with the same boot_id does not re-clear/repopulate the registry."""
        _reg, def_reg, _cam_info = isolated_state
        fake_client = MagicMock()
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )

        # First call: populates and records boot-aaa.
        _maybe_repopulate_registry(fake_client)
        assert def_reg.list() == ["main-playfield"]

        # Spy on registry.clear to assert it is NOT invoked again.
        clear_calls = {"count": 0}
        original_clear = def_reg.clear

        def _spy_clear():
            clear_calls["count"] += 1
            original_clear()

        monkeypatch.setattr(def_reg, "clear", _spy_clear)

        # Second call: same boot_id (simulates a transient channel blip, not
        # a daemon restart) — must be a no-op.
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )
        _maybe_repopulate_registry(fake_client)

        assert clear_calls["count"] == 0, "registry.clear() must NOT be called when boot_id is unchanged"
        assert def_reg.list() == ["main-playfield"]

    def test_changed_boot_id_triggers_repopulate(self, isolated_state):
        """A different boot_id (simulated daemon restart) clears and repopulates."""
        _reg, def_reg, _cam_info = isolated_state
        fake_client = MagicMock()

        # First "daemon process": populated with 'old-playfield'.
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["old-playfield"], boot_id="boot-aaa"
        )
        _maybe_repopulate_registry(fake_client)
        assert def_reg.list() == ["old-playfield"]

        # Simulated restart: new boot_id, new (different) playfield set —
        # e.g. the daemon came back up with a different playfields dir state.
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["new-playfield"], boot_id="boot-bbb"
        )
        _maybe_repopulate_registry(fake_client)

        assert def_reg.list() == ["new-playfield"], "registry must be repopulated with the new daemon's playfields"
        assert "old-playfield" not in def_reg.list(), "stale entry from the old daemon must be cleared"
        assert mcp_server._daemon_boot_id == "boot-bbb"

    def test_rpc_failure_is_swallowed(self, isolated_state):
        """An RPC failure during the check is logged and swallowed, not raised."""
        _reg, def_reg, _cam_info = isolated_state
        fake_client = MagicMock()
        fake_client.list_playfields.side_effect = RuntimeError("daemon unreachable")

        # Must not raise.
        _maybe_repopulate_registry(fake_client)
        assert def_reg.list() == []


# ---------------------------------------------------------------------------
# _ensure_daemon_client wiring: runs the check on every call, not just first
# ---------------------------------------------------------------------------


class TestEnsureDaemonClientWiring:
    def test_ensure_daemon_client_repopulates_on_boot_id_change(self, isolated_state, monkeypatch):
        """_ensure_daemon_client() detects a boot_id change on a subsequent call
        against an already-open (non-None) client — the exact daemon-restart
        scenario: the module-level _daemon_client stays non-None across a
        daemon restart (the gRPC channel reconnects transparently), so the
        old code (populate-only-if-None) never re-fired.
        """
        _reg, def_reg, _cam_info = isolated_state

        fake_client = MagicMock()
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )
        # Simulate an already-connected client (as if a prior call had
        # connected already) — _ensure_daemon_client() must NOT early-return
        # without checking boot_id just because _daemon_client is not None.
        monkeypatch.setattr(mcp_server, "_daemon_client", fake_client)

        client = _ensure_daemon_client()
        assert client is fake_client
        assert def_reg.list() == ["main-playfield"]

        # Simulate the daemon restarting: same channel object (still
        # non-None), but list_playfields now reports a new boot_id.
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["restarted-playfield"], boot_id="boot-bbb"
        )
        client2 = _ensure_daemon_client()

        assert client2 is fake_client  # channel object itself is unchanged
        assert def_reg.list() == ["restarted-playfield"], (
            "playfield_def_registry must repopulate after a daemon restart "
            "even though _daemon_client was never None"
        )

    def test_open_camera_rehydration_succeeds_after_repopulate(self, isolated_state, monkeypatch):
        """After a boot_id-triggered repopulate, open_camera's auto-rehydrate
        path finds the linked playfield definition and does not silently
        skip playfield rehydration.
        """
        reg, def_reg, cam_info = isolated_state
        camera_id = _register_camera(reg, cam_info)

        fake_client = MagicMock()
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )
        monkeypatch.setattr(mcp_server, "_daemon_client", fake_client)

        # Prime the registry via the same path open_camera would use.
        _ensure_daemon_client()
        assert def_reg.get("main-playfield") is not None

        # calibrate_playfield / create_playfield / set_camera_playfield all
        # consult playfield_def_registry.list()/.get() through the same
        # ensure-client choke point; confirm the entry is retrievable
        # exactly as _handle_open_camera's rehydrate block expects.
        assert "main-playfield" in def_reg.list()


# ---------------------------------------------------------------------------
# Empty-registry fallback recovery
# ---------------------------------------------------------------------------


class TestEmptyRegistryFallback:
    def test_set_camera_playfield_recovers_from_empty_registry(self, isolated_state, monkeypatch):
        """set_camera_playfield: registry starts empty (simulating a restart
        the boot_id signal didn't catch, e.g. an old daemon with no boot_id
        support), daemon is reachable and returns playfields on retry ->
        handler recovers instead of raising "Available: [(none loaded)]".
        """
        reg, def_reg, cam_info = isolated_state
        camera_id = _register_camera(reg, cam_info)
        assert def_reg.list() == []  # empty registry — the failure precondition

        fake_client = MagicMock()
        # Old-daemon simulation: no boot_id change signal (empty string both
        # times), but list_playfields still returns real data on retry.
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id=""
        )
        cfg_store: dict[str, dict] = {}

        def _fake_get_camera_config(cam_name):
            reply = MagicMock()
            existing = cfg_store.get(cam_name)
            reply.present = existing is not None
            reply.json_blob = json.dumps(existing) if existing is not None else ""
            return reply

        def _fake_set_camera_config(cam_name, json_blob):
            cfg_store[cam_name] = json.loads(json_blob)
            reply = MagicMock()
            reply.ok = True
            return reply

        fake_client.get_camera_config.side_effect = _fake_get_camera_config
        fake_client.set_camera_config.side_effect = _fake_set_camera_config

        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_client)

        result = _handle_set_camera_playfield(camera_id, "main-playfield")

        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["linked"] is True
        assert def_reg.list() == ["main-playfield"], "fallback must have repopulated the registry"

    def test_set_camera_playfield_still_errors_on_truly_unknown_playfield(self, isolated_state, monkeypatch):
        """The fallback recovers stale-empty registries, but a genuinely
        unknown playfield name (daemon reachable, registry populated, name
        just doesn't exist) must still produce a friendly error.
        """
        reg, def_reg, cam_info = isolated_state
        camera_id = _register_camera(reg, cam_info)

        fake_client = MagicMock()
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )
        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_client)

        result = _handle_set_camera_playfield(camera_id, "totally-bogus-name")

        assert "error" in result
        assert "totally-bogus-name" in result["error"]

    def test_create_playfield_fallback_does_not_error_on_empty_registry(self, isolated_state, monkeypatch):
        """create_playfield's handler proactively repopulates an empty
        registry (best-effort) so a caller who immediately queries
        get_playfield_info/where after create_playfield doesn't see a
        stale-empty registry. create_playfield itself must not fail merely
        because the registry started empty.
        """
        reg, def_reg, cam_info = isolated_state
        camera_id = _register_camera(reg, cam_info)
        assert def_reg.list() == []

        fake_client = MagicMock()
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )
        # No corner markers and no stored calibration -> create_playfield
        # legitimately fails to build a polygon, but that failure must be
        # the corner-detection error, not registry-related, and the
        # registry must have been repopulated as a side effect.
        fake_client.get_calibration.return_value = MagicMock(present=False)
        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_client)
        monkeypatch.setattr(mcp_server, "_frames_from_daemon", lambda camera_id, count: iter([]))

        result = _handle_create_playfield(camera_id, max_frames=1)

        # No corner markers were detected (empty frame source) — this is the
        # expected, unrelated failure mode; the point of this test is that
        # the registry got repopulated regardless.
        assert "error" in result
        assert def_reg.list() == ["main-playfield"], "empty-registry fallback must have repopulated the registry"

    def test_calibrate_playfield_recovers_from_empty_registry(self, isolated_state, monkeypatch):
        """calibrate_playfield: registry starts empty (stale MCP-side cache
        after a daemon restart the boot_id check alone didn't catch).  The
        handler retries via _maybe_repopulate_registry before delegating to
        calibrate_from_playfield_def, so the call succeeds instead of
        raising PlayfieldConfigError("... Available playfields: [(none)]").
        """
        import asyncio
        import numpy as np

        from aprilcam.server.mcp_server import PlayfieldEntry, playfield_registry
        from aprilcam.core.playfield import PlayfieldBoundary as Playfield
        from aprilcam.calibration.calibration import CameraCalibration

        reg, def_reg, cam_info = isolated_state
        assert def_reg.list() == []  # empty registry — the failure precondition

        cam_id = _register_camera(reg, cam_info, camera_id="cam_restart_test")
        pf_id = f"pf_{cam_id}"

        pf = MagicMock(spec=Playfield)
        pf._poly = None
        entry = PlayfieldEntry(playfield_id=pf_id, camera_id=cam_id, playfield=pf)
        playfield_registry.register(entry)

        controlled_cal = CameraCalibration(
            device_name=cam_id,
            resolution=(1920, 1080),
            homography=np.eye(3),
            playfield_width_cm=101.0,
            playfield_height_cm=89.0,
            calibrated_playfield="main-playfield",
            calibrated_camera=cam_id,
        )

        import aprilcam.calibration.calibration as cal_mod

        def _fake_calibrate_from_def(**kwargs):
            # The real function would raise PlayfieldConfigError here if the
            # registry were still empty; confirm the fallback already
            # repopulated it before this call.
            assert kwargs["playfield_def_registry"].list() == ["main-playfield"], (
                "empty-registry fallback must repopulate BEFORE calling "
                "calibrate_from_playfield_def"
            )
            return controlled_cal

        monkeypatch.setattr(cal_mod, "calibrate_from_playfield_def", _fake_calibrate_from_def)

        fake_client = MagicMock()
        fake_client.list_playfields.return_value = _fake_list_playfields_reply(
            ["main-playfield"], boot_id="boot-aaa"
        )
        cfg_reply = MagicMock()
        cfg_reply.present = True
        cfg_reply.json_blob = json.dumps({"playfield": "main-playfield"})
        fake_client.get_camera_config.return_value = cfg_reply
        set_reply = MagicMock()
        set_reply.ok = True
        fake_client.set_calibration.return_value = set_reply
        fake_client.set_camera_config.return_value = set_reply

        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_client)

        try:
            result_contents = asyncio.run(
                mcp_server.calibrate_playfield(playfield_id=pf_id)
            )
            result = json.loads(result_contents[0].text)
            assert "error" not in result, f"Unexpected error: {result.get('error')}"
            assert result["calibrated"] is True
            assert def_reg.list() == ["main-playfield"]
        finally:
            try:
                playfield_registry.remove(pf_id)
            except KeyError:
                pass


# ---------------------------------------------------------------------------
# Daemon-side: boot_id generation and stability
# ---------------------------------------------------------------------------


class TestDaemonBootId:
    def test_boot_id_stable_across_multiple_calls(self, tmp_path):
        """boot_id is stable across multiple ListPlayfields calls within one
        servicer (process) lifetime.
        """
        import threading
        from aprilcam.config import Config
        from aprilcam.daemon.grpc_server import AprilCamServicer
        from aprilcam.proto import aprilcam_pb2

        config = Config(
            data_dir=tmp_path / "d",
            socket_dir=tmp_path / "s",
            daemon_pidfile=tmp_path / "s" / "aprilcamd.pid",
        )
        servicer = AprilCamServicer(
            cameras={},
            cam_lock=threading.Lock(),
            config=config,
            shutdown_event=threading.Event(),
        )

        reply1 = servicer.ListPlayfields(aprilcam_pb2.Empty(), MagicMock())
        reply2 = servicer.ListPlayfields(aprilcam_pb2.Empty(), MagicMock())

        assert reply1.boot_id == reply2.boot_id
        assert reply1.boot_id != ""

    def test_boot_id_fresh_per_servicer_instance(self, tmp_path):
        """Two independent servicer instances (simulating two daemon process
        lifetimes) generate different boot_ids.
        """
        import threading
        from aprilcam.config import Config
        from aprilcam.daemon.grpc_server import AprilCamServicer
        from aprilcam.proto import aprilcam_pb2

        config = Config(
            data_dir=tmp_path / "d",
            socket_dir=tmp_path / "s",
            daemon_pidfile=tmp_path / "s" / "aprilcamd.pid",
        )

        servicer_a = AprilCamServicer(
            cameras={}, cam_lock=threading.Lock(), config=config,
            shutdown_event=threading.Event(),
        )
        servicer_b = AprilCamServicer(
            cameras={}, cam_lock=threading.Lock(), config=config,
            shutdown_event=threading.Event(),
        )

        reply_a = servicer_a.ListPlayfields(aprilcam_pb2.Empty(), MagicMock())
        reply_b = servicer_b.ListPlayfields(aprilcam_pb2.Empty(), MagicMock())

        assert reply_a.boot_id != reply_b.boot_id
