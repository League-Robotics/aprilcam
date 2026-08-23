"""Tests for the ``aprilcam.cli`` entry-point stub (ticket 020-001) and
its ``BrokenPipeError``/``SIGPIPE`` handling (ticket 031-003, SUC-003).

Subcommands land in ticket 006; the ``--help``/no-args/unknown-argument
tests below only check the entry point itself resolves and behaves like
a normal ``argparse`` CLI. The pipe-safety tests exercise ``main()``
directly with a fake dispatch function that raises ``BrokenPipeError``
-- a headless stand-in for a real subcommand's ``print()`` hitting a
closed pipe, matching the ticket's "mock that raises BrokenPipeError on
write" testing-plan wording. A real end-to-end subprocess pipeline
(``aprilcam camera image ... --base64 | head -c 80`` against a fake
daemon) lives in ``test_cli_integration.py``, which already owns that
style of test.
"""

from __future__ import annotations

import argparse
import signal
import sys

import pytest

import aprilcam.cli as cli_module
from aprilcam.cli import _SIGPIPE_EXIT_CODE, main


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


def test_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("aprilcam ")
    assert out.strip() != "aprilcam"


def test_short_version_flag_matches_long_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["-V"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("aprilcam ")


# ---------------------------------------------------------------------------
# BrokenPipeError / SIGPIPE handling -- ticket 031-003, SUC-003
# ---------------------------------------------------------------------------


class _FakeArgs(argparse.Namespace):
    """Stands in for whatever ``argparse`` would have produced --
    ``main()`` only ever reads ``.func`` off the parsed args."""


def _parser_dispatching_to(func) -> object:
    """A fake ``argparse.ArgumentParser``-shaped object whose
    ``parse_args`` always returns *func* as the resolved subcommand --
    isolates ``main()``'s dispatch/exception-handling seam from any
    real subcommand's argparse wiring or business logic."""

    class _FakeParser:
        def parse_args(self, argv):
            return _FakeArgs(func=func)

        def print_help(self) -> None:  # pragma: no cover - not exercised here
            pass

    return _FakeParser()


def test_broken_pipe_from_dispatch_prints_no_traceback_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact scenario the issue names: a subcommand's ``print()``
    raises ``BrokenPipeError`` because a downstream consumer (``head``)
    closed its read end early. ``main()`` must not let the traceback
    reach stderr, and must return a reasonable non-zero exit code."""

    def _raises_broken_pipe(args: argparse.Namespace) -> int:
        raise BrokenPipeError()

    monkeypatch.setattr(
        cli_module, "_build_parser", lambda: _parser_dispatching_to(_raises_broken_pipe)
    )

    code = main([])

    assert code == _SIGPIPE_EXIT_CODE
    assert code != 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "BrokenPipeError" not in err


def test_broken_pipe_exit_code_matches_conventional_sigpipe_termination() -> None:
    """128 + SIGPIPE (13) = 141 -- the same code a shell reports for a
    process actually killed by the signal, so a broken-pipe run looks
    the same to a caller regardless of which of the two layers
    (signal death vs. caught ``BrokenPipeError``) actually handled it."""
    assert _SIGPIPE_EXIT_CODE == 128 + getattr(signal, "SIGPIPE", 13)


@pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="no SIGPIPE on this platform")
def test_main_resets_sigpipe_to_default_disposition() -> None:
    """Belt-and-suspenders half of the fix: ``SIGPIPE`` should no
    longer be ignored (CPython's own startup default) once ``main()``
    has run, restoring conventional Unix-tool signal behaviour."""
    previous = signal.getsignal(signal.SIGPIPE)
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        main([])
        assert signal.getsignal(signal.SIGPIPE) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGPIPE, previous)


def test_broken_pipe_handling_does_not_affect_normal_dispatch_return_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subcommand that succeeds normally is completely unaffected by
    the new try/except -- same return value as before this ticket."""

    def _succeeds(args: argparse.Namespace) -> int:
        return 7

    monkeypatch.setattr(cli_module, "_build_parser", lambda: _parser_dispatching_to(_succeeds))

    assert main([]) == 7


def test_handle_broken_pipe_is_a_no_op_when_stdout_has_no_real_fd(monkeypatch) -> None:
    """``_handle_broken_pipe`` must not itself raise when ``sys.stdout``
    is a test double with no OS-level file descriptor (e.g. under
    ``capsys``) -- it should degrade to "nothing to redirect" instead
    of propagating a fresh exception from the error-handling path."""

    class _NoFilenoStdout:
        def fileno(self) -> int:
            raise AttributeError("no fileno on this stream")

    monkeypatch.setattr(sys, "stdout", _NoFilenoStdout())

    assert cli_module._handle_broken_pipe() == _SIGPIPE_EXIT_CODE
