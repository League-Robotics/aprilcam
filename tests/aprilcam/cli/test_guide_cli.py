"""Tests for ``aprilcam.cli.guide`` (ticket 025-007, UC-27).

**UC-27.S-1's "no daemon running anywhere" is proven two ways here, not
merely asserted**:

* :func:`test_guide_module_imports_nothing_daemon_or_network_shaped` --
  a static AST scan of ``cli/guide.py``'s own import statements, the
  same technique ``tests/aprilcam/client/test_guides.py`` already uses
  for ``client/guides.py`` (this module's own accessor).
* :func:`test_guide_still_works_even_if_discovery_construction_is_poisoned`
  -- a dynamic, mechanical proof: ``client.discovery.Discovery.__init__``
  is replaced with something that unconditionally raises, and ``aprilcam
  guide`` still succeeds. That is only possible if ``cmd_guide``'s
  runtime path never constructs a ``Discovery`` (or, transitively, a
  ``Daemon``) at all -- not "happens not to today."

Everything else here is the usual argv-to-accessor CLI test shape
(``tests/aprilcam/cli/test_tags_cli.py``'s established convention): a
fake ``client.interfaces.Guides`` injected through ``cmd_guide``'s
``guides_factory`` keyword for isolated wiring tests, plus a few tests
against the real ``client.guides.Guides`` for genuine content.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import List, Optional

import pytest

from aprilcam.cli import guide as guide_cli
from aprilcam.cli import main as cli_main
from aprilcam.cli._common import EXIT_ERROR, EXIT_OK
from aprilcam.client.guides import Guides
from aprilcam.errors import UnknownGuide

REPO_ROOT = Path(__file__).resolve().parents[3]
GUIDE_CLI_MODULE_PATH = REPO_ROOT / "src" / "aprilcam" / "cli" / "guide.py"

#: Same forbidden-name list ``test_guides.py`` polices ``client/guides.py``
#: with -- none of these belong in this module's own import list either,
#: since ``cmd_guide`` must never construct a ``Discovery``/``Daemon`` or
#: open a socket (UC-27.S-1).
_FORBIDDEN_IMPORT_NAMES = (
    "grpc",
    "aprilcam.client.discovery",
    "aprilcam.client.daemon_client",
    "aprilcam.client.lifecycle",
    "aprilcam.daemon.lifecycle",
    "aprilcam.daemon.service",
    "aprilcam.daemon.rpc",
)


class _FakeGuides:
    """A minimal ``client.interfaces.Guides``-shaped double -- isolates
    ``cmd_guide``'s own argv-to-call wiring from the real packaged text,
    matching ``test_tags_cli.py``'s fake-injection convention."""

    def __init__(self, text: str = "fake guide text", raises: Optional[Exception] = None) -> None:
        self._text = text
        self._raises = raises
        self.requested_names: List[Optional[str]] = []

    def get(self, name=None) -> str:
        self.requested_names.append(name)
        if self._raises is not None:
            raise self._raises
        return self._text

    def available(self):
        return ("agent", "robot")


def _args(name=None) -> argparse.Namespace:
    return argparse.Namespace(name=name)


# ---------------------------------------------------------------------------
# UC-27.S-1 -- no Discovery/Daemon object constructed anywhere in the path
# ---------------------------------------------------------------------------


def test_guide_module_imports_nothing_daemon_or_network_shaped() -> None:
    """Static proof: ``cli/guide.py``'s own import statements name none
    of the modules that could construct a ``Daemon`` or a network
    channel."""
    tree = ast.parse(GUIDE_CLI_MODULE_PATH.read_text(), filename=str(GUIDE_CLI_MODULE_PATH))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [name for name in imported if name in _FORBIDDEN_IMPORT_NAMES]
    assert not offenders, (
        f"cli/guide.py must not import daemon/network-construction modules "
        f"(UC-27.S-1), but imports: {offenders}"
    )


def test_guide_still_works_even_if_discovery_construction_is_poisoned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dynamic, mechanical proof: with ``client.discovery.Discovery.__init__``
    replaced by something that unconditionally raises, ``aprilcam guide``
    (via the real top-level ``main``) still succeeds and prints real
    content -- possible only if ``cmd_guide``'s actual runtime path never
    constructs a ``Discovery`` (structurally proving "no daemon started
    anywhere," not just "no ``-d`` flag passed")."""
    import aprilcam.client.discovery as discovery_module

    def _poisoned_init(self, *args, **kwargs):
        raise AssertionError(
            "aprilcam guide must never construct a Discovery -- UC-27.S-1 "
            "requires it to work with no daemon running at all"
        )

    monkeypatch.setattr(discovery_module.Discovery, "__init__", _poisoned_init)

    exit_code = cli_main(["guide"])

    assert exit_code == 0
    assert "A1-centred" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_guide -- direct, with an injected fake (wiring only)
# ---------------------------------------------------------------------------


def test_cmd_guide_no_name_passes_none_through_to_get() -> None:
    fake = _FakeGuides(text="hello")
    exit_code = guide_cli.cmd_guide(_args(), guides_factory=lambda: fake)

    assert exit_code == EXIT_OK
    assert fake.requested_names == [None]


def test_cmd_guide_prints_the_returned_text_with_no_extra_trailing_newline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeGuides(text="line one\nline two\n")
    guide_cli.cmd_guide(_args("robot"), guides_factory=lambda: fake)

    out = capsys.readouterr().out
    assert out == "line one\nline two\n"


def test_cmd_guide_unknown_name_reports_error_and_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeGuides(raises=UnknownGuide("nonexistent", ("agent", "robot")))
    exit_code = guide_cli.cmd_guide(_args("nonexistent"), guides_factory=lambda: fake)

    assert exit_code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "agent" in err
    assert "robot" in err


# ---------------------------------------------------------------------------
# add_subparser -- structural: no -d/--daemon, no --all (deliberate
# exception, see cli/guide.py's module docstring)
# ---------------------------------------------------------------------------


def test_add_subparser_wires_no_daemon_or_all_option() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    guide_cli.add_subparser(subparsers)

    guide_parser = None
    for action in subparsers.choices.values():
        if action.prog.endswith("guide"):
            guide_parser = action
            break
    assert guide_parser is not None

    dests = {a.dest for a in guide_parser._actions}
    assert "daemon" not in dests
    assert "all" not in dests
    assert "name" in dests


# ---------------------------------------------------------------------------
# Real content, through the real top-level `main` -- no daemon started
# anywhere in these tests (UC-27's own acceptance criterion)
# ---------------------------------------------------------------------------


def test_guide_no_name_prints_the_real_default_agent_guide(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["guide"]) == EXIT_OK
    assert capsys.readouterr().out == Guides().get("agent")


def test_guide_agent_name_prints_the_real_agent_guide(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["guide", "agent"]) == EXIT_OK
    assert capsys.readouterr().out == Guides().get("agent")


def test_guide_robot_name_prints_the_real_robot_guide(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["guide", "robot"]) == EXIT_OK
    assert capsys.readouterr().out == Guides().get("robot")


def test_guide_unknown_name_reports_the_guides_that_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["guide", "nonexistent"])

    assert exit_code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "agent" in err
    assert "robot" in err
