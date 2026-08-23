"""Packaged guide text (UC-27) -- the single leaf both accessors read.

``client.guides.Guides`` (this ticket, 025-006) and ``Daemon.get_guide``
(ticket 025-007, next) both call :func:`read_guide` here rather than
holding their own copy of the text -- that is what makes "serves
identical text with or without a daemon" true by construction rather
than by discipline (sprint 025's Design Rationale, "the packaged guide
is static text this sprint"). Pure file/text access: no daemon, no
client dependency, no network -- a leaf, importable and usable before
any daemon exists (UC-27.S-1).

Structurally identical in shape to v1's 21-line
``archive/src/aprilcam/guides.py``: a ``_GUIDE_DIR``-relative pair of
markdown files, a name-to-filename map, and a thin reader. The content
itself is rewritten for v2's conventions, not ported -- v1's guides
describe mechanisms v2 no longer has (``open_camera``/``create_playfield``
MCP tools, mobile-tag MCP tool names, homography JSON files, ``age``-based
tag staleness).

Two guides, matching v1's split:

* ``"agent"`` -- the primary, MCP-agent-facing guide (workflow-first:
  world frame, bring-up order, per-session re-registration rule,
  command overview). The default when no name is given (UC-27, A-02 is
  the primary actor).
* ``"robot"`` -- the Python/CLI robot-programmer-facing guide (the
  high-frequency direct-API path: connecting, reading tags, registering
  a mount, polling, drawing).

Both files are declared in ``pyproject.toml``'s
``[tool.setuptools.package-data]`` so they ship inside the installed
package (wheel/sdist), not just the source checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_GUIDE_DIR = Path(__file__).parent  # src/aprilcam/

_GUIDE_MAP = {
    "agent": "AGENT_GUIDE.md",
    "robot": "ROBOT_API_GUIDE.md",
}

#: The guide served when no name is given -- ``"agent"`` is UC-27's
#: primary actor (A-02, an MCP agent).
DEFAULT_GUIDE = "agent"

#: The guides that exist, in the order named above -- what an
#: ``UnknownGuide`` error (UC-27.S-3) and ``Guides.available()`` report.
AVAILABLE_GUIDES: tuple[str, ...] = tuple(_GUIDE_MAP)


def read_guide(name: str) -> Optional[str]:
    """Return the text of a packaged guide file, or ``None`` if *name*
    is unknown.

    ``name``: ``"agent"`` -> ``AGENT_GUIDE.md``
              ``"robot"`` -> ``ROBOT_API_GUIDE.md``

    Matched case- and whitespace-insensitively (``.lower().strip()``),
    like v1's reader. Returning ``None`` rather than raising here keeps
    this module free of any error-taxonomy dependency (``aprilcam.
    errors``) -- callers (``client.guides.Guides.get``, and
    ``Daemon.get_guide`` next ticket) turn ``None`` into whichever typed
    error their own layer uses.
    """
    filename = _GUIDE_MAP.get(name.lower().strip())
    if filename is None:
        return None
    return (_GUIDE_DIR / filename).read_text(encoding="utf-8")
