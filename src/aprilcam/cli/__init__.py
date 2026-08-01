"""``aprilcam`` — the single CLI entry point (spec §4.9).

**Stub.** ``pyproject.toml`` already declares
``aprilcam = "aprilcam.cli:main"`` in ``[project.scripts]``; this module
is where that name resolves to, so an editable install
(``pip install -e .``) gives a working ``aprilcam`` command and
``aprilcam --help`` runs without error, before any subcommand exists.

Ticket 006 adds the ``daemon`` and ``camera`` subcommands (sprint 020
architecture, Step 3, ``cli/daemon.py`` / ``cli/camera.py``) as thin
argv-to-client-call shells with zero business logic
(``adapters.md``'s "adapters hold no authoritative state" rule). Nothing
here imports ``aprilcam.client`` or ``aprilcam.daemon`` yet — there is
nothing to dispatch to.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence


def _build_parser() -> argparse.ArgumentParser:
    """The top-level ``argparse`` parser.

    A bare ``ArgumentParser`` with no subparsers is deliberate for this
    ticket: it is enough for ``aprilcam --help``/``aprilcam -h`` to
    work and for the entry point to resolve, without inventing command
    shapes ticket 006 owns designing.
    """
    return argparse.ArgumentParser(
        prog="aprilcam",
        description=(
            "AprilCam daemon control, camera management, and fiducial "
            "tag generation."
        ),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Stub for this ticket: parses only ``-h``/``--help`` (and rejects
    anything else, since no subcommands exist yet). Ticket 006 replaces
    the no-op body with subparser dispatch.
    """
    parser = _build_parser()
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via console script
    sys.exit(main())
