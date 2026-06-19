"""Tests for the aprilcam CLI dispatcher's optional-dependency handling.

These tests run on a base (client-only) install: they only import
``aprilcam.cli`` and simulate the missing daemon stack via monkeypatching,
so they never require OpenCV / mcp / the daemon extra.
"""

import importlib

import pytest

import aprilcam.cli as cli


def _raise_missing(name):
    """Stand-in for importlib.import_module that fails like a base install."""
    raise ModuleNotFoundError("No module named 'mcp'", name="mcp")


def test_daemon_command_missing_dep_prints_hint(monkeypatch, capsys):
    """A daemon subcommand with the stack absent exits 1 with an install hint."""
    monkeypatch.setattr(importlib, "import_module", _raise_missing)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["mcp"])

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "aprilcam[daemon]" in err
    assert "mcp" in err  # names the missing module
    assert "Traceback" not in err  # no raw traceback


def test_non_daemon_command_reraises_module_error(monkeypatch):
    """A pure-client command does not get the daemon hint; the error propagates."""
    monkeypatch.setattr(importlib, "import_module", _raise_missing)

    # `tool` is a lightweight client command, not in DAEMON_COMMANDS.
    with pytest.raises(ModuleNotFoundError):
        cli.main(["tool"])


def test_all_heavy_subcommands_are_marked():
    """Every subcommand except the pure-client ones is flagged daemon-only."""
    client_only = {"init", "tool", "config"}
    expected = set(cli.SUBCOMMANDS) - client_only
    assert cli.DAEMON_COMMANDS == expected
