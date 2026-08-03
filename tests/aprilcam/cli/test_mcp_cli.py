"""Tests for ``aprilcam.cli.mcp`` (ticket 026-001, UC-22).

Headless: ``cmd_mcp`` is driven against a fake server object injected
through its ``server_factory`` keyword, exactly like every other
``cli/`` module's ``*_factory`` seam (``test_daemon_cli.py``'s
convention) -- no real ``FastMCP`` instance, no stdio transport, no
daemon or camera hardware touched here.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import List, Optional

import pytest

from aprilcam.cli import main as cli_main
from aprilcam.cli import mcp as mcp_cli


def _args(*, daemon: Optional[str] = None) -> argparse.Namespace:
    return argparse.Namespace(daemon=daemon)


@dataclass
class _FakeServer:
    run_calls: List[str] = field(default_factory=list)

    def run(self, transport: str = "stdio") -> None:
        self.run_calls.append(transport)


@dataclass
class _FakeServerFactory:
    server: _FakeServer = field(default_factory=_FakeServer)
    build_calls: List[Optional[str]] = field(default_factory=list)

    def __call__(self, *, default_daemon_host: Optional[str] = None) -> _FakeServer:
        self.build_calls.append(default_daemon_host)
        return self.server


# ---------------------------------------------------------------------------
# cmd_mcp -- builds the server with the -d flag as the default host,
# then serves over stdio
# ---------------------------------------------------------------------------


def test_cmd_mcp_builds_the_server_with_the_daemon_flag_as_the_default_host():
    factory = _FakeServerFactory()

    code = mcp_cli.cmd_mcp(_args(daemon="pi-field.local"), server_factory=factory)

    assert code == 0
    assert factory.build_calls == ["pi-field.local"]


def test_cmd_mcp_with_no_flag_passes_none_as_the_default_host():
    factory = _FakeServerFactory()

    code = mcp_cli.cmd_mcp(_args(), server_factory=factory)

    assert code == 0
    assert factory.build_calls == [None]


def test_cmd_mcp_serves_over_stdio():
    factory = _FakeServerFactory()

    mcp_cli.cmd_mcp(_args(), server_factory=factory)

    assert factory.server.run_calls == ["stdio"]


# ---------------------------------------------------------------------------
# argparse wiring -- -d accepted, -h stays help
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="aprilcam")
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    mcp_cli.add_subparser(subparsers)
    return top


def test_mcp_accepts_the_daemon_flag_without_erroring_at_parse_time():
    parser = _build_parser()
    args = parser.parse_args(["mcp", "-d", "pi-field.local"])
    assert args.daemon == "pi-field.local"
    assert args.func is mcp_cli.cmd_mcp


def test_mcp_with_no_flag_defaults_daemon_to_none():
    parser = _build_parser()
    args = parser.parse_args(["mcp"])
    assert args.daemon is None


def test_mcp_help_flag_is_still_help_not_a_daemon_target():
    parser = _build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["mcp", "-h"])
    assert excinfo.value.code == 0


def test_mcp_wired_into_top_level_main_help(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "mcp" in out


def test_mcp_subcommand_help_describes_the_mcp_server(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["mcp", "-h"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "usage: aprilcam mcp" in out
    assert "-d" in out or "--daemon" in out
