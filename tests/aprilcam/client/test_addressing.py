"""Tests for ``aprilcam.client.addressing`` (sprint 048, ticket 003).

A fake ``Discovery`` and fake ``Daemon``s; no network.
"""

from __future__ import annotations

from typing import Optional

import pytest

from aprilcam.client.addressing import ResolvedCamera, resolve_camera
from aprilcam.client.interfaces import DaemonInfo, Discovery
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import ConflictingTarget, ConnectionFailed, UnknownHost
from aprilcam.types import CameraState


def _state(name: str, number: int) -> CameraState:
    return CameraState(
        name=name, present=True, usable=True, playfield=None,
        calibrated=False, number=number,
    )


class FakeDaemon(Daemon):
    def __init__(self, cameras=(), fail: bool = False) -> None:
        self.cameras = tuple(cameras)
        self.fail = fail
        self.calls = 0

    def list_cameras(self):
        self.calls += 1
        if self.fail:
            raise ConnectionFailed("fake")
        return self.cameras


class FakeDiscovery(Discovery):
    def __init__(self, infos=(), daemons=None) -> None:
        self.infos = tuple(infos)
        self.daemons = daemons or {}
        self.connects: list[Optional[str]] = []

    def find_all(self):
        return self.infos

    def connect_local(self):
        self.connects.append("<local>")
        return self.daemons[None]

    def connect(self, host=None):
        self.connects.append(host)
        if host in self.daemons:
            return self.daemons[host]
        return FakeDaemon(fail=True)


def _info(name: str, ip: str, local: bool = False) -> DaemonInfo:
    return DaemonInfo(host=f"{ip}:5280", port=5280, is_local=local,
                      reachable=True, name=name)


def _disc(local_cams=()):
    local = FakeDaemon(local_cams)
    return FakeDiscovery(
        [_info("here", "127.0.0.1", True), _info("donald", "192.168.4.60"),
         _info("buzzkill", "192.168.4.61")],
        {None: local, **{t: FakeDaemon() for t in (
            "buzzkill", "donald", "192.168.4.60", "192.168.4.60:5280")}},
    )


def test_host_n_resolves_remote_daemon_and_number():
    r = resolve_camera("donald-4", discovery=_disc())
    assert r == ResolvedCamera(host="192.168.4.60:5280", camera="4")


def test_local_hit_gives_host_none():
    assert resolve_camera("here-2", discovery=_disc()) == ResolvedCamera(
        host=None, camera="2", local=True)


def test_case_and_domain_normalize():
    d = _disc()
    want = ResolvedCamera(host="192.168.4.60:5280", camera="4")
    assert resolve_camera("Donald-4", discovery=d) == want
    assert resolve_camera("DONALD.local-4", discovery=d) == want


def test_slug_stays_slug_even_with_daemon_named_brio():
    local = FakeDaemon([_state("brio-501", 1)])
    d = FakeDiscovery([_info("brio", "192.168.4.70")], {None: local})
    assert resolve_camera("brio-501", discovery=d) == ResolvedCamera(
        host=None, camera="brio-501")


def test_hyphenless_and_number_cause_no_rpc():
    d = _disc()
    for arg in ("4", "brio"):
        assert resolve_camera(arg, discovery=d).camera == arg
    assert d.connects == []


def test_slug_with_hyphens_but_no_digits_no_rpc():
    d = _disc()
    resolve_camera("hd-pro-webcam", discovery=d)
    assert d.connects == []


def test_conflicting_target():
    with pytest.raises(ConflictingTarget):
        resolve_camera("donald-4", discovery=_disc(), target="buzzkill")


def test_same_target_by_name_or_address_accepted():
    d = _disc()
    want = ResolvedCamera(host="192.168.4.60:5280", camera="4")
    assert resolve_camera("donald-4", discovery=d, target="donald") == want
    assert resolve_camera("donald-4", discovery=d, target="192.168.4.60") == want
    assert resolve_camera(
        "donald-4", discovery=d, target="192.168.4.60:5280") == want


def test_slug_match_on_explicit_target_is_never_a_conflict():
    far = FakeDaemon([_state("brio-501", 1)])
    d = FakeDiscovery([_info("brio", "1.2.3.4")], {"buzzkill": far})
    assert resolve_camera("brio-501", discovery=d, target="buzzkill").camera == "brio-501"


def test_unknown_host_lists_daemons():
    with pytest.raises(UnknownHost) as ei:
        resolve_camera("nowhere-4", discovery=_disc())
    assert "donald" in str(ei.value) and "buzzkill" in str(ei.value)


def test_direct_connect_fallback():
    d = FakeDiscovery([], {None: FakeDaemon(), "10.0.0.9": FakeDaemon(), "10.0.0.8": FakeDaemon()})
    assert resolve_camera("10.0.0.9-3", discovery=d) == ResolvedCamera(
        host="10.0.0.9", camera="3")


def test_direct_connect_with_different_target_is_conflict():
    d = FakeDiscovery([], {None: FakeDaemon(), "10.0.0.9": FakeDaemon(), "10.0.0.8": FakeDaemon()})
    with pytest.raises(ConflictingTarget):
        resolve_camera("10.0.0.9-3", discovery=d, target="10.0.0.8")


def test_unreachable_target_passes_through():
    d = FakeDiscovery([_info("donald", "1.1.1.1")], {None: FakeDaemon(fail=True)})
    assert resolve_camera("donald-4", discovery=d) == ResolvedCamera(
        host=None, camera="donald-4")


def test_default_target_never_conflicts_with_host_n():
    """APRILCAM_DAEMON_HOST / `aprilcam mcp -d HOST` are defaults: HOST-N
    overrides them. Only an explicit target conflicts."""
    r = resolve_camera("donald-4", discovery=_disc(), default_target="buzzkill")
    assert r == ResolvedCamera(host="192.168.4.60:5280", camera="4")


def test_default_target_is_the_step_one_lookup_daemon():
    buzz = FakeDaemon([_state("donald-4", 7)])
    d = FakeDiscovery([_info("donald", "192.168.4.60")], {"buzzkill": buzz})
    r = resolve_camera("donald-4", discovery=d, default_target="buzzkill")
    assert r == ResolvedCamera(host="buzzkill", camera="donald-4")
    assert d.connects == ["buzzkill"]


def test_default_target_used_for_pass_through():
    d = _disc()
    assert resolve_camera("4", discovery=d, default_target="buzzkill") == (
        ResolvedCamera(host="buzzkill", camera="4"))


def test_explicit_target_wins_over_default_and_still_conflicts():
    with pytest.raises(ConflictingTarget):
        resolve_camera("donald-4", discovery=_disc(), target="buzzkill",
                       default_target="donald")


def test_local_hit_is_flagged_local_with_a_default_pointing_elsewhere():
    r = resolve_camera("here-2", discovery=_disc(), default_target="buzzkill")
    assert r.local is True and r.camera == "2"


def test_remote_hit_is_not_local():
    assert resolve_camera("donald-4", discovery=_disc()).local is False
