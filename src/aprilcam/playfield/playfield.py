"""Concrete ``playfield.interfaces.Playfield`` (ticket 022-001).

``linked_cameras()`` must be **derived at serve time** from the
daemon's held camera configs, never stored in the definition itself
(``playfield/interfaces.py``'s docstring) -- so the frozen dataclass
adds one field beyond the base (``name``, ``definition``): a snapshot
of every :class:`~aprilcam.config.interfaces.CameraConfig` the daemon
holds *at the moment this instance was built*. ``daemon/service.py``
constructs a fresh instance on every ``get_playfield``/``list_playfields``
call with the then-current snapshot, which is what "serve time" means
in practice -- there is no caching or background recomputation here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from aprilcam.config.interfaces import CameraConfig
from aprilcam.playfield.interfaces import Playfield as _PlayfieldBase

__all__ = ["Playfield"]


@dataclass(frozen=True, kw_only=True)
class Playfield(_PlayfieldBase):
    """Concrete ``playfield.interfaces.Playfield``. See the module
    docstring for the ``camera_configs`` snapshot."""

    camera_configs: Tuple[CameraConfig, ...] = ()

    def linked_cameras(self) -> Tuple[str, ...]:
        """See ``Playfield.linked_cameras``. Every camera config in the
        snapshot naming this playfield, in the snapshot's own order."""
        return tuple(c.name for c in self.camera_configs if c.playfield == self.name)
