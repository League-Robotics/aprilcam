"""The daemon: sole camera owner and sole vision authority.

Everything else in the system is a thin client of this. The daemon is
kept deliberately small because it is the one component that cannot be
casually restarted — it owns the cameras, holds the calibration, and
every consumer depends on it being up (``docs/design/adapters.md``).

Capability belongs here only when it must: it needs camera access or
vision, two adapters would otherwise duplicate it, or it must be computed
once and shared.
"""

from __future__ import annotations

from typing import Iterator, Optional, Protocol, runtime_checkable

from aprilcam.calibration.interfaces import Calibration, CalibrationResult
from aprilcam.config.interfaces import CameraConfig, Feature, PlayfieldDefinition
from aprilcam.playfield.interfaces import Playfield
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    CameraState,
    ImageFrame,
    MountParameters,
    ProbeReport,
    TagFrame,
    TagId,
    TagRecord,
)


@runtime_checkable
class ImageStream(Protocol):
    """A subscription to frames. Closing it unsubscribes.

    Defined here, next to the method that returns it: unsubscription is
    a **daemon-visible event**, not client bookkeeping. When the last
    subscriber leaves, the daemon drops that product to idle — so
    holding a stream open costs the daemon real work and releasing it
    matters.
    """

    def __iter__(self) -> Iterator[ImageFrame]: ...

    def close(self) -> None: ...


@runtime_checkable
class TagStream(Protocol):
    """A subscription to tag detections. Closing it unsubscribes."""

    def __iter__(self) -> Iterator[TagFrame]: ...

    def close(self) -> None: ...


@runtime_checkable
class Daemon(Protocol):
    """The control plane every client talks to.

    One daemon per machine (enforced by a lock). It advertises itself
    over mDNS so clients can discover it without configuration.

    **Configuration is ephemeral.** Documents pushed here live in memory
    and are discarded on restart; disk is the source of record (UC-01).
    The daemon never writes config — the config root is not writable by
    it, which makes that rule structural rather than remembered.
    """

    # -- cameras ---------------------------------------------------------

    def list_cameras(self) -> tuple[CameraState, ...]:
        """Every camera in the registry, present or not.

        An absent camera is **listed and flagged**, not omitted: a config
        outliving its hardware is normal (UC-25). An unusable camera is
        likewise listed, so "plugged in and broken" stays distinguishable
        from "not plugged in".
        """
        ...

    def probe_cameras(self) -> ProbeReport:
        """Force a deep re-interrogation of all backends (UC-25).

        Returns a report, not a bare list: a backend skipped to protect
        live subscribers, or a wedged subprocess timed out into a
        partial result, is **named in the report** rather than presented
        as a complete probe (UC-25.S-7).
        """
        ...

    def get_camera_config(self, camera: str) -> CameraConfig: ...

    def set_camera_config(self, config: CameraConfig) -> None:
        """Apply a whole camera config, immediately, in memory.

        Targeted by the document's own ``name`` — the caller does not
        name the camera separately (UC-23). Unsupported settings fail the
        **entire** push (UC-03).
        """
        ...

    def reset_camera_config(self, camera: str) -> None:
        """Re-apply the stored on-disk config, discarding live overrides.

        The RPC face of ``Camera.reset()`` — spec §4.1's
        ``ResetCameraConfig``. How an experiment ends without a daemon
        restart: the disk document wins again, immediately.
        """
        ...

    # -- playfields ------------------------------------------------------

    def list_playfields(self) -> tuple[Playfield, ...]: ...

    def get_playfield(self, name: str) -> Playfield: ...

    def set_playfield(self, definition: PlayfieldDefinition) -> None:
        """Adopt a playfield definition, in memory, targeted by its name.

        Rejected whole if malformed, leaving any previous definition
        intact (UC-02.S-2). Replacing a definition that an existing
        calibration was computed against marks that calibration stale.
        """
        ...

    def find_feature(self, playfield: str, query: str) -> tuple[Feature, ...]:
        """Where is the blue dot (UC-08) — spec §4.1's ``WhereIs``.

        The RPC face of ``FeatureQuery.find``: fuzzy match by name,
        colour, kind, or cardinal, answered from the **surveyed
        definition, not vision** — no camera or calibration required.
        Ambiguity returns every match, never an arbitrary pick
        (UC-08.S-2); no match returns empty (UC-08.S-3).
        """
        ...

    # -- calibration -----------------------------------------------------

    def calibrate(self, camera: str) -> CalibrationResult:
        """Run calibration in the daemon (UC-04).

        All vision happens here; the caller sends a command, not pixels.
        """
        ...

    def get_calibration(self, camera: str) -> Optional[Calibration]: ...

    # -- perception ------------------------------------------------------

    def get_frame(
        self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg"
    ) -> ImageFrame:
        """One frame from a named camera (UC-06).

        **Camera-addressed, always** — there is no default camera and no
        playfield-resolves-the-camera shortcut, even when a playfield has
        exactly one camera.

        ``encoding`` selects the delivered format: ``"jpeg"`` (default)
        for ordinary capture, ``"png"`` for **lossless** frames — test
        fixtures (UC-17.S-1) must not carry JPEG artefacts into
        detection tests. The returned ``ImageFrame.encoding`` reports
        what was actually produced. One-shot capture only; streams are
        JPEG, where per-frame loss is the accepted trade.

        A deskewed request on an uncalibrated camera **fails explicitly**
        and never silently substitutes a raw frame. A raw frame that
        looks plausible but is not deskewed is how someone starts
        trusting wrong coordinates.
        """
        ...

    def get_tag(self, camera: str, tag: TagId) -> Optional[TagRecord]:
        """One tag by family and id (UC-09).

        Returns ``None`` for not-detected, which must never be confusable
        with a tag detected at world ``(0, 0)`` — hence Optional rather
        than a zero-valued record.
        """
        ...

    def get_tags(self, camera: str) -> TagFrame:
        """**All** currently detected tags (UC-10).

        No count, no limit, no pagination — hence no truncation ordering
        to define. An empty field returns an empty frame, not an error.
        """
        ...

    def get_tag_history(
        self, camera: str, tag: Optional[TagId] = None, frames: Optional[int] = None
    ) -> tuple[TagFrame, ...]:
        """Recent per-frame history from the ring buffer (UC-26).

        Bounded and lossy by design — roughly 10 seconds. This is recent
        history for motion reasoning, **not** a recording system.

        A client cannot reconstruct this by polling: it would see only
        the frames it happened to ask for, and an MCP agent cannot poll
        anywhere near frame rate. The daemon already computes every
        frame, so this is the only place the full-rate record exists.

        Gaps where a tag was absent are **explicit** in the returned
        records. Interpolating across a gap would invent motion that did
        not happen.
        """
        ...

    # -- streams ---------------------------------------------------------

    def stream_images(
        self, camera: str, *, deskewed: bool = False
    ) -> ImageStream:
        """Subscribe to frames until the consumer stops (UC-07).

        Computed **once and shared**: two subscribers to the same
        deskewed stream cause one warp, not two.

        Backpressure is silent drop from a bounded queue. A slow consumer
        loses frames; it never stalls the pipeline or another subscriber.

        Frame geometry is stable for the life of the stream.
        """
        ...

    def stream_tags(self, camera: str) -> TagStream:
        """Subscribe to tag detections (UC-11).

        Change-driven and rate-capped, with a **heartbeat** so a quiet
        field stays distinguishable from a dead stream.

        If calibration goes stale mid-stream the subscriber is told
        rather than silently receiving plausible wrong positions —
        in-band, via ``TagFrame.calibration_stale`` (UC-11.S-3): a
        stream has no side channel, so the flag rides in the payload.
        """
        ...

    # -- tag parameters --------------------------------------------------

    def register_tag(self, tag: TagId, params: MountParameters) -> None:
        """Describe how a tag is mounted on its robot (UC-12).

        Applied per frame so queries and streams report **the robot's**
        pose rather than the tag's. Runtime registrations, in memory
        only — calibration itself stays robot-agnostic.

        Registrations do **not** survive a daemon restart (v1's
        ``tags.json`` is dropped). A client that expects its tag to
        have a height or mount orientation calls this **at session
        start, every session** — the same reconnect ritual as
        re-drawing annotations (UC-28.S-4).
        """
        ...

    def list_tag_parameters(self) -> dict[TagId, MountParameters]: ...

    def unregister_tag(self, tag: TagId) -> None:
        """Forget a mount registration; the tag reports as itself again.

        Absent key is a no-op. Without this, a session's registry only
        grows — a tag moved to a different robot mid-session would keep
        the old robot's mount until a daemon restart.
        """
        ...

    # -- annotations -----------------------------------------------------
    #
    # The shared drawing surface (UC-28, spec §9 #8). The daemon stores
    # and embeds; it never renders. Runtime state exactly like the
    # mobile-tag registry above: in-memory, record-level, gone on
    # restart, re-drawn by clients on reconnect. Playfield-addressed —
    # observations are camera-addressed, field state is
    # playfield-addressed, and frame embedding joins them via the
    # camera's playfield link.

    def put_annotation(self, playfield: str, annotation: Annotation) -> None:
        """Upsert one annotation by its (layer, id) key (UC-28).

        Last write wins — two writers on one key is documented
        semantics, not a race bug (UC-28.S-6). Every subsequent deskewed
        frame from a camera linked to this playfield embeds the updated
        set.

        An unknown playfield is **rejected**, naming the playfields the
        daemon holds (UC-28.S-5). Open (UC-28): the mobile-tag registry
        accepts registrations for tags not yet seen — the
        accept-and-hold analogy for playfields is recorded, not chosen.
        """
        ...

    def remove_annotation(self, playfield: str, layer: str, id: str) -> None:
        """Delete one annotation. Removing an absent key is a no-op."""
        ...

    def replace_layer(
        self, playfield: str, layer: str, annotations: tuple[Annotation, ...]
    ) -> None:
        """Atomically swap a layer's entire contents (UC-28.S-2).

        The batch path: a planner redrawing its route makes one call,
        and no consumer ever sees a half-updated layer — frames embed
        the old set or the new one, never a mix.
        """
        ...

    def clear_annotations(
        self, playfield: str, layer: Optional[str] = None
    ) -> None:
        """Clear one layer, or everything on the playfield.

        Clearing a layer that does not exist is a **no-op, not an
        error** — a known-empty state is the caller's actual goal.
        """
        ...

    def get_annotations(
        self, playfield: str, layer: Optional[str] = None
    ) -> AnnotationSet:
        """The current set — how a robot reads another robot's plan
        without fetching pixels (UC-28.S-3).

        After a restart this is an empty set at revision 0
        (UC-28.S-4): the wipe is the contract, not a fault.

        No ``stream_annotations`` exists in v2 by decision — embedded
        frames are the publish channel and this is the query.
        """
        ...

    # -- self-description ------------------------------------------------

    def status(self) -> "DaemonStatus":
        """Readiness, queryable by **any** client, remote included
        (UC-05).

        ``DaemonLifecycle.status()`` is the local process view — is
        something running on *this* machine. This is the service view:
        a robot program on another host asking "can I trust positions
        yet" needs it over the wire, and each negative answer names
        which precondition failed (UC-05.S-2).
        """
        ...

    def get_guide(self, name: Optional[str] = None) -> str:
        """The packaged instructions (UC-27).

        An agent works entirely through tool calls and cannot read repo
        files, so the conventions it needs — world frame, tag addressing,
        bring-up order, what "uncalibrated" means — ship with the
        software. Shipping them with the system is what stops them
        drifting from a document nobody updated.
        """
        ...


@runtime_checkable
class DaemonLifecycle(Protocol):
    """Starting and stopping the daemon process (UC-13).

    Process lifecycle is **local-only**. A remote daemon is started over
    SSH, not by this API — which is why ``aprilcam daemon start -d
    remote`` refuses rather than pretending.
    """

    def start(self) -> None:
        """Take the singleton lock, open the control plane, enumerate.

        Refuses if another daemon holds the lock, naming it. Starting
        with **no cameras and no configuration is success**, not an
        error — configuration can arrive before hardware.
        """
        ...

    def stop(self) -> None: ...

    def status(self) -> "DaemonStatus": ...


@runtime_checkable
class DaemonStatus(Protocol):
    """Whether the system is ready to answer questions (UC-05).

    The diagnostic someone reaches for when positions look wrong, so each
    negative answer must name **which** precondition failed and what to
    do about it — not merely report "not ready".
    """

    @property
    def running(self) -> bool: ...

    @property
    def cameras(self) -> tuple[CameraState, ...]: ...

    @property
    def config_dir(self) -> str:
        """Resolved config root, reported so the cascade is never a mystery."""
        ...

    @property
    def state_dir(self) -> str: ...
