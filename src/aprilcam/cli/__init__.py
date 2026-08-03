"""``aprilcam`` — the single CLI entry point (spec §4.9).

``pyproject.toml`` declares ``aprilcam = "aprilcam.cli:main"`` in
``[project.scripts]``; this module is where that name resolves to, so
an editable install (``pip install -e .``) gives a working ``aprilcam``
command.

Wires the ``daemon`` and ``camera`` subcommands (ticket 020-006,
``cli/daemon.py`` / ``cli/camera.py``) into the ``-h``/``--help``-only
stub ticket 001 left here, plus ``playfield`` (ticket 022-001,
``cli/playfield.py``) and ``tags`` (ticket 025-005, ``cli/tags.py``).
Each subcommand module owns its own argparse shape and dispatch
functions; this module only assembles them and routes ``argv`` to
whichever ``func`` argparse resolved — itself zero business logic, per
``adapters.md``'s "adapters hold no authoritative state" rule.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from aprilcam.cli import camera, daemon, playfield, tags


def _build_parser() -> argparse.ArgumentParser:
    """The top-level ``argparse`` parser: ``-h``/``--help`` plus the
    ``daemon``, ``camera``, ``playfield``, and ``tags`` subcommand
    trees."""
    parser = argparse.ArgumentParser(
        prog="aprilcam",
        description=(
            "AprilCam daemon control, camera management, and fiducial "
            "tag generation."
        ),
    )
    parser.set_defaults(func=None)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    daemon.add_subparser(subparsers)
    camera.add_subparser(subparsers)
    playfield.add_subparser(subparsers)
    tags.add_subparser(subparsers)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: parse *argv* and dispatch to whichever
    subcommand function argparse resolved.

    No subcommand at all (``aprilcam`` alone) prints top-level help and
    returns 0 — an empty invocation is not a usage error. A subcommand
    tree entered without a leaf command (e.g. ``aprilcam daemon`` with
    no ``start``/``stop``/``status``) is handled by that subparser's
    own ``incomplete_command`` default (``cli/_common.py``), which
    returns non-zero.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = args.func
    if func is None:
        parser.print_help()
        return 0
    return func(args)


if __name__ == "__main__":  # pragma: no cover - exercised via console script
    sys.exit(main())
