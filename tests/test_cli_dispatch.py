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

    # `daemon` is a genuine daemon-only command (still in DAEMON_COMMANDS).
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["daemon"])

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "aprilcam[daemon]" in err
    assert "Traceback" not in err  # no raw traceback


def test_non_daemon_command_reraises_module_error(monkeypatch):
    """A pure-client command does not get the daemon hint; the error propagates."""
    monkeypatch.setattr(importlib, "import_module", _raise_missing)

    # `tool` is a lightweight client command, not in DAEMON_COMMANDS.
    with pytest.raises(ModuleNotFoundError):
        cli.main(["tool"])


def test_all_heavy_subcommands_are_marked():
    """Only the genuinely opencv/daemon-requiring commands are in DAEMON_COMMANDS.

    Since ticket 015-006, `cameras`, `tags`, `view`, `mcp`, and `web` are
    opencv-free thin clients — they no longer print the "[daemon]" hint.
    Only `daemon`, `taggen`, and `calibrate` remain in DAEMON_COMMANDS.
    """
    assert cli.DAEMON_COMMANDS == frozenset({"daemon", "taggen", "calibrate"})


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


def test_main_help_omits_env_var_table_points_to_config(capsys):
    """The APRILCAM_* variable table lives in `aprilcam config --help`, not the
    main help — the main help only points there."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "APRILCAM_DATA_DIR" not in out          # the full table moved out
    assert "aprilcam config --help" in out          # …and the main help points there
