"""Tests for ``aprilcam.cli.playfield`` (ticket 022-001).

Headless, matching ``test_camera_cli.py``'s established style: a fake
``ConfigLoader`` and a fake ``Daemon``/``Discovery`` pair injected
through ``cmd_config``'s keyword seams -- no real file I/O, no daemon.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from aprilcam.cli import playfield as playfield_cli
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.errors import MalformedDocument, UnknownPlayfield


def _definition(name: str = "main-playfield") -> PlayfieldDefinition:
    return PlayfieldDefinition(
        name=name,
        width_cm=134.3,
        height_cm=89.3,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"u{i}", x=float(i), y=float(-i), tag_id=i)
            for i in range(1, 11)
        ),
    )


class _FakePlayfieldLoader:
    def __init__(self, definition=None, raises: Optional[Exception] = None):
        self._definition = definition
        self._raises = raises
        self.requested_path = None

    def load_playfield(self, path):
        self.requested_path = path
        if self._raises is not None:
            raise self._raises
        return self._definition

    def load_camera_config(self, path):  # pragma: no cover - not exercised here
        raise NotImplementedError


@dataclass
class _FakeDaemon:
    set_playfield_raises: Optional[Exception] = None
    pushed: object = None

    def set_playfield(self, definition) -> None:
        if self.set_playfield_raises is not None:
            raise self.set_playfield_raises
        self.pushed = definition


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


def _args(**overrides) -> argparse.Namespace:
    base = dict(daemon=None, file="main-playfield.json")
    base.update(overrides)
    return argparse.Namespace(**base)


def test_config_pushes_the_loaded_definition(capsys, tmp_path):
    definition = _definition()
    loader = _FakePlayfieldLoader(definition=definition)
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)
    file_path = tmp_path / "main-playfield.json"

    code = playfield_cli.cmd_config(
        _args(file=str(file_path)),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 0
    assert loader.requested_path == Path(file_path)
    assert daemon.pushed is definition
    out = capsys.readouterr().out
    assert "main-playfield" in out
    assert "10 features" in out
    assert "10 required ArUco ids" in out


def test_config_malformed_document_is_rejected_before_any_rpc(capsys):
    loader = _FakePlayfieldLoader(raises=MalformedDocument("duplicate ArUco id 3"))
    daemon = _FakeDaemon()
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_config(
        _args(), discovery_factory=lambda: discovery, loader_factory=lambda: loader
    )

    assert code == 1
    assert "duplicate ArUco id 3" in capsys.readouterr().err
    assert daemon.pushed is None


def test_config_reports_a_daemon_side_rejection(capsys):
    loader = _FakePlayfieldLoader(definition=_definition())
    daemon = _FakeDaemon(set_playfield_raises=UnknownPlayfield("main-playfield", ()))
    discovery = _FakeDiscovery(daemon=daemon)

    code = playfield_cli.cmd_config(
        _args(), discovery_factory=lambda: discovery, loader_factory=lambda: loader
    )

    assert code == 1
    assert "main-playfield" in capsys.readouterr().err


def test_config_reports_connection_failure(capsys):
    from aprilcam.errors import ConnectionFailed

    loader = _FakePlayfieldLoader(definition=_definition())
    discovery = _FakeDiscovery(connect_raises=ConnectionFailed("pi-field.local", reason="refused"))

    code = playfield_cli.cmd_config(
        _args(daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "pi-field.local" in err and "refused" in err


def test_config_passes_the_daemon_flag_through_to_connect():
    loader = _FakePlayfieldLoader(definition=_definition())
    discovery = _FakeDiscovery(daemon=_FakeDaemon())

    playfield_cli.cmd_config(
        _args(daemon="pi-field.local"),
        discovery_factory=lambda: discovery,
        loader_factory=lambda: loader,
    )

    assert discovery.connected_with == "pi-field.local"


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="aprilcam")
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    playfield_cli.add_subparser(subparsers)
    return top


def test_config_is_wired_to_cmd_config():
    parser = _build_parser()
    args = parser.parse_args(["playfield", "config", "main-playfield.json"])
    assert args.func is playfield_cli.cmd_config
    assert args.file == "main-playfield.json"
    assert args.daemon is None


def test_config_requires_a_file_positional_argument():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["playfield", "config"])


def test_playfield_with_no_subcommand_uses_the_incomplete_command_default(capsys):
    parser = _build_parser()
    args = parser.parse_args(["playfield"])

    code = args.func(args)

    assert code == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_config_h_prints_help_not_a_missing_hostname_error():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["playfield", "config", "main-playfield.json", "-h"])
    assert excinfo.value.code == 0
