"""Tests for ``aprilcam.cli.locate`` (sprint 029 ticket 003, SUC-003).

Headless, matching ``test_calibrate_cli.py``/``test_camera_cli.py``'s
established style: a fake ``Daemon``/``Discovery``/``ConfigLoader``
triple injected through ``cmd_locate``'s keyword seams -- no real
daemon, no vision, no real ``config_dir`` (a ``_FakeDirectories``
pointed at ``tmp_path`` stands in for the real system roots, the same
role ``config.directories.Directories(config_dir=tmp_path / ...)``
plays in ``tests/aprilcam/config/test_loader.py``). ``cmd_locate`` is a
pure RPC/orchestration shell around a client-side pure solve (module
docstring), so every test here is about argv shaping, precondition
ordering, and result/error presentation -- never about the solver's
math itself (``tests/aprilcam/calibration/test_geometry.py`` covers
that; the "real fixture" test below only checks ``cmd_locate`` reports
and applies whatever that already-tested solver returns).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from aprilcam.cli import camera as camera_cli
from aprilcam.cli import daemon as daemon_cli
from aprilcam.cli import locate as locate_cli
from aprilcam.cli import playfield as playfield_cli
from aprilcam.config.interfaces import CameraConfig, CameraPosition
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.daemon.interfaces import MountRegistration
from aprilcam.errors import ConnectionFailed, NoPlayfieldLinked, UnknownCamera
from aprilcam.playfield.playfield import Playfield
from aprilcam.types import MountParameters, TagFamily, TagId, TagRecord, WorldPoint

CAMERA = "arducam-ov9782-usb-camera"
PLAYFIELD = "main-playfield"

# The real fixture (sprint.md Problem section; ticket 002's own pinned
# regression): tag 15 on the NE orange dot at 17.2 cm, tag 14 on the SW
# orange dot at 15.0 cm.
_REAL_FIXTURE_DOTS = (
    PlayfieldFeatureEntry(slug="dot-northeast-orange", x=50.0, y=30.0),
    PlayfieldFeatureEntry(slug="dot-southwest-orange", x=-50.0, y=-30.0),
)
_TAG_15 = TagId(TagFamily.APRILTAG, 15)
_TAG_14 = TagId(TagFamily.APRILTAG, 14)
_TAG_15_OBSERVED = WorldPoint(x=57.38, y=34.90)
_TAG_14_OBSERVED = WorldPoint(x=-57.15, y=-33.40)


def _forward_apparent(
    true: WorldPoint, height_cm: float, nadir: WorldPoint, camera_height_cm: float
) -> WorldPoint:
    """The forward relation ``apparent = C + k*(true - C)``, ``k = H /
    (H - h)`` -- copied from ``tests/aprilcam/calibration/test_geometry.py``'s
    own helper of the same name, used here only to build a synthetic
    near-collinear (ill-conditioned) fixture, not to re-verify the
    solver's math."""
    k = camera_height_cm / (camera_height_cm - height_cm)
    return WorldPoint(x=nadir.x + k * (true.x - nadir.x), y=nadir.y + k * (true.y - nadir.y))


def _record(tag: TagId, world: WorldPoint) -> TagRecord:
    return TagRecord(
        tag=tag, world=world, heading_rad=None, world_velocity=None, speed=None, timestamp=0.0
    )


def _camera_config(**overrides) -> CameraConfig:
    base = dict(
        name=CAMERA,
        playfield=PLAYFIELD,
        settings={"exposure": "100"},
        camera_position=None,
    )
    base.update(overrides)
    return CameraConfig(**base)


def _playfield(dots: Tuple[PlayfieldFeatureEntry, ...] = _REAL_FIXTURE_DOTS) -> Playfield:
    definition = PlayfieldDefinition(name=PLAYFIELD, width_cm=134.3, height_cm=89.3, dots=dots)
    return Playfield(name=PLAYFIELD, definition=definition, camera_configs=())


@dataclass
class _FakeDaemon:
    camera_config: Optional[CameraConfig] = None
    get_camera_config_raises: Optional[Exception] = None
    requested_camera_config: Optional[str] = None

    playfield: Optional[Playfield] = None
    get_playfield_raises: Optional[Exception] = None
    get_playfield_called: bool = False
    requested_playfield: Optional[str] = None

    mount_parameters: Dict[TagId, MountRegistration] = field(default_factory=dict)
    list_tag_parameters_raises: Optional[Exception] = None
    list_tag_parameters_called: bool = False

    tag_records: Dict[TagId, TagRecord] = field(default_factory=dict)
    get_tag_raises: Optional[Exception] = None
    get_tag_calls: List[Tuple[str, TagId]] = field(default_factory=list)

    set_camera_config_raises: Optional[Exception] = None
    pushed_camera_config: Optional[CameraConfig] = None

    def get_camera_config(self, camera: str) -> CameraConfig:
        self.requested_camera_config = camera
        if self.get_camera_config_raises is not None:
            raise self.get_camera_config_raises
        assert self.camera_config is not None, "test forgot to set camera_config"
        return self.camera_config

    def get_playfield(self, name: str) -> Playfield:
        self.get_playfield_called = True
        self.requested_playfield = name
        if self.get_playfield_raises is not None:
            raise self.get_playfield_raises
        assert self.playfield is not None, "test forgot to set playfield"
        return self.playfield

    def list_tag_parameters(self) -> Dict[TagId, MountRegistration]:
        self.list_tag_parameters_called = True
        if self.list_tag_parameters_raises is not None:
            raise self.list_tag_parameters_raises
        return self.mount_parameters

    def get_tag(self, camera: str, tag: TagId) -> Optional[TagRecord]:
        self.get_tag_calls.append((camera, tag))
        if self.get_tag_raises is not None:
            raise self.get_tag_raises
        return self.tag_records.get(tag)

    def set_camera_config(self, config: CameraConfig) -> None:
        if self.set_camera_config_raises is not None:
            raise self.set_camera_config_raises
        self.pushed_camera_config = config


@dataclass
class _FakeDiscovery:
    daemon: Optional[_FakeDaemon] = None
    connect_raises: Optional[Exception] = None
    connected_with: Optional[str] = None

    def connect(self, host: Optional[str] = None):
        self.connected_with = host
        if self.connect_raises is not None:
            raise self.connect_raises
        return self.daemon


class _FakeCameraConfigLoader:
    """A ``ConfigLoader``-shaped double, mirroring
    ``test_camera_cli.py``'s ``_FakeCameraConfigLoader`` exactly: returns
    a pre-built ``CameraConfig`` (or raises) instead of touching a real
    file, and records the path it was asked to load."""

    def __init__(self, config: Optional[CameraConfig] = None, raises: Optional[Exception] = None):
        self._config = config
        self._raises = raises
        self.requested_path: Optional[Path] = None

    def load_camera_config(self, path: Path) -> CameraConfig:
        self.requested_path = path
        if self._raises is not None:
            raise self._raises
        assert self._config is not None, "test forgot to set config"
        return self._config

    def load_playfield(self, path: Path):  # pragma: no cover - not exercised here
        raise NotImplementedError


@dataclass
class _FakeDirectories:
    """Stands in for ``config.directories.Directories`` -- only
    ``config_dir`` is ever read by ``cmd_locate``."""

    config_dir: Path


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        daemon=None,
        all=False,
        camera=CAMERA,
        reference=None,
        dry_run=False,
        force=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _seams(
    *,
    daemon: _FakeDaemon,
    loader: Optional[_FakeCameraConfigLoader] = None,
    config_dir: Optional[Path] = None,
) -> dict:
    discovery = _FakeDiscovery(daemon=daemon)
    kwargs = dict(discovery_factory=lambda: discovery)
    if loader is not None:
        kwargs["loader_factory"] = lambda: loader
    if config_dir is not None:
        kwargs["directories_factory"] = lambda: _FakeDirectories(config_dir=config_dir)
    return kwargs


# ---------------------------------------------------------------------------
# --all refusal (single-target, matching cmd_calibrate/cmd_view precedent)
# ---------------------------------------------------------------------------


def test_locate_refuses_all_before_ever_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect when --all is refused up front")

    code = locate_cli.cmd_locate(
        _args(all=True, reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        discovery_factory=lambda: _ExplodingDiscovery(),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "--all" in err
    assert "single" in err.lower()


# ---------------------------------------------------------------------------
# UC-29.S-3 -- fewer than 2 references, refused before any connection
# ---------------------------------------------------------------------------


def test_locate_refuses_fewer_than_two_references_before_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect before the reference count is validated")

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2"]),
        discovery_factory=lambda: _ExplodingDiscovery(),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "at least 2" in err


def test_locate_with_zero_references_is_refused_before_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect with no --reference at all")

    code = locate_cli.cmd_locate(_args(reference=None), discovery_factory=lambda: _ExplodingDiscovery())

    assert code == 1
    assert "at least 2" in capsys.readouterr().err


def test_locate_malformed_reference_is_refused_before_connecting(capsys):
    class _ExplodingDiscovery:
        def connect(self, host=None):
            raise AssertionError("must not connect with a malformed --reference")

    code = locate_cli.cmd_locate(
        _args(reference=["not-a-valid-reference", "14:dot-southwest-orange:15.0"]),
        discovery_factory=lambda: _ExplodingDiscovery(),
    )

    assert code == 1
    assert "not-a-valid-reference" in capsys.readouterr().err


def test_locate_exactly_two_references_prints_a_non_blocking_advisory(capsys, tmp_path):
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        tag_records={_TAG_15: _record(_TAG_15, _TAG_15_OBSERVED), _TAG_14: _record(_TAG_14, _TAG_14_OBSERVED)},
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader, config_dir=tmp_path / "config"),
    )

    assert code == 0
    err = capsys.readouterr().err
    assert "only 2 references" in err or "3+" in err


# ---------------------------------------------------------------------------
# UC-29.S-1 -- the real fixture, end-to-end: good fit, applied without --force
# ---------------------------------------------------------------------------


def test_locate_real_fixture_solves_and_applies_without_force(capsys, tmp_path):
    on_disk = _camera_config()
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        tag_records={_TAG_15: _record(_TAG_15, _TAG_15_OBSERVED), _TAG_14: _record(_TAG_14, _TAG_14_OBSERVED)},
    )
    loader = _FakeCameraConfigLoader(config=on_disk)
    config_dir = tmp_path / "config"

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader, config_dir=config_dir),
    )

    assert code == 0
    out = capsys.readouterr().out
    # Pinned to ticket 002's own regression fixture (tests/aprilcam/
    # calibration/test_geometry.py::test_solve_camera_geometry_real_fixture_regression):
    # nadir (3.057, -2.799) cm, height 128.230 cm, rms_residual_cm 0.1585,
    # condition_number 67.44 -- reported verbatim, not just applied silently.
    assert "nadir=(3.057, -2.799) cm" in out
    assert "height=128.230 cm" in out
    assert "rms_residual_cm=0.1585" in out
    assert "condition_number=67.44" in out

    # Applied both live and on disk, matching ticket 002's pinned solve
    # (nadir (3.057, -2.799) cm, height 128.230 cm) within a loose
    # tolerance -- this test is about cmd_locate's plumbing, not
    # re-verifying solve_camera_geometry's own math.
    assert daemon.pushed_camera_config is not None
    pushed_position = daemon.pushed_camera_config.camera_position
    assert pushed_position is not None
    assert pushed_position.nadir.x == pytest.approx(3.057, abs=0.05)
    assert pushed_position.nadir.y == pytest.approx(-2.799, abs=0.05)
    assert pushed_position.height_cm == pytest.approx(128.23, abs=0.05)
    # every other field is untouched
    assert daemon.pushed_camera_config.playfield == PLAYFIELD
    assert daemon.pushed_camera_config.settings == {"exposure": "100"}

    written_path = config_dir / "cameras" / CAMERA / "config.json"
    assert written_path.exists()
    data = json.loads(written_path.read_text())
    assert data["playfield"] == PLAYFIELD
    assert data["settings"] == {"exposure": "100"}
    assert data["camera_position"]["nadir_x_cm"] == pytest.approx(3.057, abs=0.05)
    assert data["camera_position"]["height_cm"] == pytest.approx(128.23, abs=0.05)

    assert loader.requested_path == config_dir / "cameras" / CAMERA / "config.json"


def test_locate_fail_fast_stops_at_the_first_undetected_reference(capsys):
    """UC-29's 'reference tag not detected' AC: fail fast, never a
    partial solve over whatever *was* seen. Tag 14 (listed first) is
    never detected; tag 15 must never even be requested."""
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        tag_records={_TAG_15: _record(_TAG_15, _TAG_15_OBSERVED)},  # tag 14 absent
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())

    code = locate_cli.cmd_locate(
        _args(reference=["14:dot-southwest-orange:15.0", "15:dot-northeast-orange:17.2"]),
        **_seams(daemon=daemon, loader=loader),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "14" in err
    assert "not detected" in err
    assert daemon.get_tag_calls == [(CAMERA, _TAG_14)]  # 15 never attempted
    assert daemon.pushed_camera_config is None


def test_locate_uncalibrated_record_with_no_world_is_treated_as_not_detected(capsys):
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        tag_records={
            _TAG_15: TagRecord(
                tag=_TAG_15, world=None, heading_rad=None, world_velocity=None, speed=None, timestamp=0.0
            ),
            _TAG_14: _record(_TAG_14, _TAG_14_OBSERVED),
        },
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader),
    )

    assert code == 1
    assert "not detected" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# UC-29.S-2 / UC-29.S-5 -- ill-conditioned / poor-fit: warned, not written
# without --force; --dry-run never writes regardless
# ---------------------------------------------------------------------------


def _near_collinear_fixture() -> Tuple[_FakeDaemon, _FakeCameraConfigLoader, List[str]]:
    """Reproduces ticket 002's own near-collinear degenerate case
    (two references on nearly the same ray from the nadir) end-to-end
    through the CLI -- the exact failure shape sprint.md's Problem
    section calls out as the trap this ticket's quality gate exists to
    catch."""
    true_nadir = WorldPoint(x=5.0, y=-5.0)
    true_height_cm = 130.0
    height_cm = 17.2

    true_a = WorldPoint(x=50.0, y=30.0)
    direction = WorldPoint(x=true_a.x - true_nadir.x, y=true_a.y - true_nadir.y)
    true_b = WorldPoint(x=true_nadir.x + 1.02 * direction.x, y=true_nadir.y + 1.02 * direction.y)

    dots = (
        PlayfieldFeatureEntry(slug="ref-a", x=true_a.x, y=true_a.y),
        PlayfieldFeatureEntry(slug="ref-b", x=true_b.x, y=true_b.y),
    )
    tag_a = TagId(TagFamily.APRILTAG, 21)
    tag_b = TagId(TagFamily.APRILTAG, 22)
    observed_a = _forward_apparent(true_a, height_cm, true_nadir, true_height_cm)
    observed_b = _forward_apparent(true_b, height_cm, true_nadir, true_height_cm)

    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(dots=dots),
        tag_records={tag_a: _record(tag_a, observed_a), tag_b: _record(tag_b, observed_b)},
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())
    references = [f"21:ref-a:{height_cm}", f"22:ref-b:{height_cm}"]
    return daemon, loader, references


def test_locate_ill_conditioned_references_are_reported_but_not_written(capsys):
    daemon, loader, references = _near_collinear_fixture()

    code = locate_cli.cmd_locate(_args(reference=references), **_seams(daemon=daemon, loader=loader))

    assert code == 1
    out, err = capsys.readouterr()
    assert "solved:" in out  # still reported
    assert "warning" in err.lower()
    assert "--force" in err
    assert daemon.pushed_camera_config is None


def test_locate_ill_conditioned_references_written_with_force(capsys, tmp_path):
    daemon, loader, references = _near_collinear_fixture()
    config_dir = tmp_path / "config"

    code = locate_cli.cmd_locate(
        _args(reference=references, force=True),
        **_seams(daemon=daemon, loader=loader, config_dir=config_dir),
    )

    assert code == 0
    assert daemon.pushed_camera_config is not None
    assert (config_dir / "cameras" / CAMERA / "config.json").exists()


def test_locate_dry_run_never_writes_even_with_a_good_fit(capsys, tmp_path):
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        tag_records={_TAG_15: _record(_TAG_15, _TAG_15_OBSERVED), _TAG_14: _record(_TAG_14, _TAG_14_OBSERVED)},
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())
    config_dir = tmp_path / "config"

    code = locate_cli.cmd_locate(
        _args(
            reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"],
            dry_run=True,
        ),
        **_seams(daemon=daemon, loader=loader, config_dir=config_dir),
    )

    assert code == 0
    assert "dry-run" in capsys.readouterr().out
    assert daemon.pushed_camera_config is None
    assert not (config_dir / "cameras" / CAMERA / "config.json").exists()


def test_locate_dry_run_never_writes_even_with_a_poor_fit(capsys, tmp_path):
    daemon, loader, references = _near_collinear_fixture()
    config_dir = tmp_path / "config"

    code = locate_cli.cmd_locate(
        _args(reference=references, dry_run=True, force=True),  # --force must not matter here
        **_seams(daemon=daemon, loader=loader, config_dir=config_dir),
    )

    assert code == 0
    assert "dry-run" in capsys.readouterr().out
    assert daemon.pushed_camera_config is None
    assert not (config_dir / "cameras" / CAMERA / "config.json").exists()


# ---------------------------------------------------------------------------
# UC-29.S-4 -- contaminated reference (registered, nonzero mount_z)
# ---------------------------------------------------------------------------


def test_locate_refuses_a_reference_registered_with_nonzero_mount_z(capsys):
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        mount_parameters={
            _TAG_15: MountRegistration(
                params=MountParameters(mount_z=5.0), mount_z_applied=True
            )
        },
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "15" in err
    assert "mount_z" in err
    assert daemon.get_tag_calls == []  # no detection was attempted


def test_locate_allows_a_reference_registered_with_zero_mount_z(capsys, tmp_path):
    """mount_z <= 0 is project_tag_to_field_plane's own no-op sentinel --
    identical to 'unregistered' for the contamination guard's purpose.

    ``config_dir`` is pinned to ``tmp_path`` (2026-08-22 hotfix) --
    without it, ``_seams`` omits ``directories_factory`` entirely and
    ``cmd_locate`` falls back to its real default, ``SystemDirectories``.
    This test reaches ``code == 0`` (a genuine solve-and-apply, unlike
    every other ``_seams(...)`` call in this file with no ``config_dir``,
    all of which exit before ever writing), so with no override it wrote
    ``_camera_config()``'s fixture settings straight into this real dev
    machine's actual ``~/.config/aprilcam/cameras/arducam-ov9782-usb-
    camera/config.json`` -- the exact live file for the exact real
    camera this constant names -- silently clobbering whatever was
    really configured there. Confirmed live: running this file
    reintroduced the real ``"exposure": "100"`` string-typed bug this
    same sprint's hotfix had just corrected on disk."""
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        mount_parameters={
            _TAG_15: MountRegistration(
                params=MountParameters(mount_x=1.0, mount_z=0.0), mount_z_applied=True
            )
        },
        tag_records={_TAG_15: _record(_TAG_15, _TAG_15_OBSERVED), _TAG_14: _record(_TAG_14, _TAG_14_OBSERVED)},
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader, config_dir=tmp_path / "config"),
    )

    assert code == 0
    assert len(daemon.get_tag_calls) == 2


# ---------------------------------------------------------------------------
# Unresolved feature slug -- refused before any detection
# ---------------------------------------------------------------------------


def test_locate_refuses_an_unresolved_feature_slug_before_any_detection(capsys):
    daemon = _FakeDaemon(camera_config=_camera_config(), playfield=_playfield())
    loader = _FakeCameraConfigLoader(config=_camera_config())

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-nonexistent:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "dot-nonexistent" in err
    assert daemon.list_tag_parameters_called is False
    assert daemon.get_tag_calls == []


# ---------------------------------------------------------------------------
# No playfield linked -- reused NoPlayfieldLinked, not reinvented
# ---------------------------------------------------------------------------


def test_locate_no_playfield_linked_reports_the_typed_error(capsys):
    daemon = _FakeDaemon(camera_config=_camera_config(playfield=None))
    loader = _FakeCameraConfigLoader(config=_camera_config())

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert CAMERA in err
    assert "playfield" in err.lower()
    assert daemon.get_playfield_called is False


def test_locate_no_playfield_linked_matches_the_exact_error_type(capsys):
    """Same shape as ``test_calibrate_cli.py``'s equivalent test --
    ``NoPlayfieldLinked`` is reused, not reinvented (ticket 003's own
    acceptance criterion): the printed message is exactly what
    ``str(NoPlayfieldLinked(...))`` produces, not a bespoke rewording."""
    daemon = _FakeDaemon(camera_config=_camera_config(playfield=None))
    loader = _FakeCameraConfigLoader(config=_camera_config())

    locate_cli.cmd_locate(
        _args(reference=["15:a:1.0", "14:b:2.0"]), **_seams(daemon=daemon, loader=loader)
    )

    assert str(NoPlayfieldLinked(CAMERA)) in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Transport / precondition errors surfaced generically via report_error
# ---------------------------------------------------------------------------


def test_locate_reports_a_connection_failure(capsys):
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))

    code = locate_cli.cmd_locate(
        _args(daemon="pi-field.local", reference=["15:a:1.0", "14:b:2.0"]),
        discovery_factory=lambda: discovery,
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


def test_locate_reports_unknown_camera(capsys):
    daemon = _FakeDaemon(get_camera_config_raises=UnknownCamera("nope", (CAMERA,)))

    code = locate_cli.cmd_locate(
        _args(camera="nope", reference=["15:a:1.0", "14:b:2.0"]), **_seams(daemon=daemon)
    )

    assert code == 1
    assert "nope" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Reference parsing (--reference TAG:SLUG:HEIGHT_CM)
# ---------------------------------------------------------------------------


def test_parse_reference_accepts_a_well_formed_value():
    spec = locate_cli._parse_reference("15:dot-northeast-orange:17.2")
    assert spec.tag == 15
    assert spec.slug == "dot-northeast-orange"
    assert spec.height_cm == 17.2


@pytest.mark.parametrize(
    "raw",
    [
        "15:dot-northeast-orange",  # missing height
        "15:dot-northeast-orange:17.2:extra",  # too many parts
        "not-an-int:dot-northeast-orange:17.2",  # bad tag
        "15::17.2",  # empty slug
        "15:dot-northeast-orange:not-a-float",  # bad height
        "15:dot-northeast-orange:0",  # non-positive height
        "15:dot-northeast-orange:-5.0",  # negative height
    ],
)
def test_parse_reference_rejects_malformed_values(raw):
    with pytest.raises(ValueError):
        locate_cli._parse_reference(raw)


# ---------------------------------------------------------------------------
# Config-file write is atomic and preserves untouched fields
# ---------------------------------------------------------------------------


def test_locate_write_preserves_settings_and_is_atomic(tmp_path):
    on_disk = _camera_config(settings={"exposure": "150", "gain": "2.0"})
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        tag_records={_TAG_15: _record(_TAG_15, _TAG_15_OBSERVED), _TAG_14: _record(_TAG_14, _TAG_14_OBSERVED)},
    )
    loader = _FakeCameraConfigLoader(config=on_disk)
    config_dir = tmp_path / "config"

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader, config_dir=config_dir),
    )

    assert code == 0
    written_path = config_dir / "cameras" / CAMERA / "config.json"
    data = json.loads(written_path.read_text())
    assert data["name"] == CAMERA
    assert data["playfield"] == PLAYFIELD
    assert data["settings"] == {"exposure": "150", "gain": "2.0"}
    assert "camera_position" in data
    # no leftover .tmp file after a successful write
    assert not any(p.name.endswith(".tmp") for p in written_path.parent.iterdir())


def test_locate_reports_but_does_not_crash_if_the_live_push_fails_after_the_disk_write(
    capsys, tmp_path
):
    daemon = _FakeDaemon(
        camera_config=_camera_config(),
        playfield=_playfield(),
        tag_records={_TAG_15: _record(_TAG_15, _TAG_15_OBSERVED), _TAG_14: _record(_TAG_14, _TAG_14_OBSERVED)},
        set_camera_config_raises=ConnectionFailed("local", reason="daemon restarted mid-push"),
    )
    loader = _FakeCameraConfigLoader(config=_camera_config())
    config_dir = tmp_path / "config"

    code = locate_cli.cmd_locate(
        _args(reference=["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]),
        **_seams(daemon=daemon, loader=loader, config_dir=config_dir),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "failed to push it live" in err
    # the disk write itself still happened -- reported, not silently lost
    assert (config_dir / "cameras" / CAMERA / "config.json").exists()


# ---------------------------------------------------------------------------
# argv wiring -- 'camera locate' (grouped under camera.add_subparser)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aprilcam")
    parser.set_defaults(func=None)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    daemon_cli.add_subparser(subparsers)
    camera_cli.add_subparser(subparsers)
    playfield_cli.add_subparser(subparsers)
    return parser


def test_locate_is_wired_under_camera_as_cmd_locate():
    parser = _build_parser()
    args = parser.parse_args(
        [
            "camera",
            "locate",
            CAMERA,
            "--reference",
            "15:dot-northeast-orange:17.2",
            "--reference",
            "14:dot-southwest-orange:15.0",
        ]
    )
    assert args.func is locate_cli.cmd_locate
    assert args.camera == CAMERA
    assert args.reference == ["15:dot-northeast-orange:17.2", "14:dot-southwest-orange:15.0"]
    assert args.all is False
    assert args.daemon is None
    assert args.dry_run is False
    assert args.force is False


def test_locate_requires_a_camera_positional_argument():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["camera", "locate"])


def test_locate_accepts_a_persistent_number_as_the_camera_token():
    parser = _build_parser()
    args = parser.parse_args(["camera", "locate", "3", "--reference", "1:a:1.0", "--reference", "2:b:2.0"])
    assert args.camera == "3"


def test_locate_dry_run_and_force_flags_are_wired():
    parser = _build_parser()
    args = parser.parse_args(
        ["camera", "locate", CAMERA, "--reference", "1:a:1.0", "--reference", "2:b:2.0", "--dry-run", "--force"]
    )
    assert args.dry_run is True
    assert args.force is True


def test_locate_accepts_all_at_parse_time_but_cmd_locate_refuses_it_at_runtime():
    parser = _build_parser()
    args = parser.parse_args(
        ["camera", "locate", CAMERA, "--reference", "1:a:1.0", "--reference", "2:b:2.0", "--all"]
    )
    assert args.all is True


def test_locate_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["camera", "locate", CAMERA, "-h"])
    assert excinfo.value.code == 0
