"""Tests for ``aprilcam.cli.tags`` (ticket 025-005, UC-12; ``generate``
added by ticket 028-002, UC-21).

Headless, matching ``test_playfield_cli.py``'s/``test_camera_cli.py``'s
established style: a fake ``Daemon``/``Discovery`` pair injected through
``cmd_register``/``cmd_clear``/``cmd_list``'s keyword seams -- no real
daemon, no real gRPC. Each CLI test module defines its own small fakes
rather than importing another module's (this file's own precedent,
matching every other ``tests/aprilcam/cli/test_*_cli.py`` module).

``generate``'s own tests need no ``Discovery``/daemon fake at all --
it is purely offline (SUC-001/002/003) -- only a fake/spy
``TagGenerator`` injected through ``cmd_generate``'s
``tag_generator_factory`` seam, so these tests never import real
OpenCV rendering (ticket 028-001's own suite already covers that).
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from aprilcam.cli import tags as tags_cli
from aprilcam.daemon.interfaces import MountRegistration
from aprilcam.errors import ConnectionFailed, InvalidTagId, NotCalibrated, UnwritableOutputPath
from aprilcam.types import MountParameters, TagFamily, TagId
from aprilcam.cli._common import add_daemon_option


@dataclass
class _FakeDaemon:
    register_tag_raises: Optional[Exception] = None
    register_tag_returns: Optional[str] = None
    """Ticket 030-004, SUC-004: the daemon's optional mount_z-inert
    warning, echoed back from register_tag exactly as the real one
    would return it."""
    registered: list = field(default_factory=list)
    unregister_tag_raises: Optional[Exception] = None
    unregistered: list = field(default_factory=list)
    list_tag_parameters_returns: Dict[TagId, MountRegistration] = field(default_factory=dict)
    list_tag_parameters_raises: Optional[Exception] = None

    def register_tag(self, tag: TagId, params: MountParameters) -> Optional[str]:
        if self.register_tag_raises is not None:
            raise self.register_tag_raises
        self.registered.append((tag, params))
        return self.register_tag_returns

    def unregister_tag(self, tag: TagId) -> None:
        if self.unregister_tag_raises is not None:
            raise self.unregister_tag_raises
        self.unregistered.append(tag)

    def list_tag_parameters(self) -> Dict[TagId, MountRegistration]:
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


@dataclass
class _FakeTagGenerator:
    """A spy standing in for ``tags.generator.TagGenerator`` -- no real
    OpenCV rendering happens in this file's tests (see module
    docstring)."""

    generate_returns: Tuple[Path, ...] = ()
    generate_raises: Optional[Exception] = None
    calls: List[tuple] = field(default_factory=list)
    sheet_calls: List[tuple] = field(default_factory=list)

    def generate(self, family, ids, *, size_cm, out_dir, dpi=300):
        self.calls.append((family, ids, size_cm, out_dir, dpi))
        if self.generate_raises is not None:
            raise self.generate_raises
        return self.generate_returns

    def generate_sheet(self, family, ids, *, out_path):
        self.sheet_calls.append((family, ids, out_path))
        if self.generate_raises is not None:
            raise self.generate_raises
        return out_path


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
    add_daemon_option(top)
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    tags_cli.add_subparser(subparsers)
    return top


def _generate_args(**overrides) -> argparse.Namespace:
    base = dict(
        ids="1-10",
        aruco=True,
        output=None,
        png=False,
        size_cm=None,
        dpi=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


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


def test_register_prints_the_daemons_warning_to_stderr_but_still_exits_zero(capsys):
    """Ticket 030-004, SUC-004: a nonzero --z registered while no
    camera is located is non-fatal -- the CLI still succeeds (exit 0,
    the registration itself still happened) but prints the daemon's
    warning to stderr so an operator sees it without a second lookup."""
    daemon = _FakeDaemon(
        register_tag_returns=(
            "mount_z=20 for apriltag 7 is currently inert -- ask your operator "
            "to run `aprilcam camera locate`"
        )
    )
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_register(_args(z=20.0), discovery_factory=lambda: discovery)

    assert code == 0
    assert len(daemon.registered) == 1  # registration still happened
    out, err = capsys.readouterr()
    assert "camera locate" in err
    assert "registered" in out  # the usual success line still prints too


def test_register_prints_no_warning_when_the_daemon_reports_none(capsys):
    daemon = _FakeDaemon()  # register_tag_returns defaults to None
    discovery = _FakeDiscovery(daemon=daemon)

    code = tags_cli.cmd_register(_args(), discovery_factory=lambda: discovery)

    assert code == 0
    assert capsys.readouterr().err == ""


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
        TagId(family=TagFamily.APRILTAG, number=7): MountRegistration(
            params=MountParameters(
                mount_x=8.0, mount_y=3.0, mount_z=15.0, mount_yaw_rad=math.pi, size_cm=10.0
            ),
            mount_z_applied=False,
        ),
        TagId(family=TagFamily.ARUCO, number=5): MountRegistration(
            params=MountParameters(), mount_z_applied=True
        ),
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
        list_tag_parameters_returns={
            TagId(family=TagFamily.APRILTAG, number=7): MountRegistration(
                params=MountParameters(), mount_z_applied=True
            )
        }
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
            "-d", "pi-field.local", "tags", "mount", "register", "aruco", "5",
            "--x", "8.0", "--y", "3.0", "--z", "15.0",
            "--yaw-deg", "90.0", "--size", "12.5",
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


# ---------------------------------------------------------------------------
# generate -- _parse_ids (ticket 028-002, UC-21)
# ---------------------------------------------------------------------------


def test_parse_ids_single_values():
    assert tags_cli._parse_ids("1,2,3") == (1, 2, 3)


def test_parse_ids_a_range_is_inclusive_on_both_ends():
    assert tags_cli._parse_ids("1-10") == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)


def test_parse_ids_mixed_ranges_and_singles():
    assert tags_cli._parse_ids("1-3,55") == (1, 2, 3, 55)


def test_parse_ids_deduplicates_and_sorts():
    assert tags_cli._parse_ids("5,1,3-4,1,3") == (1, 3, 4, 5)


def test_parse_ids_tolerates_surrounding_whitespace():
    assert tags_cli._parse_ids(" 1 , 3-4 , 7 ") == (1, 3, 4, 7)


def test_parse_ids_rejects_a_non_numeric_part():
    with pytest.raises(ValueError):
        tags_cli._parse_ids("1,abc")


# ---------------------------------------------------------------------------
# generate -- cmd_generate (ticket 028-002, UC-21, SUC-001/002/003)
# ---------------------------------------------------------------------------


def test_generate_default_writes_one_pdf_sheet_with_the_whole_id_batch(capsys):
    generator = _FakeTagGenerator()

    code = tags_cli.cmd_generate(
        _generate_args(ids="1-8", output="tags.pdf"), tag_generator_factory=lambda: generator
    )

    assert code == 0
    assert generator.calls == []  # PNG path untouched
    assert generator.sheet_calls == [(TagFamily.ARUCO, tuple(range(1, 9)), Path("tags.pdf"))]
    out = capsys.readouterr().out
    assert "8" in out and "tags.pdf" in out


def test_generate_needs_no_size_for_the_pdf_path():
    generator = _FakeTagGenerator()

    code = tags_cli.cmd_generate(_generate_args(), tag_generator_factory=lambda: generator)

    assert code == 0
    assert generator.sheet_calls[0][2] == Path("tags.pdf")  # default output


def test_generate_without_aruco_flag_is_apriltag():
    generator = _FakeTagGenerator()

    tags_cli.cmd_generate(
        _generate_args(aruco=False, ids="0-3,7"), tag_generator_factory=lambda: generator
    )

    family, ids, _ = generator.sheet_calls[0]
    assert family == TagFamily.APRILTAG
    assert ids == (0, 1, 2, 3, 7)


def test_generate_calls_tag_generator_exactly_once_for_the_whole_batch():
    """SUC-002's 'nothing written for any id in the batch' guarantee is
    ``TagGenerator``'s, not the CLI's -- the CLI must not loop per-id."""
    generator = _FakeTagGenerator()

    tags_cli.cmd_generate(_generate_args(ids="1-3,55"), tag_generator_factory=lambda: generator)

    assert len(generator.sheet_calls) == 1
    assert generator.sheet_calls[0][1] == (1, 2, 3, 55)


def test_generate_png_routes_to_per_id_images_with_documented_defaults(capsys):
    generator = _FakeTagGenerator(generate_returns=(Path("a.png"), Path("b.png"), Path("c.png")))

    code = tags_cli.cmd_generate(
        _generate_args(ids="1-3", png=True), tag_generator_factory=lambda: generator
    )

    assert code == 0
    assert generator.sheet_calls == []
    assert generator.calls == [
        (TagFamily.ARUCO, (1, 2, 3), tags_cli.DEFAULT_PNG_SIZE_CM, Path("tags"), 300)
    ]
    assert "3" in capsys.readouterr().out


def test_generate_png_honours_size_cm_dpi_and_output_dir():
    generator = _FakeTagGenerator()

    tags_cli.cmd_generate(
        _generate_args(png=True, size_cm=8.0, dpi=600, output="out-dir"),
        tag_generator_factory=lambda: generator,
    )

    assert generator.calls[0][2:] == (8.0, Path("out-dir"), 600)


@pytest.mark.parametrize("override", [{"size_cm": 8.0}, {"dpi": 600}])
def test_generate_refuses_size_or_dpi_without_png(override, capsys):
    generator = _FakeTagGenerator()

    code = tags_cli.cmd_generate(
        _generate_args(**override), tag_generator_factory=lambda: generator
    )

    assert code == 1
    assert "--png" in capsys.readouterr().err
    assert generator.sheet_calls == [] and generator.calls == []


def test_generate_surfaces_invalid_tag_id_via_report_error(capsys):
    """SUC-002: an out-of-range id surfaces ``InvalidTagId`` through
    ``report_error`` -- stderr, non-zero exit."""
    generator = _FakeTagGenerator(
        generate_raises=InvalidTagId("aruco", 55, range(0, 50))
    )

    code = tags_cli.cmd_generate(
        _generate_args(ids="1-3,55"), tag_generator_factory=lambda: generator
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "55" in err
    assert "outside valid range" in err


def test_generate_surfaces_unwritable_output_path_via_report_error(capsys):
    generator = _FakeTagGenerator(
        generate_raises=UnwritableOutputPath(Path("/no/such/writable/tags.pdf"))
    )

    code = tags_cli.cmd_generate(
        _generate_args(output="/no/such/writable/tags.pdf"),
        tag_generator_factory=lambda: generator,
    )

    assert code == 1
    assert "/no/such/writable/tags.pdf" in capsys.readouterr().err


def test_generate_rejects_a_malformed_ids_spec_without_a_traceback(capsys):
    generator = _FakeTagGenerator()

    code = tags_cli.cmd_generate(
        _generate_args(ids="1,abc"), tag_generator_factory=lambda: generator
    )

    assert code == 1
    assert "ids" in capsys.readouterr().err
    assert generator.calls == [] and generator.sheet_calls == []


# ---------------------------------------------------------------------------
# generate -- argparse wiring (UC-21)
# ---------------------------------------------------------------------------


def test_generate_is_wired_with_v1_shaped_arguments():
    parser = _build_parser()
    args = parser.parse_args(["tags", "generate", "1-8", "-A", "-o", "tags.pdf"])
    assert args.func is tags_cli.cmd_generate
    assert args.ids == "1-8"
    assert args.aruco is True
    assert args.output == "tags.pdf"
    assert args.png is False
    assert args.size_cm is None


def test_generate_needs_only_the_ids():
    args = _build_parser().parse_args(["tags", "generate", "0-3,7"])
    assert args.aruco is False
    assert args.output is None
    assert args.size_cm is None and args.dpi is None


def test_generate_png_flags_parse():
    args = _build_parser().parse_args(
        ["tags", "generate", "1", "--png", "--size-cm", "5", "--dpi", "600", "--aruco"]
    )
    assert args.png is True and args.size_cm == 5.0 and args.dpi == 600


def test_generate_requires_ids():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["tags", "generate"])


def test_generate_has_no_daemon_or_all_option():
    """``generate`` is purely offline -- it never gets ``-d``/``--daemon``
    or ``--all`` (module docstring, ticket 028-002)."""
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["tags", "generate", "1", "--daemon", "pi-field.local"])
    with pytest.raises(SystemExit):
        parser.parse_args(["tags", "generate", "1", "--all"])


def test_tags_help_shows_generate_alongside_mount(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["tags", "--help"])
    out = capsys.readouterr().out
    assert "mount" in out
    assert "generate" in out


def test_generate_help_does_not_crash_and_documents_its_flags(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["tags", "generate", "--help"])
    out = capsys.readouterr().out
    assert "IDS" in out
    assert "--aruco" in out
    assert "--output" in out
    assert "--png" in out
    assert "--size-cm" in out
    assert "--dpi" in out
    assert "--daemon" not in out
    assert "--all" not in out
