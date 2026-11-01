"""``aprilcam`` — the single CLI entry point (spec §4.9).

``pyproject.toml`` declares ``aprilcam = "aprilcam.cli:main"`` in
``[project.scripts]``; this module is where that name resolves to, so
an editable install (``pip install -e .``) gives a working ``aprilcam``
command.

Wires the ``daemon`` and ``camera`` subcommands (ticket 020-006,
``cli/daemon.py`` / ``cli/camera.py``) into the ``-h``/``--help``-only
stub ticket 001 left here, plus ``playfield`` (ticket 022-001,
``cli/playfield.py``), ``tags`` (ticket 025-005, ``cli/tags.py``),
``guide`` (ticket 025-007, ``cli/guide.py`` -- the one subcommand that
deliberately never talks to a daemon; see that module's docstring),
``mcp`` (ticket 026-001, ``cli/mcp.py`` -- starts the MCP stdio server,
UC-22), and ``config`` (OOP change, 2026-08-22, ``cli/config.py`` --
reports resolved config/state/socket roots, env vars, and file
locations; the second subcommand, after ``guide``, that never talks to
a daemon).
Each subcommand module owns its own argparse shape and dispatch
functions; this module only assembles them and routes ``argv`` to
whichever ``func`` argparse resolved — itself zero business logic, per
``adapters.md``'s "adapters hold no authoritative state" rule.

**Pipe safety (ticket 031-003, SUC-003).** ``main()`` is the single
dispatch seam every subcommand's ``func(args)`` funnels through, so
``BrokenPipeError`` handling lives here once rather than at each of the
~20 individual ``print()`` call sites scattered across ``cli/*.py`` --
see :func:`main`'s own docstring for the mechanism.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Optional, Sequence

from aprilcam.cli import camera, config, daemon, guide, mcp, playfield, tags
from aprilcam.cli._common import (
    add_daemon_option,
    add_verbose_option,
    add_verbose_to_subparsers,
    configure_logging,
)


def _package_version() -> str:
    """The installed ``aprilcam`` distribution version, read from
    package metadata -- the same mechanism ``testdev/capture_matrix.py``
    already uses, rather than ``aprilcam.__version__`` (a hardcoded,
    stale placeholder never wired to the ``dotconfig``-managed version
    that ``pyproject.toml`` actually carries). Falls back to
    ``"unknown"`` for an editable/zipapp/frozen context where package
    metadata isn't discoverable, so ``--version`` degrades gracefully
    instead of crashing.
    """
    try:
        return version("aprilcam")
    except PackageNotFoundError:
        return "unknown"


#: Conventional Unix exit code for a process terminated by ``SIGPIPE``
#: (128 + signal number) -- used whether the process is actually killed
#: by the signal itself (see :func:`_reset_sigpipe_to_default`) or a
#: ``BrokenPipeError`` reaches :func:`main`'s own handling instead
#: (belt-and-suspenders path, e.g. platforms/contexts where the signal
#: reset does not apply or the write already raced past it). ``13`` is
#: ``SIGPIPE``'s POSIX value, used as the literal fallback on platforms
#: (Windows) that have no ``signal.SIGPIPE`` at all -- this module never
#: reaches that fallback there, since :func:`_reset_sigpipe_to_default`
#: is a no-op and a plain ``print()`` to a closed handle raises
#: ``OSError``, not ``BrokenPipeError``, on Windows anyway.
_SIGPIPE_EXIT_CODE = 128 + getattr(signal, "SIGPIPE", 13)


def _build_parser() -> argparse.ArgumentParser:
    """The top-level ``argparse`` parser: ``-h``/``--help``, ``-V``/
    ``--version`` (argparse's standard ``action="version"``, which
    prints and exits before any subcommand dispatch runs), plus the
    ``daemon``, ``camera``, ``playfield``, ``tags``, ``guide``,
    ``config``, and ``mcp`` subcommand trees."""
    parser = argparse.ArgumentParser(
        prog="aprilcam",
        description=(
            "AprilCam daemon control, camera management, and fiducial "
            "tag generation."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"aprilcam {_package_version()}",
    )
    # Once, here, for every subcommand: `aprilcam -d HOST camera list`.
    # Commands that never talk to a daemon (guide, config, tags generate,
    # daemon list) ignore it.
    add_daemon_option(parser)
    add_verbose_option(parser)
    parser.set_defaults(func=None)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    daemon.add_subparser(subparsers)
    camera.add_subparser(subparsers)
    playfield.add_subparser(subparsers)
    tags.add_subparser(subparsers)
    guide.add_subparser(subparsers)
    config.add_subparser(subparsers)
    mcp.add_subparser(subparsers)
    add_verbose_to_subparsers(parser)
    return parser


def _reset_sigpipe_to_default() -> None:
    """Restore ``SIGPIPE``'s default disposition on POSIX, undoing
    CPython's own startup behaviour of ignoring it (``SIG_IGN``) so
    that a write to a pipe with no reader raises ``BrokenPipeError``
    instead of being ignored outright.

    Resetting to ``SIG_DFL`` here gives ``aprilcam`` the conventional
    Unix-tool behaviour: the OS can kill the process directly on a
    broken-pipe write, before any Python-level exception machinery (or
    interpreter-shutdown stdout flush) ever runs -- no traceback is
    possible in that path because no Python code runs at all. This is
    belt-and-suspenders alongside :func:`main`'s own ``except
    BrokenPipeError`` handling, which still catches the case where a
    ``BrokenPipeError`` reaches Python anyway (e.g. the write races
    past the signal, or a context that does not deliver the signal the
    same way). A no-op on Windows, which has no ``SIGPIPE`` at all --
    hence the ``getattr`` guard rather than a bare ``signal.SIGPIPE``
    reference.
    """
    sigpipe = getattr(signal, "SIGPIPE", None)
    if sigpipe is not None:
        signal.signal(sigpipe, signal.SIG_DFL)


def _handle_broken_pipe() -> int:
    """Handle a ``BrokenPipeError`` raised by a subcommand's
    ``func(args)`` -- the standard Python idiom for a CLI piped into a
    truncating consumer (``| head``, a killed downstream pipeline, ...).

    Redirects the ``stdout`` file descriptor to ``os.devnull`` before
    returning: CPython's normal interpreter shutdown flushes
    ``sys.stdout`` regardless of how ``main()`` returns, and without
    this redirect that flush would itself raise a second
    ``BrokenPipeError`` against the still-broken pipe -- which CPython
    reports as "Exception ignored in: ..." noise on stderr during
    finalization, exactly the traceback-shaped output this ticket
    exists to suppress. Best-effort: ``sys.stdout`` may not expose a
    real file descriptor in some contexts (e.g. a test double standing
    in for a broken pipe without a real one), in which case there is
    nothing at the OS level to redirect and this is a no-op.
    """
    try:
        target_fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        target_fd = None
    if target_fd is not None:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, target_fd)
        finally:
            os.close(devnull_fd)
    return _SIGPIPE_EXIT_CODE


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: parse *argv* and dispatch to whichever
    subcommand function argparse resolved.

    No subcommand at all (``aprilcam`` alone) prints top-level help and
    returns 0 — an empty invocation is not a usage error. A subcommand
    tree entered without a leaf command (e.g. ``aprilcam daemon`` with
    no ``start``/``stop``/``status``) is handled by that subparser's
    own ``incomplete_command`` default (``cli/_common.py``), which
    returns non-zero.

    Pipe safety (ticket 031-003, SUC-003): every subcommand's
    ``print()`` calls funnel through this one dispatch point, so a
    downstream consumer closing its read end early (``camera image
    <camera> --base64 | head``) is handled once, here -- see
    :func:`_reset_sigpipe_to_default` and :func:`_handle_broken_pipe`
    for the two-layer mechanism.
    """
    _reset_sigpipe_to_default()
    parser = _build_parser()
    args = parser.parse_args(argv)
    # `-v` before the subcommand counts into `verbose`, after it into
    # `sub_verbose`; one merged count is what every command reads.
    args.verbose = getattr(args, "verbose", 0) + getattr(args, "sub_verbose", 0)
    configure_logging(args.verbose)
    func = args.func
    if func is None:
        parser.print_help()
        return 0
    try:
        return func(args)
    except BrokenPipeError:
        return _handle_broken_pipe()


if __name__ == "__main__":  # pragma: no cover - exercised via console script
    sys.exit(main())
