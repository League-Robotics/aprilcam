"""Tests for the aprilcam CLI dispatcher's optional-dependency handling.

These tests run on a base (client-only) install: they only import
``aprilcam.cli`` and simulate the missing daemon stack via monkeypatching,
so they never require OpenCV / mcp / the daemon extra.
"""

import importlib

import pytest

import aprilcam.cli as cli
from aprilcam.guides import read_guide


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


# ---------------------------------------------------------------------------
# read_guide() helper
# ---------------------------------------------------------------------------

def test_read_guide_agent_returns_content():
    """read_guide('agent') returns the AGENT_GUIDE.md content."""
    content = read_guide("agent")
    assert content is not None
    assert "AprilCam" in content


def test_read_guide_robot_returns_content():
    """read_guide('robot') returns the ROBOT_API_GUIDE.md content."""
    content = read_guide("robot")
    assert content is not None
    assert len(content) > 0


def test_read_guide_unknown_returns_none():
    """read_guide with an unknown name returns None."""
    assert read_guide("unknown") is None
    assert read_guide("") is None


# ---------------------------------------------------------------------------
# --agent CLI flag
# ---------------------------------------------------------------------------

def test_agent_flag_prints_agent_guide(capsys):
    """aprilcam --agent prints AGENT_GUIDE.md to stdout and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--agent"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "AprilCam" in out


def test_agent_flag_robot_prints_robot_guide(capsys):
    """aprilcam --agent robot prints ROBOT_API_GUIDE.md to stdout and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--agent", "robot"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert len(out) > 0


def test_agent_flag_unknown_exits_nonzero(capsys):
    """aprilcam --agent unknown_guide prints error to stderr and exits 1."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--agent", "bad_guide_name"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "bad_guide_name" in err
    assert "Available" in err


def test_help_lists_agent_flag(capsys):
    """aprilcam --help includes --agent in the flags section."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "--agent" in out
