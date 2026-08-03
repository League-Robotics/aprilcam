"""Annotation/drawing tools: ``draw_marker``, ``draw_path``,
``draw_polygon``, ``draw_circle``, ``draw_text``, ``remove_annotation``,
``clear_layer``, ``list_annotations`` (ticket 026-005; sprint.md's
Architecture Step 3 ``mcp/tools/annotations.py`` row: "Exposes the
drawing/annotation-store surface as tools. Calls Daemon annotation
methods only; builds types.AnnotationShape values from flat tool
arguments.").

Every tool here is a thin wrapper, per ``docs/design/adapters.md``'s "a
translation layer and nothing more": resolve a ``Daemon`` via
``connection.call`` (ticket 001's :class:`~aprilcam.mcp.connection.
ConnectionManager`), call the matching ``Daemon`` method, hand the
result back. No tool catches anything itself -- every one is wrapped by
ticket 001's :func:`~aprilcam.mcp.errors.handle_tool_errors`, registered
*after* ``@mcp.tool()`` so ``functools.wraps`` preserves the signature
FastMCP's schema introspection needs.

**World coordinates only, no rendering.** Every ``draw_*`` tool takes
flat world-cm arguments (``x``/``y``, ``center_x``/``center_y``/
``radius_cm``, a flat list of ``[x, y]`` pairs for paths/polygons) and
builds the matching :class:`~aprilcam.types.AnnotationShape` variant
(:class:`~aprilcam.types.Marker`/:class:`~aprilcam.types.PathShape`/
:class:`~aprilcam.types.Polygon`/:class:`~aprilcam.types.Circle`/
:class:`~aprilcam.types.Text`), wraps it in an
:class:`~aprilcam.types.Annotation`, and calls
``Daemon.put_annotation``. This module never imports ``cv2`` and never
rasterizes anything -- authoring is a daemon RPC surface,
``client.renderer.Renderer`` is the one place that turns an
``AnnotationSet`` into pixels (spec §9 decision 8). ``clear_layer``
maps onto ``Daemon.clear_annotations`` -- the tool name is the
agent-facing name from spec §4.7; the daemon call underneath differs by
name only, not behaviour.

**``list_annotations`` returns the ``AnnotationSet`` dataclass
directly, unlike ticket 004's ``list_tag_parameters``.**
``AnnotationSet.annotations`` is a plain ``tuple[Annotation, ...]`` --
not a dict keyed by anything dataclass-shaped -- so it does not hit
ticket 004's "dataclass-keyed dict can't cross the JSON tool boundary"
problem at all; ``Annotation.shape``'s ``Union[Marker, PathShape,
Polygon, Circle, Text]`` field serializes cleanly too, because FastMCP's
pydantic ``TypeAdapter`` dumps a union member using its own concrete
runtime type rather than needing to disambiguate on the way out
(verified directly against the installed SDK: a ``list_annotations``-
shaped return round-trips through ``func_metadata().convert_result``
with every shape variant present, each keeping its own fields). No
flattening helper is needed here, unlike ``mobile_tags.py``'s
``TagMountEntry``.

Every tool takes an optional ``daemon`` parameter, matching every other
tool group's per-call target-host override. An unknown *playfield* on
any write (``put_annotation``, ``remove_annotation``,
``clear_annotations``) surfaces the daemon's own ``UnknownPlayfield``,
naming every playfield the daemon currently holds (UC-28.S-5) -- this
module does not pre-validate the playfield name itself, it is entirely
the daemon's contract, passed through by ``handle_tool_errors``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence, Tuple

from mcp.server.fastmcp import FastMCP

from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.errors import handle_tool_errors
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    Circle,
    Marker,
    PathShape,
    Polygon,
    Style,
    Text,
    WorldPoint,
)

__all__ = ["register"]


def _world_points(pairs: Sequence[Tuple[float, float]]) -> Tuple[WorldPoint, ...]:
    """Turn a flat list of ``[x, y]`` pairs (a ``draw_path``/
    ``draw_polygon`` argument, already coerced to ``(float, float)``
    tuples by FastMCP's own JSON-argument validation) into the
    ``WorldPoint`` tuple :class:`~aprilcam.types.PathShape`/
    :class:`~aprilcam.types.Polygon` store.
    """
    return tuple(WorldPoint(x=x, y=y) for x, y in pairs)


def _style(color: Optional[str], line_width: Optional[float]) -> Style:
    """The :class:`~aprilcam.types.Style` every ``draw_*`` tool builds
    from its own ``color``/``line_width`` arguments -- presentation
    hints only, never geometry (``types.Style``'s own docstring).
    """
    return Style(color=color, line_width=line_width)


def register(mcp: FastMCP, connection: ConnectionManager, temp_dir: Path) -> None:
    """Register every annotation/drawing tool on *mcp*, resolving
    daemons through *connection* -- the same locals ``mcp/server.py``'s
    ``build_server`` constructs once per server process and hands to
    every tool-group module (ticket 001's handoff). *temp_dir* is
    accepted only to match that uniform
    ``mcp.tools.*.register(mcp, connection, temp_dir)`` shape; this
    group returns no images, so it never writes under it.
    """

    @mcp.tool()
    @handle_tool_errors
    def draw_marker(
        playfield: str,
        layer: str,
        id: str,
        x: float,
        y: float,
        label: Optional[str] = None,
        color: Optional[str] = None,
        line_width: Optional[float] = None,
        ttl_seconds: Optional[float] = None,
        daemon: Optional[str] = None,
    ) -> None:
        """Draw a point of interest at world position (*x*, *y*) cm,
        optionally labelled (UC-28) -- a robot's goal, a detected
        obstacle, anything worth naming a single point on the field.
        Upserts by ``(layer, id)``: drawing the same *id* again on the
        same *layer* replaces it in place, last write wins
        (UC-28.S-6) -- there is no ownership check. *ttl_seconds*,
        when given, is the annotation's requested lifetime; omitted, it
        never expires on its own. An unknown *playfield* is rejected,
        naming the playfields this daemon currently holds
        (UC-28.S-5) -- ``Daemon.put_annotation``'s own contract, not
        reimplemented here.
        """
        annotation = Annotation(
            id=id,
            layer=layer,
            shape=Marker(at=WorldPoint(x=x, y=y), label=label),
            style=_style(color, line_width),
            ttl_seconds=ttl_seconds,
        )
        connection.call(lambda d: d.put_annotation(playfield, annotation), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def draw_path(
        playfield: str,
        layer: str,
        id: str,
        points: Sequence[Tuple[float, float]],
        color: Optional[str] = None,
        line_width: Optional[float] = None,
        ttl_seconds: Optional[float] = None,
        daemon: Optional[str] = None,
    ) -> None:
        """Draw an open polyline through *points* -- a flat list of
        ``[x, y]`` world-cm pairs, in order -- a route, a trajectory, a
        planned motion (UC-28). Upserts by ``(layer, id)``, same
        last-write-wins semantics as :func:`draw_marker`. An unknown
        *playfield* is rejected, naming what this daemon holds
        (UC-28.S-5).
        """
        annotation = Annotation(
            id=id,
            layer=layer,
            shape=PathShape(points=_world_points(points)),
            style=_style(color, line_width),
            ttl_seconds=ttl_seconds,
        )
        connection.call(lambda d: d.put_annotation(playfield, annotation), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def draw_polygon(
        playfield: str,
        layer: str,
        id: str,
        points: Sequence[Tuple[float, float]],
        color: Optional[str] = None,
        line_width: Optional[float] = None,
        ttl_seconds: Optional[float] = None,
        daemon: Optional[str] = None,
    ) -> None:
        """Draw a closed region through *points* -- a flat list of
        ``[x, y]`` world-cm pairs -- a zone, a keep-out area (UC-28).
        Upserts by ``(layer, id)``, same last-write-wins semantics as
        :func:`draw_marker`. An unknown *playfield* is rejected, naming
        what this daemon holds (UC-28.S-5).
        """
        annotation = Annotation(
            id=id,
            layer=layer,
            shape=Polygon(points=_world_points(points)),
            style=_style(color, line_width),
            ttl_seconds=ttl_seconds,
        )
        connection.call(lambda d: d.put_annotation(playfield, annotation), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def draw_circle(
        playfield: str,
        layer: str,
        id: str,
        center_x: float,
        center_y: float,
        radius_cm: float,
        color: Optional[str] = None,
        line_width: Optional[float] = None,
        ttl_seconds: Optional[float] = None,
        daemon: Optional[str] = None,
    ) -> None:
        """Draw a circular region centred at world position
        (*center_x*, *center_y*) cm with *radius_cm* -- world units,
        like everything (``types.Circle``'s own docstring). Upserts by
        ``(layer, id)``, same last-write-wins semantics as
        :func:`draw_marker`. An unknown *playfield* is rejected, naming
        what this daemon holds (UC-28.S-5).
        """
        annotation = Annotation(
            id=id,
            layer=layer,
            shape=Circle(center=WorldPoint(x=center_x, y=center_y), radius_cm=radius_cm),
            style=_style(color, line_width),
            ttl_seconds=ttl_seconds,
        )
        connection.call(lambda d: d.put_annotation(playfield, annotation), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def draw_text(
        playfield: str,
        layer: str,
        id: str,
        x: float,
        y: float,
        text: str,
        color: Optional[str] = None,
        line_width: Optional[float] = None,
        ttl_seconds: Optional[float] = None,
        daemon: Optional[str] = None,
    ) -> None:
        """Draw a free-standing label *text* anchored at world position
        (*x*, *y*) cm (UC-28). Upserts by ``(layer, id)``, same
        last-write-wins semantics as :func:`draw_marker`. An unknown
        *playfield* is rejected, naming what this daemon holds
        (UC-28.S-5).
        """
        annotation = Annotation(
            id=id,
            layer=layer,
            shape=Text(at=WorldPoint(x=x, y=y), text=text),
            style=_style(color, line_width),
            ttl_seconds=ttl_seconds,
        )
        connection.call(lambda d: d.put_annotation(playfield, annotation), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def remove_annotation(
        playfield: str, layer: str, id: str, daemon: Optional[str] = None
    ) -> None:
        """Delete one annotation by its ``(layer, id)`` key (UC-28).
        Removing an *id* that was never drawn, or already removed, is a
        **no-op, not an error** -- ``Daemon.remove_annotation``'s own
        contract, not reimplemented here.
        """
        connection.call(lambda d: d.remove_annotation(playfield, layer, id), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def clear_layer(
        playfield: str, layer: Optional[str] = None, daemon: Optional[str] = None
    ) -> None:
        """Clear one *layer*, or every annotation on *playfield* when
        *layer* is omitted (UC-28) -- calls ``Daemon.clear_annotations``
        (the tool name is the agent-facing name from spec §4.7; the
        underlying daemon call differs by name only). Clearing a layer
        that is already empty, or does not exist, is a **no-op, not an
        error** -- a known-empty state is the caller's actual goal
        (UC-28's own contract for ``clear_annotations``).
        """
        connection.call(lambda d: d.clear_annotations(playfield, layer), host=daemon)

    @mcp.tool()
    @handle_tool_errors
    def list_annotations(
        playfield: str, layer: Optional[str] = None, daemon: Optional[str] = None
    ) -> AnnotationSet:
        """The current annotation set for *playfield* -- every layer,
        or just *layer* when given -- how a robot reads another robot's
        plan without fetching pixels (UC-28.S-3). After a daemon restart
        this is an empty set at revision 0 (UC-28.S-4): the wipe is the
        contract, not a fault. Returns the ``AnnotationSet`` dataclass
        directly (``annotations``, ``revision``, ``timestamp``) -- see
        the module docstring for why no flattening is needed here,
        unlike ``mobile_tags.py``'s ``list_tag_parameters``.
        """
        return connection.call(lambda d: d.get_annotations(playfield, layer), host=daemon)
