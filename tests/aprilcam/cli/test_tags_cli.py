"""Tests for ``aprilcam.cli.tags`` (ticket 025-005, UC-12).

Headless, matching ``test_playfield_cli.py``'s/``test_camera_cli.py``'s
established style: a fake ``Daemon``/``Discovery`` pair injected through
``cmd_register``/``cmd_clear``/``cmd_list``'s keyword seams -- no real
daemon, no real gRPC. Each CLI test module defines its own small fakes
rather than importing another module's (this file's own precedent,
matching every other ``tests/aprilcam/cli/test_*_cli.py`` module).
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import pytest

from aprilcam.cli import tags as tags_cli
from aprilcam.errors import ConnectionFailed, NotCalibrated
from aprilcam.types import MountParameters, TagFamily, TagId


@dataclass
class _FakeDaemon:
    register_tag_raises: Optional[Exception] = None
    registered: list = field(default_factory=list)
    unregister_tag_raises: Optional[Exception] = None
    unregistered: list = field(default_factory=list)
    list_tag_parameters_returns: Dict[TagId, MountParameters] = field(default_factory=dict)
    list_tag_parameters_raises: Optional[Exception] = None

    def register_tag(self, tag: TagId, params: MountParameters) -> None:
        if self.register_tag_raises is not None:
            raise self.register_tag_raises
        self.registered.append((tag, params))

    def unregister_tag(self, tag: TagId) -> None:
        if self.unregister_tag_raises is not None:
            raise self.unregister_tag_raises
        self.unregistered.append(tag)

    def list_tag_parameters(self) -> Dict[TagId, MountParameters]:
        if self.list_tag_parameters_raises is not None:
            raise self.list_tag_parameters_raises
        return self.list_tag_parameters_returns


@dataclass
class _FakeDiscovery:
    daemon: Optional[_FakeDaemon] = None
    connect_raises: Optional[Exception] = None
    connected_with: Optional[str] = None
    daemons_by_host: dict = field(default_factory=dict)
    infos: Tuple[object, ...] = ()
    find_all_raises: Optional[Exception] = None

    def connect(self, host: Optional[str] = None):
        self.connected_with = host
        if host in self.daemons_by_host:
            target = self.daemons_by_host[host]
            if isinstance(target, Exception):
                raise target
            return target
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.daemon

    def find_all(self):
        if self.find_all_raises is not None:
            raise self.find_all_raises
        return self.infos


@dataclass
class _Info:
    host: str
    is_local: bool


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        daemon=None,
        family="apriltag",
        number=7,
        x=0.0,
        y=0.0,
        z=0.0,
        yaw_deg=0.0,
        size=None,
        all=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="aprilcam")
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    tags_cli.add_subparser(subparsers)
    return top


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_converts_yaw_degrees_to_radians_and_passes_defaults(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_register(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert len(daemon.registered) == 1
    tag, params = daemon.registered[0]
    assert tag == TagId(family=TagFamily.APRILTAG, number=7)
    assert params == MountParameters(size_cm=None, mount_x=0.0, mount_y=0.0, mount_z=0.0, mount_yaw_rad=0.0)


def test_register_passes_x_y_z_yaw_and_size_through(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_register(
        _args(family="aruco", number=5, x=8.0, y=3.0, z=15.0, yaw_deg=90.0, size=12.5),
        discovery_factory=lambda: discovery,
    )

    assert code == 0
    tag, params = daemon.registered[0]
    assert tag == TagId(family=TagFamily.ARUCO, number=5)
    assert params.mount_x == 8.0
    assert params.mount_y == 3.0
    assert params.mount_z == 15.0
    assert params.mount_yaw_rad == pytest.approx(math.pi / 2)
    assert params.size_cm == 12.5

    out = capsys.readouterr().out
    assert "aruco 5" in out
    assert "x=8" in out and "y=3" in out and "z=15" in out
    assert "yaw=90deg" in out
    assert "12.5cm" in out


def test_register_all_flag_is_refused_before_any_connection(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_register(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "single-target" in capsys.readouterr().err
    assert discovery.connected_with is None
    assert daemon.registered == []


def test_register_surfaces_not_calibrated_for_nonzero_z_with_no_calibration(capsys):
    daemon = _FakeDaemon(
        register_tag_raises=NotCalibrated("cannot register mount_z: no calibration anywhere")
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_register(_args(z=20.0), discovery_factory=lambda: discovery)

    assert code == 1
    assert "no calibration" in capsys.readouterr().err


def test_register_passes_the_daemon_flag_through_to_connect():
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    tags_cli.cmd_register(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert discovery.connected_with == "pi-field.local"


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_unregisters_the_named_tag(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_clear(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert daemon.unregistered == [TagId(family=TagFamily.APRILTAG, number=7)]
    assert "cleared" in capsys.readouterr().out


def test_clear_on_an_unregistered_tag_still_succeeds_with_a_confirmation(capsys):
    """UC-12: an absent registration is a no-op, not an error --
    the fake daemon here never raises, mirroring the real service's
    own no-op-on-absent contract; the CLI must still print a
    confirmation and exit 0, matching ``cmd_annotations --clear``'s
    tone (module docstring)."""
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_clear(_args(number=999), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "cleared" in out
    assert "999" in out


def test_clear_all_flag_is_refused_before_any_connection(capsys):
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_clear(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1
    assert "single-target" in capsys.readouterr().err
    assert discovery.connected_with is None
    assert daemon.unregistered == []


def test_clear_passes_the_daemon_flag_through_to_connect():
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    tags_cli.cmd_clear(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert discovery.connected_with == "pi-field.local"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_prints_no_registered_tags_when_the_registry_is_empty(capsys):
    daemon = _FakeDaemon(list_tag_parameters_returns={})
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert "(no registered tags)" in capsys.readouterr().out


def test_list_prints_one_row_per_registration_in_cm_and_degrees(capsys):
    registrations = {
        TagId(family=TagFamily.APRILTAG, number=7): MountParameters(
            mount_x=8.0, mount_y=3.0, mount_z=15.0, mount_yaw_rad=math.pi, size_cm=10.0
        ),
        TagId(family=TagFamily.ARUCO, number=5): MountParameters(),
    }
    daemon = _FakeDaemon(list_tag_parameters_returns=registrations)
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    out = capsys.readouterr().out
    assert "apriltag" in out and "7" in out
    assert "x=8.00" in out and "y=3.00" in out and "z=15.00" in out
    assert "yaw=180.0deg" in out
    assert "10cm" in out
    assert "aruco" in out and "5" in out


def test_list_reports_a_daemon_side_error(capsys):
    daemon = _FakeDaemon(list_tag_parameters_raises=ConnectionFailed("pi-field.local", reason="refused"))
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_list(_args(), discovery_factory=lambda: discovery)

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


def test_list_all_fans_out_and_reports_one_unreachable_daemon(capsys):
    good = _FakeDaemon(
        list_tag_parameters_returns={TagId(family=TagFamily.APRILTAG, number=7): MountParameters()}
    )
    discovery = _FakeDiscovery(
        infos=(_Info(host="localhost", is_local=True), _Info(host="unreachable-host", is_local=False)),
        daemons_by_host={
            None: good,
            "unreachable-host": ConnectionFailed("unreachable-host", reason="no answer"),
        },
    )

    code = tags_cli.cmd_list(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 1  # partial failure is visible in the exit code
    out, err = capsys.readouterr()
    assert "localhost (local):" in out
    assert "apriltag" in out
    assert "unreachable-host: unreachable" in err
    assert "no answer" in err


def test_list_all_with_zero_daemons_found_is_success_not_error(capsys):
    discovery = _FakeDiscovery(infos=())

    code = tags_cli.cmd_list(_args(all=True), discovery_factory=lambda: discovery)

    assert code == 0


def test_list_passes_the_daemon_flag_through_to_connect():
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    tags_cli.cmd_list(_args(daemon="pi-field.local"), discovery_factory=lambda: discovery)

    assert discovery.connected_with == "pi-field.local"


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def test_register_is_wired_with_zero_defaults():
    parser = _build_parser()
    args = parser.parse_args(["tags", "mount", "register", "apriltag", "7"])
    assert args.func is tags_cli.cmd_register
    assert args.family == "apriltag"
    assert args.number == 7
    assert args.x == 0.0
    assert args.y == 0.0
    assert args.z == 0.0
    assert args.yaw_deg == 0.0
    assert args.size is None
    assert args.daemon is None
    assert args.all is False


def test_register_accepts_all_offset_flags():
    parser = _build_parser()
    args = parser.parse_args(
        [
            "tags", "mount", "register", "aruco", "5",
            "--x", "8.0", "--y", "3.0", "--z", "15.0",
            "--yaw-deg", "90.0", "--size", "12.5", "-d", "pi-field.local",
        ]
    )
    assert args.family == "aruco"
    assert args.number == 5
    assert args.x == 8.0
    assert args.y == 3.0
    assert args.z == 15.0
    assert args.yaw_deg == 90.0
    assert args.size == 12.5
    assert args.daemon == "pi-field.local"


def test_register_requires_family_and_number_positionals():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["tags", "mount", "register"])


def test_register_rejects_an_unknown_family():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["tags", "mount", "register", "qr", "7"])


def test_clear_is_wired_to_cmd_clear():
    parser = _build_parser()
    args = parser.parse_args(["tags", "mount", "clear", "apriltag", "7"])
    assert args.func is tags_cli.cmd_clear
    assert args.family == "apriltag"
    assert args.number == 7


def test_list_is_wired_to_cmd_list():
    parser = _build_parser()
    args = parser.parse_args(["tags", "mount", "list"])
    assert args.func is tags_cli.cmd_list
    assert args.all is False


def test_tags_with_no_subcommand_uses_the_incomplete_command_default(capsys):
    parser = _build_parser()
    args = parser.parse_args(["tags"])

    code = args.func(args)

    assert code == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_mount_with_no_subcommand_uses_the_incomplete_command_default(capsys):
    parser = _build_parser()
    args = parser.parse_args(["tags", "mount"])

    code = args.func(args)

    assert code == 1
    assert "usage" in capsys.readouterr().out.lower()
