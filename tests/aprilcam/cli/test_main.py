"""Tests for the ``aprilcam.cli`` entry-point stub (ticket 020-001).

Subcommands land in ticket 006; this only checks the entry point itself
resolves and ``--help`` behaves like a normal ``argparse`` CLI, per the
ticket's acceptance criterion that ``aprilcam --help`` runs without
error after an editable install.
"""

from __future__ import annotations

import pytest

from aprilcam.cli import main


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "aprilcam" in out


def test_no_args_returns_zero() -> None:
    assert main([]) == 0


def test_unknown_argument_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--not-a-real-flag"])

    assert excinfo.value.code != 0
