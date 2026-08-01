"""Playfields: the surveyed field and queries against it.

A playfield is the coordinate system plus the ground truth of what is
physically on the field and where. It is **not** a camera and not a
service — a definition document held by the daemon, plus the queries it
can answer.

Cardinality: a camera observes exactly one playfield; a playfield has
zero or more cameras.

The playfield is also the **addressing scope for annotations** (UC-28):
drawings live on the field, not on any one camera's view of it. The
annotation RPCs themselves are on ``Daemon`` — this protocol stays a
definition document plus queries.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from aprilcam.config.interfaces import Feature, PlayfieldDefinition


@runtime_checkable
class Playfield(Protocol):
    """A playfield the daemon knows about."""

    @property
    def name(self) -> str: ...

    @property
    def definition(self) -> PlayfieldDefinition: ...

    def linked_cameras(self) -> tuple[str, ...]:
        """Cameras currently observing this playfield.

        **Derived at serve time**, not stored in the definition file. A
        camera's config names its playfield; the link is read back from
        those configs rather than duplicated here, so the two can never
        disagree.
        """
        ...


@runtime_checkable
class FeatureQuery(Protocol):
    """Answering "where is the blue dot?" (UC-08).

    Answers come from the **surveyed definition, not from vision** — so
    this needs no calibration and no camera. That is a real limitation
    worth understanding rather than hiding: if someone physically removed
    the blue dot, this still reports where it is supposed to be. The
    definition is ground truth, not observation.
    """

    def find(self, query: str) -> tuple[Feature, ...]:
        """Fuzzy-match features by name, colour, kind, or cardinal.

        "the blue dot", "blue dot west", and the exact slug all reach
        ``dot-west-blue``.

        **Ambiguity returns every match, never an arbitrary pick**
        (UC-08.S-2): "the orange dot" matches four features on
        ``main-playfield`` and all four come back with their positions.
        Silently choosing one would be a wrong answer that looks right.

        No match returns empty (UC-08.S-3) — asking for "the orange
        triangle" must not return the nearest orange thing as though it
        were a triangle.
        """
        ...
