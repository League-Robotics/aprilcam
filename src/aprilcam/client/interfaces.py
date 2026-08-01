"""The client library: the only sanctioned way to talk to a daemon.

Every adapter sits on this — the CLI, the MCP server, the ROS node, and
robot programs all use it rather than speaking the wire protocol
directly. ``docs/design/process.md`` requires every capability to be
reachable from the Python module, with the CLI a shell over it and never
the reverse.

**Contains no OpenCV.** OpenCV is a base dependency so that tag
generation works on any install, but no client module may import it —
the daemon is the sole vision authority. That is now a discipline the
tests enforce rather than something packaging enforces for us.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from aprilcam.daemon.interfaces import Daemon
from aprilcam.types import Annotation, ImageFrame


@runtime_checkable
class DaemonInfo(Protocol):
    """A daemon found on the network (UC-13)."""

    @property
    def host(self) -> str: ...

    @property
    def port(self) -> int: ...

    @property
    def is_local(self) -> bool: ...

    @property
    def reachable(self) -> bool:
        """Whether it answered, as opposed to merely advertising.

        A daemon that advertises but does not respond is exactly what a
        developer needs to see, so it is listed rather than filtered out.
        """
        ...


@runtime_checkable
class Discovery(Protocol):
    """Finding daemons (UC-13)."""

    def find_all(self) -> tuple[DaemonInfo, ...]:
        """Discover daemons via mDNS.

        Finding nothing is an **empty result, not an error** — a field
        with nothing running is a normal state.

        Discovery *failing* (multicast blocked) must be reported
        distinctly from finding nothing (UC-13.S-5). An empty list would
        send someone hunting for a dead daemon instead of a network
        problem.
        """
        ...

    def connect(self, host: Optional[str] = None) -> Daemon:
        """Connect to a named daemon, or the local one if unnamed.

        Selection precedence is flag > env > file > default, so an
        explicit host beats ``APRILCAM_DAEMON_HOST``
        (``docs/design/configuration.md``).
        """
        ...


# ImageStream and TagStream live in aprilcam.daemon.interfaces, next to
# the Daemon methods that return them — unsubscription is a
# daemon-visible event, not client bookkeeping.


@runtime_checkable
class Guides(Protocol):
    """The packaged instructions, readable with **no daemon** (UC-27).

    UC-27.S-1 is explicit: an agent's first question may be "what is
    this and how do I use it?", asked before any daemon exists — so the
    guides ship with the software and this accessor needs nothing but
    the package. ``Daemon.get_guide`` serves the same texts over the
    wire (open there: whether a daemon augments them with live facts).
    """

    def get(self, name: Optional[str] = None) -> str:
        """A guide's text. Unknown name raises ``UnknownGuide``,
        naming the guides that exist (UC-27.S-3)."""
        ...

    def available(self) -> tuple[str, ...]: ...


@runtime_checkable
class Renderer(Protocol):
    """Rasterizes a frame's embedded overlays to an image, client-side.

    **Authoring lives in the daemon; rendering lives here** (spec §9
    #8, UC-28). This replaces the old ``Canvas``: clients no longer
    hold drawing state — they push annotations to the daemon and render
    whatever arrives embedded in the frame. Rendering is the one
    sanctioned pixel step (spec principle 5's render-time carve-out),
    done cv2-free.

    What it draws, all from the frame itself with no vision work and no
    second query:

    * **Tags** — boundary quads and front "hats" from the embedded
      ``TagFrame``'s corners and yaw. On a deskewed frame it converts
      ``world_corners`` via the frame's metadata; on a raw frame it
      uses ``pixel_corners``, published in raw-stream pixel space.
    * **Annotations** — the embedded ``AnnotationSet``, converted
      world → pixel via the frame's metadata (deskewed frames only —
      raw frames carry no annotations by contract). An unchanged
      ``revision`` is licence to reuse the previously rasterized
      overlay.

    A client that rescales the image owns the conversion from then on.
    """

    def render(
        self,
        frame: ImageFrame,
        *,
        layers: Optional[tuple[str, ...]] = None,
        extra: tuple[Annotation, ...] = (),
    ) -> bytes:
        """Render ``frame`` with its overlays to PNG bytes.

        ``layers`` selects which annotation layers to draw (``None`` =
        all) — the viewer's collapsible panels are exactly this
        parameter. ``extra`` is purely local, ad-hoc drawing (debug
        overlays): the same ``Annotation`` types, never sent to the
        daemon — the old Canvas capability without a second vocabulary.
        """
        ...
