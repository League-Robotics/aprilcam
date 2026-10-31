"""Tests for ``aprilcam.cli.mcp`` (ticket 026-001, UC-22).

Headless: ``cmd_mcp`` is driven against a fake server object injected
through its ``server_factory`` keyword, exactly like every other
``cli/`` module's ``*_factory`` seam (``test_daemon_cli.py``'s
convention) -- no real ``FastMCP`` instance, no stdio transport, no
daemon or camera hardware touched here.

**Base-install crash regression (OOP fix, 2026-08-22).** The block
below headed "lazy mcp SDK import" covers the bug this fix addresses:
``cli/mcp.py`` used to import the ``mcp`` SDK at its own module top
level, which ``cli/__init__.py``'s unconditional
``from aprilcam.cli import camera, daemon, guide, mcp, playfield, tags``
then pulled in at CLI startup -- before any subcommand dispatch --
so a base install missing (or with a broken) ``mcp`` package could not
run *any* ``aprilcam`` subcommand, not just ``mcp``. These tests pin
down the two halves of the fix: ``_is_missing_mcp_sdk`` correctly
classifies which ``ModuleNotFoundError``s are "the mcp SDK is missing"
(real end-to-end proof, via a ``sys.meta_path`` finder that makes
``mcp`` genuinely unimportable, that ``_import_build_server`` converts
that into a typed error), and ``cmd_mcp`` reports that typed error
cleanly instead of letting it crash the process.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

import pytest

from aprilcam.cli import main as cli_main
from aprilcam.cli import mcp as mcp_cli
from aprilcam.errors import MissingOptionalDependency
from aprilcam.cli._common import add_daemon_option


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
    add_daemon_option(top)
    top.set_defaults(func=None)
    subparsers = top.add_subparsers(dest="command")
    mcp_cli.add_subparser(subparsers)
    return top


def test_mcp_accepts_the_root_daemon_flag_without_erroring_at_parse_time():
    parser = _build_parser()
    args = parser.parse_args(["-d", "pi-field.local", "mcp"])
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


def test_daemon_flag_is_on_the_root_not_on_subcommands(capsys):
    """-d targets a daemon for every command, so it lives on the root
    parser once: ``aprilcam -d HOST mcp``, never ``aprilcam mcp -d HOST``."""
    with pytest.raises(SystemExit):
        cli_main(["--help"])
    assert "--daemon" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli_main(["mcp", "-h"])
    assert "--daemon" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["camera", "list", "-d", "pi-field.local"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# _is_missing_mcp_sdk -- classifies which ModuleNotFoundErrors mean "the
# mcp SDK isn't installed" vs. an unrelated import failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_name",
    ["mcp", "mcp.server", "mcp.server.fastmcp", "mcp.types"],
)
def test_is_missing_mcp_sdk_true_for_mcp_and_its_submodules(missing_name: str) -> None:
    exc = ModuleNotFoundError(f"No module named {missing_name!r}", name=missing_name)
    assert mcp_cli._is_missing_mcp_sdk(exc) is True


@pytest.mark.parametrize("missing_name", ["numpy", "mcpx", "aprilcam.mcp.server"])
def test_is_missing_mcp_sdk_false_for_unrelated_modules(missing_name: str) -> None:
    exc = ModuleNotFoundError(f"No module named {missing_name!r}", name=missing_name)
    assert mcp_cli._is_missing_mcp_sdk(exc) is False


def test_is_missing_mcp_sdk_false_when_exception_carries_no_name() -> None:
    # `ModuleNotFoundError.name` can be None in principle; must not
    # raise trying to call .startswith() on it.
    exc = ModuleNotFoundError("something went wrong")
    assert mcp_cli._is_missing_mcp_sdk(exc) is False


# ---------------------------------------------------------------------------
# _import_build_server -- the actual lazy import, end to end
# ---------------------------------------------------------------------------


class _BlockMcpImports:
    """A ``sys.meta_path`` finder that makes ``mcp``/``mcp.*`` genuinely
    unimportable, reproducing the stakeholder's base-install failure
    mode without uninstalling anything from this dev environment."""

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001, ANN201
        if fullname == "mcp" or fullname.startswith("mcp."):
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (blocked for test)", name=fullname
            )
        return None


@pytest.fixture()
def mcp_sdk_uninstalled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Simulates an environment where the ``mcp`` SDK cannot be
    imported at all: evicts every already-cached ``mcp``/``mcp.*`` and
    ``aprilcam.mcp``/``aprilcam.mcp.*`` module from ``sys.modules`` (so
    the next import actually re-executes rather than hitting the
    cache) and installs a blocking finder ahead of the real ones.
    ``monkeypatch`` restores both ``sys.modules`` and ``sys.meta_path``
    to their prior state on teardown, so this cannot leak into other
    tests.
    """
    for name in list(sys.modules):
        if (
            name == "mcp"
            or name.startswith("mcp.")
            or name == "aprilcam.mcp"
            or name.startswith("aprilcam.mcp.")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockMcpImports(), *sys.meta_path])
    yield


def test_import_build_server_raises_missing_optional_dependency_when_mcp_sdk_is_absent(
    mcp_sdk_uninstalled: None,
) -> None:
    with pytest.raises(MissingOptionalDependency) as excinfo:
        mcp_cli._import_build_server()

    assert excinfo.value.package == "mcp"
    assert excinfo.value.feature == "aprilcam mcp"
    assert "mcp" in str(excinfo.value)


def test_import_build_server_returns_the_real_build_server_when_mcp_sdk_is_available() -> None:
    from aprilcam.mcp.server import build_server

    assert mcp_cli._import_build_server() is build_server


# ---------------------------------------------------------------------------
# cmd_mcp -- reports the missing-dependency error cleanly, no traceback
# ---------------------------------------------------------------------------


def test_cmd_mcp_reports_missing_mcp_sdk_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise_missing() -> None:
        raise MissingOptionalDependency(
            "aprilcam mcp", "mcp", install_hint="pip install mcp"
        )

    monkeypatch.setattr(mcp_cli, "_import_build_server", _raise_missing)

    code = mcp_cli.cmd_mcp(_args())

    assert code == 1
    err = capsys.readouterr().err
    assert "aprilcam mcp" in err
    assert "mcp" in err
    assert "Traceback" not in err


def test_cmd_mcp_end_to_end_with_mcp_sdk_genuinely_absent(
    mcp_sdk_uninstalled: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full path exercised with no injected factory at all: the
    default (``server_factory=None``) resolves through
    ``_import_build_server``, which hits the real blocked import."""
    code = mcp_cli.cmd_mcp(_args())

    assert code == 1
    err = capsys.readouterr().err
    assert "aprilcam mcp" in err
    assert "Traceback" not in err


def test_cmd_mcp_with_injected_factory_never_touches_the_real_import(
    mcp_sdk_uninstalled: None,
) -> None:
    """A test-injected ``server_factory`` (the existing convention)
    must keep working even with the real ``mcp`` SDK unimportable --
    proving the lazy import is only ever consulted when no factory is
    given."""
    factory = _FakeServerFactory()

    code = mcp_cli.cmd_mcp(_args(daemon="pi-field.local"), server_factory=factory)

    assert code == 0
    assert factory.build_calls == ["pi-field.local"]
