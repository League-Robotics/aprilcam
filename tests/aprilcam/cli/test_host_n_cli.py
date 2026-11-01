"""``HOST-N`` camera addressing in the CLI camera subcommands (sprint 048,
ticket 004), plus the ``HOST-N`` address on ``camera list --all`` rows.

A fake ``Discovery`` hands out recording fake ``Daemon``s, so each test
asserts which daemon was connected and which camera string reached the
RPC. No network.
"""

from __future__ import annotations

import argparse
from typing import Optional

import pytest

from aprilcam.cli import calibrate as calibrate_cli
from aprilcam.cli import camera as camera_cli
from aprilcam.cli import flatfield as flatfield_cli
from aprilcam.cli import intrinsics as intrinsics_cli
from aprilcam.cli import locate as locate_cli
from aprilcam.cli._common import label_host
from aprilcam.client.interfaces import DaemonInfo, Discovery
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import AprilCamError, ConnectionFailed
from aprilcam.types import CameraState, ImageFrame, TagFrame

DONALD = "192.168.4.60:5280"


def _state(name: str, number: int) -> CameraState:
    return CameraState(name=name, present=True, usable=True, playfield=None,
                       calibrated=False, number=number)


class _Stop(AprilCamError):
    """Raised by a fake RPC to end a command right after it was recorded."""


class FakeDaemon(Daemon):
    def __init__(self, label: str, cameras=()) -> None:
        self.label = label
        self.cameras = tuple(cameras)
        self.rpcs: list = []

    def list_cameras(self):
        return self.cameras

    def get_frame(self, camera, *, deskewed=False, encoding="jpeg"):
        self.rpcs.append(("get_frame", camera))
        return ImageFrame(data=b"x", encoding=encoding, width=1, height=1,
                          timestamp=0.0)

    def get_tags(self, camera):
        self.rpcs.append(("get_tags", camera))
        return TagFrame(tags=(), timestamp=0.0, frame_index=0)

    def stream_tags(self, camera):
        self.rpcs.append(("stream_tags", camera))
        raise _Stop("stop")

    def close_camera(self, camera, force=False):
        self.rpcs.append(("close_camera", camera))

    def calibrate(self, camera):
        self.rpcs.append(("calibrate", camera))
        raise _Stop("stop")

    def capture_flat_field(self, camera, **kw):
        self.rpcs.append(("capture_flat_field", camera))
        raise _Stop("stop")

    def get_camera_config(self, camera):
        self.rpcs.append(("get_camera_config", camera))
        raise _Stop("stop")

    def start_intrinsics(self, camera, board=None):
        self.rpcs.append(("start_intrinsics", camera))
        raise _Stop("stop")

    def abort_intrinsics(self, camera):
        pass


class FakeDiscovery(Discovery):
    def __init__(self) -> None:
        self.local = FakeDaemon("local", [_state("brio-501", 1)])
        self.donald = FakeDaemon("donald", [_state("cam-a", 4)])
        self.buzzkill = FakeDaemon("buzzkill", [_state("cam-b", 2)])
        self.connects: list[Optional[str]] = []

    def find_all(self):
        return (
            DaemonInfo(host="127.0.0.1:5280", port=5280, is_local=True,
                       reachable=True, name="here"),
            DaemonInfo(host=DONALD, port=5280, is_local=False,
                       reachable=True, name="donald"),
            DaemonInfo(host="192.168.4.61:5280", port=5280, is_local=False,
                       reachable=True, name="buzzkill"),
        )

    def connect(self, host=None):
        self.connects.append(host)
        table = {None: self.local, DONALD: self.donald, "donald": self.donald,
                 "192.168.4.61:5280": self.buzzkill, "buzzkill": self.buzzkill}
        if host in table:
            return table[host]
        raise ConnectionFailed(str(host))


def _args(**kw) -> argparse.Namespace:
    base = dict(daemon=None, all=False, camera="donald-4", output=None,
                base64=True, encoding="jpeg", force=False, follow=False,
                json=False, verbose=0, exposure=None, frames=None,
                dry_run=False, clear=False, reference=["1:a:10", "2:b:10", "3:c:10"],
                board=None, install=False, no_gui=True)
    base.update(kw)
    return argparse.Namespace(**base)


def _call(fn, args, disc, **kw):
    return fn(args, discovery_factory=lambda: disc, **kw)


# (function, rpc name recorded on the daemon)
CASES = [
    pytest.param(camera_cli.cmd_image, "get_frame", {}, id="image"),
    pytest.param(camera_cli.cmd_tags, "get_tags", {}, id="tags"),
    pytest.param(camera_cli.cmd_tags, "stream_tags", {"follow": True}, id="tags-follow"),
    pytest.param(camera_cli.cmd_close, "close_camera", {}, id="close"),
    pytest.param(calibrate_cli.cmd_calibrate, "calibrate", {}, id="calibrate"),
    pytest.param(flatfield_cli.cmd_flatfield, "capture_flat_field", {}, id="flatfield"),
    pytest.param(locate_cli.cmd_locate, "get_camera_config", {}, id="locate"),
    pytest.param(intrinsics_cli.cmd_intrinsics, "start_intrinsics",
                 {"camera": ["donald-4"]}, id="intrinsics"),
]


@pytest.mark.parametrize("fn,rpc,extra", CASES)
def test_host_n_goes_to_the_named_daemon_with_the_number(fn, rpc, extra, capsys):
    disc = FakeDiscovery()
    _call(fn, _args(**extra), disc)
    assert (rpc, "4") in disc.donald.rpcs
    assert disc.local.rpcs == [] and disc.buzzkill.rpcs == []


@pytest.mark.parametrize("fn,rpc,extra", CASES)
def test_slug_is_unchanged_and_uses_the_flag_daemon(fn, rpc, extra, capsys):
    disc = FakeDiscovery()
    extra = dict(extra)
    extra["camera"] = ["cam-b"] if "camera" in extra else "cam-b"
    _call(fn, _args(daemon="buzzkill", **extra), disc)
    assert (rpc, "cam-b") in disc.buzzkill.rpcs


@pytest.mark.parametrize("fn,rpc,extra", CASES)
def test_conflicting_d_is_a_clean_error(fn, rpc, extra, capsys):
    disc = FakeDiscovery()
    code = _call(fn, _args(daemon="buzzkill", **extra), disc)
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "donald" in err and "buzzkill" in err
    assert all(d.rpcs == [] for d in (disc.local, disc.donald, disc.buzzkill))


@pytest.mark.parametrize("fn,rpc,extra", CASES)
def test_unknown_host_is_a_clean_error(fn, rpc, extra, capsys):
    disc = FakeDiscovery()
    extra = dict(extra)
    extra["camera"] = ["nowhere-4"] if "camera" in extra else "nowhere-4"
    code = _call(fn, _args(**extra), disc)
    assert code == 1
    assert "nowhere" in capsys.readouterr().err


def test_view_resolves_host_n(capsys):
    disc = FakeDiscovery()
    seen = []
    code = camera_cli.cmd_view(
        _args(), discovery_factory=lambda: disc,
        run_view=lambda cam, daemon: seen.append((cam, daemon)),
    )
    assert code == 0
    assert seen == [("4", disc.donald)]


def test_intrinsics_cameras_on_different_daemons_conflict(capsys):
    disc = FakeDiscovery()
    code = _call(intrinsics_cli.cmd_intrinsics,
                 _args(camera=["donald-4", "buzzkill-2"]), disc)
    assert code == 1
    assert "error:" in capsys.readouterr().err
    assert all(d.rpcs == [] for d in (disc.donald, disc.buzzkill))


def test_intrinsics_same_daemon_cameras_resolve_together(capsys):
    disc = FakeDiscovery()
    disc.donald.cameras += (_state("cam-c", 5),)
    _call(intrinsics_cli.cmd_intrinsics,
          _args(camera=["donald-4", "donald-5"]), disc)
    assert ("start_intrinsics", "4") in disc.donald.rpcs


def test_env_default_does_not_conflict_with_host_n(monkeypatch, capsys):
    """APRILCAM_DAEMON_HOST is a default, not an explicit -d: HOST-N wins.
    The CLI passes only args.daemon as target, so the env never conflicts."""
    monkeypatch.setenv("APRILCAM_DAEMON_HOST", "buzzkill")
    disc = FakeDiscovery()
    assert _call(camera_cli.cmd_image, _args(), disc) == 0
    assert ("get_frame", "4") in disc.donald.rpcs


def test_list_all_rows_show_host_n_address(capsys):
    disc = FakeDiscovery()
    code = _call(camera_cli.cmd_list, _args(all=True), disc)
    assert code == 0
    out = capsys.readouterr().out
    assert "address=donald-4" in out
    assert "address=here-1" in out


def test_list_all_nameless_daemon_has_no_address():
    assert label_host("donald (192.168.4.60:5280)") == "donald"
    assert label_host("here (local)") == "here"
    assert label_host("192.168.4.60:5280") is None
