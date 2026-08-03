"""Mobile-tag mount-registry tools: ``register_tag``, ``unregister_tag``,
``list_tag_parameters`` (ticket 026-004; sprint.md's Architecture Step 3
``mcp/tools/mobile_tags.py`` row: "Exposes mount-parameter registration
as tools. Calls Daemon.register_tag/unregister_tag/list_tag_parameters
only.").

The smallest tool group in the sprint -- three flat wrappers over
``Daemon``'s UC-12 surface, following ticket 002's ``mcp/tools/
perception.py`` precedent exactly: resolve via ``connection.call``
(ticket 001's :class:`~aprilcam.mcp.connection.ConnectionManager`), call
the matching ``Daemon`` method, hand the result back. No tool here does
anything beyond shaping arguments into a
:class:`~aprilcam.types.TagId`/:class:`~aprilcam.types.MountParameters`
pair and calling the one matching ``Daemon`` method -- per
``adapters.md``'s "adapters hold no authoritative state, and none of
them does vision" rule extended to "none of them re-derives daemon
logic": ``register_tag``'s upsert-by-tag semantics, the mount_z/
``NotCalibrated`` precondition (UC-12.S-5), and unregister's
absent-tag-is-a-no-op behaviour all live in ``daemon/service.py`` and
are never reimplemented here.

Every tool here is wrapped by ticket 001's
:func:`~aprilcam.mcp.errors.handle_tool_errors`, registered *after*
``@mcp.tool()`` so ``functools.wraps`` preserves the signature FastMCP's
schema introspection needs, and every tool takes an optional ``daemon``
parameter, matching every other tool group's per-call target-host
override.

**``list_tag_parameters``'s return shape: a flat tuple of pairs, not a
``dict``.** ``Daemon.list_tag_parameters`` returns ``dict[TagId,
MountParameters]`` -- a fine shape in-process, where ``TagId`` is
hashable, but an MCP tool's structured output crosses a JSON boundary,
and JSON object keys must be strings. A dataclass-keyed dict does not
degrade to something an agent can use (pydantic's own ``TypeAdapter``
falls back to stringifying the key via its ``repr()``, e.g.
``"TagId(family='apriltag', number=7)"`` -- not a shape any caller
should have to parse). This is the *exact* problem
``client/models.py``'s ``tag_parameters_from_proto`` docstring already
names one layer out: "proto3 disallows message-typed map keys, so the
wire carries a repeated pair message instead of a native map"
(``aprilcam_v2.proto``'s ``TagMountEntry``). This module hits the same
wall for the same reason one layer further out and takes the same
fix -- :class:`TagMountEntry` below pairs a tag with its parameters, and
``list_tag_parameters`` returns ``tuple[TagMountEntry, ...]``, one entry
per registration. This is presentation shaping, not business logic --
the same category of thing ``mcp/tools/configuration.py``'s
``get_playfield``/``list_playfields`` already do when they return
``PlayfieldDefinition.to_dict()`` instead of the concrete dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.errors import handle_tool_errors
from aprilcam.types import MountParameters, TagFamily, TagId

__all__ = ["register", "TagMountEntry"]


@dataclass(frozen=True)
class TagMountEntry:
    """One ``list_tag_parameters`` entry: a tag paired with its mount
    parameters -- this module's JSON-safe analogue of the wire's own
    ``TagMountEntry`` proto message (see the module docstring for why a
    flat pair, not a dict keyed by tag, is the shape that survives the
    trip through an MCP tool call).
    """

    tag: TagId
    params: MountParameters


def register(mcp: FastMCP, connection: ConnectionManager, temp_dir: Path) -> None:
    """Register every mobile-tag tool on *mcp*, resolving daemons
    through *connection* -- the same locals ``mcp/server.py``'s
    ``build_server`` constructs once per server process and hands to
    every tool-group module (ticket 001's handoff). *temp_dir* is
    accepted only to match that uniform
    ``mcp.tools.*.register(mcp, connection, temp_dir)`` shape; this
    group returns no images, so it never writes under it.
    """

    @mcp.tool()
    @handle_tool_errors
    def register_tag(
        family: TagFamily,
        number: int,
        mount_x: float = 0.0,
        mount_y: float = 0.0,
        mount_z: float = 0.0,
        mount_yaw_rad: float = 0.0,
        size_cm: Optional[float] = None,
        daemon: Optional[str] = None,
    ) -> None:
        """Describe how *family*/*number* is mounted on its robot
        (UC-12), so subsequent ``get_tag``/``get_tags``/streams report
        **the robot's** pose rather than the tag's. Upserts by tag -- a
        second call for the same tag wholly replaces the first, never
        merges -- and takes effect starting with the daemon's next
        detection burst.

        ``mount_x``/``mount_y``/``mount_yaw_rad`` are in the **robot's
        own frame**: ``+x`` forward, ``+y`` left, rotating with
        ``mount_yaw_rad`` -- not the world frame (see
        ``types.MountParameters``'s own docstring for the composed
        math). All offsets default to ``0.0`` -- the tag mounted flush
        at the robot's reference point, facing forward -- and
        ``size_cm`` defaults to ``None``, meaning the daemon's own
        configured default tag size applies.

        **This registration is in-memory only and does NOT survive a
        daemon restart** -- v1's persisted ``tags.json`` is dropped by
        design (UC-12). A client that wants its tag to have a height or
        mount orientation must call this again at the start of every
        session; never assume a previous session's registration is
        still there.

        **``mount_z`` is currently a no-op against real hardware.** It
        is accepted, stored, and fully wired into the parallax-
        projection math, but that math needs a camera's own ground
        position and height above the field, which nothing in this
        codebase models yet -- every camera reports a stopgap
        ``(0, 0)``/``0.0`` today (tracked as its own follow-up issue,
        ``camera-geometry-for-real-parallax-correction.md``). Only
        ``mount_x``/``mount_y``/``mount_yaw_rad`` have a measurable
        effect on a real, live daemon right now. A **nonzero**
        ``mount_z`` is still rejected with a ``NotCalibrated`` tool
        error when this daemon holds no calibration for any camera at
        all (UC-12.S-5) -- registering an elevation with nowhere it
        could ever apply is refused rather than silently stored inert;
        a **zero** ``mount_z`` (the default) never triggers this check.
        """
        tag = TagId(family=family, number=number)
        params = MountParameters(
            size_cm=size_cm,
            mount_x=mount_x,
            mount_y=mount_y,
            mount_z=mount_z,
            mount_yaw_rad=mount_yaw_rad,
        )
        connection.call(lambda d: d.register_tag(tag, params), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def unregister_tag(family: TagFamily, number: int, daemon: Optional[str] = None) -> None:
        """Forget *family*/*number*'s mount registration (UC-12); the
        tag reports as itself again on the next detection burst.
        Unregistering a tag that was never registered, or already
        unregistered, is a **no-op, not an error** -- the daemon's own
        ``Daemon.unregister_tag`` contract, not reimplemented here.
        """
        tag = TagId(family=family, number=number)
        connection.call(lambda d: d.unregister_tag(tag), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def list_tag_parameters(daemon: Optional[str] = None) -> tuple[TagMountEntry, ...]:
        """Every tag currently registered with this daemon, one
        :class:`TagMountEntry` (tag + mount parameters) per
        registration (UC-12) -- see the module docstring for why this
        is a flat tuple of pairs rather than a dict keyed by tag. An
        empty registry (nothing registered, or a daemon that just
        restarted -- registrations do not survive one) returns an
        empty tuple, not an error.
        """
        registrations = connection.call(lambda d: d.list_tag_parameters(), host=daemon)
        return tuple(TagMountEntry(tag=tag, params=params) for tag, params in registrations.items())
