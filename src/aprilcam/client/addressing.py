"""Camera address resolution: ``HOST-N`` to ``(host, camera)`` (sprint
048, ticket 003).

A camera argument is normally a slug (``brio-501``) or a persistent
number (``4``). Fan-out listings also show each camera as ``HOST-N``
(``donald-4``), where ``N`` is the camera's persistent registry number
-- not its row position -- and ``HOST`` is the daemon's short host
name. :func:`resolve_camera` turns an argument plus the effective
daemon target into a :class:`ResolvedCamera`, so the CLI and the MCP
``ConnectionManager`` apply identical rules. It depends only on
``Discovery`` and ``Daemon`` (no CLI, MCP, or cv2 imports).

Order (the sprint.md "Resolution order"):

1. No trailing ``-digits``: pass through, no RPC.
2. Otherwise ``list_cameras()`` on the target daemon; an exact slug or
   number match wins (a daemon named ``brio`` must never shadow the
   camera ``brio-501``). Target unreachable: pass through unchanged so
   the normal connection error surfaces.
3. Split at the last ``-``; match ``short_host_name(HOST)`` against
   ``find_all()`` names, case-insensitively. A local hit gives
   ``local=True`` (``host=None``), so no env or session default can
   redirect it.
4. No name hit: direct connect to ``HOST`` as typed, validated with
   ``list_cameras()``; failure raises :class:`UnknownHost`.
5. An explicit target naming a different daemon than the one resolved
   raises :class:`ConflictingTarget`.

Decisions on the sprint's open questions:

* ``Discovery.connect(host)`` may be lazy (a channel exists before any
  RPC), so step 4 validates with ``list_cameras()``, not ``connect``.
* When the addressed daemon is not discoverable (step 4) the target is
  compared with ``HOST`` as typed; if sameness cannot be confirmed
  the result is a conservative ``ConflictingTarget``.
* Only an *explicit* ``target`` (the ``-d`` flag, a per-call ``daemon``)
  is conflict-checked in step 5. A *default* (``default_target``: the
  ``APRILCAM_DAEMON_HOST`` env var, an ``aprilcam mcp -d HOST`` session
  default) is only the daemon used for the step-2 slug/number lookup
  and for pass-through results; ``HOST-N`` overrides it, never
  conflicts with it. ``target`` wins over ``default_target`` when both
  are given. The CLI passes ``args.daemon`` as ``target`` and no
  ``default_target``: ``Discovery.connect(None)`` already applies the
  env default itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from aprilcam.client.discovery import short_host_name, split_host_port
from aprilcam.client.interfaces import DaemonInfo, Discovery
from aprilcam.errors import (
    AprilCamError,
    ConflictingTarget,
    ConnectionFailed,
    UnknownHost,
)

__all__ = ["ResolvedCamera", "resolve_camera"]

_HOST_N = re.compile(r"^(.+)-(\d+)$")
_LOCAL_ALIASES = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, kw_only=True)
class ResolvedCamera:
    """The daemon to ask and the camera argument to send it.

    ``local`` is true when a ``HOST-N`` address named this machine's
    own daemon: connect with ``Discovery.connect_local()``, which
    ignores ``APRILCAM_DAEMON_HOST`` and any session default. Otherwise
    ``host`` is ``None`` when the caller's own target should be used
    unchanged, else a value accepted by ``Discovery.connect``.
    ``camera`` is a slug or a number string.
    """

    host: Optional[str]
    camera: str
    local: bool = False


def _target_host(target: str) -> str:
    """The host part of *target*, without any ``:port``."""
    try:
        return split_host_port(target)[0]
    except Exception:  # unparseable target: compare it as typed
        return target


def _same_daemon(target: str, name: str, address: str, *, is_local: bool) -> bool:
    """Whether explicit *target* names the daemon (*name*, *address*)."""
    if target == address:
        return True
    host = _target_host(target)
    if host and _target_host(address) == host:
        return True
    if name and short_host_name(host) == name:
        return True
    return is_local and host.lower() in _LOCAL_ALIASES


def resolve_camera(
    camera: str,
    *,
    discovery: Discovery,
    target: Optional[str] = None,
    default_target: Optional[str] = None,
) -> ResolvedCamera:
    """Resolve *camera* against the daemon at *target* (explicit; checked
    for conflicts) or, when that is ``None``, *default_target* (never
    conflict-checked); both ``None`` means the local/default daemon.
    See the module docstring for the order.
    """
    effective = target if target is not None else default_target
    match = _HOST_N.match(camera)
    if match is None:
        return ResolvedCamera(host=effective, camera=camera)
    host_part, number = match.group(1), match.group(2)

    try:
        cameras = discovery.connect(effective).list_cameras()
    except ConnectionFailed:
        return ResolvedCamera(host=effective, camera=camera)
    for state in cameras:
        if camera == state.name or camera == str(state.number):
            return ResolvedCamera(host=effective, camera=camera)

    wanted = short_host_name(host_part)
    daemons = discovery.find_all()
    for info in daemons:
        if info.name and info.name.lower() == wanted:
            return _selected(camera, number, info, target)

    try:
        discovery.connect(host_part).list_cameras()
    except AprilCamError as exc:
        raise UnknownHost(
            host_part, tuple(d.name or d.host for d in daemons)
        ) from exc
    if target is not None and not _same_daemon(
        target, wanted, host_part, is_local=False
    ):
        raise ConflictingTarget(camera, host_part, target)
    return ResolvedCamera(host=host_part, camera=number)


def _selected(
    camera: str, number: str, info: DaemonInfo, target: Optional[str]
) -> ResolvedCamera:
    if target is not None and not _same_daemon(
        target, info.name, info.host, is_local=info.is_local
    ):
        raise ConflictingTarget(camera, info.name, target)
    if info.is_local:
        return ResolvedCamera(host=None, camera=number, local=True)
    return ResolvedCamera(host=info.host, camera=number)
