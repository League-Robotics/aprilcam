"""``Guides`` -- the no-daemon-needed guide accessor, client side
(ticket 025-006, UC-27).

Concrete ``client.interfaces.Guides``. Reads **only** from
``aprilcam.guides`` (the shared leaf module also read by
``Daemon.get_guide``, ticket 025-007, next) -- no ``Daemon``, no
``Discovery``, no network, no gRPC channel, no socket. Constructing and
calling this class touches nothing but the package's own packaged text,
which is the literal, mechanical proof of UC-27.S-1's "must work with no
daemon running" property: an agent's first tool call can be a guide
request, before a daemon has ever been started.
"""

from __future__ import annotations

from typing import Optional

from aprilcam import guides
from aprilcam.client.interfaces import Guides as _GuidesBase
from aprilcam.errors import UnknownGuide

__all__ = ["Guides"]


class Guides(_GuidesBase):
    """Concrete ``client.interfaces.Guides``. See the module docstring.

    Derives from ``client.interfaces.Guides`` (a plain base class, not
    ``Protocol`` -- see ``daemon.interfaces``'s module docstring for the
    design rule).
    """

    def get(self, name: Optional[str] = None) -> str:
        """See ``client.interfaces.Guides.get``. An omitted or ``None``
        *name* defaults to ``aprilcam.guides.DEFAULT_GUIDE`` (``"agent"``,
        UC-27's primary actor). An unknown name raises
        :class:`~aprilcam.errors.UnknownGuide` naming the guides that
        actually exist (UC-27.S-3), never empty text.
        """
        chosen = name if name is not None else guides.DEFAULT_GUIDE
        text = guides.read_guide(chosen)
        if text is None:
            raise UnknownGuide(chosen, guides.AVAILABLE_GUIDES)
        return text

    def available(self) -> tuple[str, ...]:
        """See ``client.interfaces.Guides.available``."""
        return guides.AVAILABLE_GUIDES
