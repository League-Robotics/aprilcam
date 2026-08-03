"""Perception tools: the read-only query surface over ``Daemon``
(ticket 026-002; sprint.md's Architecture Step 3 ``mcp/tools/
perception.py`` row: "Exposes read-only camera/tag/frame/feature
queries as tools. Calls Daemon query methods only; never mutates daemon
state.").

Every tool here is a thin wrapper, per ``docs/design/adapters.md``'s
"a translation layer and nothing more": resolve a ``Daemon`` via
``connection.call`` (ticket 001's :class:`~aprilcam.mcp.connection.
ConnectionManager`), call the matching ``Daemon`` method, and hand the
result straight back -- FastMCP's own return-type introspection turns
the returned dataclass (or tuple of dataclasses) into structured tool
output with no serialization code of this module's own (verified
directly against the installed ``mcp==1.26.0`` SDK: a dataclass or
``Optional``/``tuple`` of one round-trips through ``pydantic``'s
``TypeAdapter`` exactly like a ``BaseModel`` would). ``get_frame`` is
the one exception that touches ``mcp/images.py``, because encoded image
bytes are the one return shape that is not "hand the dataclass back
verbatim" (ticket 001's ``image_response``, imported unchanged).

No tool here catches anything itself -- every one is wrapped by ticket
001's :func:`~aprilcam.mcp.errors.handle_tool_errors`, registered
*after* ``@mcp.tool()`` so ``functools.wraps`` preserves the signature
FastMCP's schema introspection needs (ticket 001's handoff note; see
``errors.py``'s own module docstring for why that ordering matters).

Every tool takes an optional ``daemon`` parameter -- a per-call target
host that overrides this server's session default
(``aprilcam mcp -d HOST``), via the exact same precedence
``ConnectionManager``/``Discovery.connect`` already implement (ticket
001; sprint.md's Design Rationale, "a session default, plus an optional
per-tool-call daemon parameter"). No tool below re-derives that
precedence -- it is entirely ``connection.call``'s concern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from aprilcam.config.interfaces import Feature
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.errors import handle_tool_errors
from aprilcam.mcp.images import image_response
from aprilcam.types import (
    CameraState,
    ProbeReport,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
)

__all__ = ["register"]


def _tag_id_or_none(family: Optional[TagFamily], number: Optional[int]) -> Optional[TagId]:
    """A :class:`~aprilcam.types.TagId` filter for ``get_tag_history``,
    built only when *both* *family* and *number* are given -- ``None``
    (no filter, every tag's history) when neither is given. Giving
    exactly one without the other names half an identity -- family
    alone or number alone never picks out one tag (UC-09's own "family
    disambiguates" contract) -- so that combination is rejected rather
    than silently guessing which half was meant.
    """
    if family is None and number is None:
        return None
    if family is None or number is None:
        raise ValueError(
            "get_tag_history needs both family and number together to "
            "filter to one tag, or neither to get every tag's history"
        )
    return TagId(family=family, number=number)


def register(mcp: FastMCP, connection: ConnectionManager, temp_dir: Path) -> None:
    """Register every perception tool on *mcp*, resolving daemons
    through *connection* and writing ``format="file"`` image output
    under *temp_dir* -- the exact locals ``mcp/server.py``'s
    ``build_server`` constructs once per server process (ticket 001's
    handoff).
    """

    @mcp.tool()
    @handle_tool_errors
    def list_cameras(daemon: Optional[str] = None) -> tuple[CameraState, ...]:
        """Every camera the daemon's registry knows about (UC-14,
        UC-25) -- name, presence, usability, playfield link,
        calibration state, and its stable persistent number. An absent
        camera (unplugged, but still configured) is listed and flagged
        rather than omitted, and a present-but-broken camera is listed
        distinctly from an absent one -- both straight from
        ``Daemon.list_cameras`` with no filtering here.
        """
        return connection.call(lambda d: d.list_cameras(), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def probe_cameras(daemon: Optional[str] = None) -> ProbeReport:
        """Force a fresh, deep re-interrogation of every camera backend
        (UC-25) and return the full report -- refreshed cameras *and*
        any backend the probe had to skip, each named with a reason
        (UC-25.S-7). A partial probe is never returned as though it
        were complete: whatever ``Daemon.probe_cameras`` reports,
        skipped entries and all, is exactly what this tool returns.
        """
        return connection.call(lambda d: d.probe_cameras(), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def get_frame(
        camera: str,
        deskewed: bool = False,
        encoding: str = "jpeg",
        format: str = "base64",
        daemon: Optional[str] = None,
    ) -> Union[ImageContent, TextContent]:
        """One frame from a named camera (UC-06) -- raw, or the
        deskewed top-down view when *deskewed* is true. Requesting a
        deskewed frame from an uncalibrated camera surfaces the
        daemon's ``NotCalibrated`` error as a tool error; it never
        falls back to a silently-substituted raw frame (UC-06.S-3).

        *format* is per-request, per the issue's own image-return
        contract: ``"base64"`` (default) returns the image inline in
        the tool result; ``"file"`` writes it under this server
        process's temp directory and returns only that path -- never
        both, and never a path the caller did not ask for
        (``mcp/images.py``'s ``image_response``, unchanged from ticket
        001).
        """
        frame = connection.call(
            lambda d: d.get_frame(camera, deskewed=deskewed, encoding=encoding),
            host=daemon,
        )
        return image_response(frame.data, encoding=frame.encoding, format=format, temp_dir=temp_dir)

    @mcp.tool()
    @handle_tool_errors
    def get_tags(camera: str, daemon: Optional[str] = None) -> TagFrame:
        """Every tag currently detected on a named camera (UC-10),
        world-first: each record's ``world`` position is populated when
        the camera is calibrated, ``None`` (with ``pixel`` as the only
        representation) otherwise -- never a fabricated world position.
        An empty field returns an empty frame, not an error.
        """
        return connection.call(lambda d: d.get_tags(camera), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def get_tag(
        camera: str,
        family: TagFamily,
        number: int,
        daemon: Optional[str] = None,
    ) -> Optional[TagRecord]:
        """One tag by family and number (UC-09) -- AprilTag 7 and ArUco
        7 are different tags, and *family* is always given explicitly,
        never inferred from *number* alone (UC-09.S-3). Returns
        ``None`` when the tag is not currently detected -- never a
        fabricated ``(0, 0)`` position, which would be indistinguishable
        from a tag genuinely detected at the field's centre.
        """
        tag = TagId(family=family, number=number)
        return connection.call(lambda d: d.get_tag(camera, tag), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def get_tag_history(
        camera: str,
        family: Optional[TagFamily] = None,
        number: Optional[int] = None,
        frames: Optional[int] = None,
        daemon: Optional[str] = None,
    ) -> tuple[TagFrame, ...]:
        """Recent per-frame tag history from the daemon's ring buffer
        (UC-26, roughly the last 10 seconds) -- enough for a caller to
        compute or verify velocities and trajectories itself. Omitting
        *family*/*number* returns every tag's history; giving both
        filters to one tag's history only (see ``Daemon.
        get_tag_history``'s own ``tag`` parameter). Gaps where a tag
        was absent stay explicit in the returned frames -- nothing here
        interpolates across one.
        """
        tag = _tag_id_or_none(family, number)
        return connection.call(lambda d: d.get_tag_history(camera, tag, frames), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def where(playfield: str, query: str, daemon: Optional[str] = None) -> tuple[Feature, ...]:
        """Where a surveyed feature is, by fuzzy name/colour/kind/
        cardinal match against a playfield's definition (UC-08) --
        answered from the definition itself, not vision, so no
        calibration is required. An ambiguous query (e.g. "the orange
        dot" matching four) returns *every* match rather than picking
        one arbitrarily (UC-08.S-2); no match returns an empty tuple,
        never an error (UC-08.S-3).
        """
        return connection.call(lambda d: d.find_feature(playfield, query), host=daemon)
