"""The daemon: sole camera owner and sole vision authority.

Everything else in the system is a thin client of this. The daemon is
kept deliberately small because it is the one component that cannot be
casually restarted — it owns the cameras, holds the calibration, and
every consumer depends on it being up (``docs/design/adapters.md``).

Capability belongs here only when it must: it needs camera access or
vision, two adapters would otherwise duplicate it, or it must be computed
once and shared.

Interface design rule — every interface is connected by inheritance
(read before converting or adding an interface anywhere in
``*/interfaces.py``)
--------------------------------------------------------------------
This codebase declares no ``typing.Protocol``. Every interface, of
either shape below, is a real class that its implementations *derive*
from — a mechanical connection, so ``isinstance`` is truthful and there
is never a base and an unrelated same-named implementation that merely
happen to look alike (the bug this rule replaces: ``camera/
interfaces.py`` used to declare a structural ``CameraIdentity`` while
``camera/identity.py`` declared an entirely different dataclass of the
same name — two classes, zero relationship). Two shapes of contract
live in this codebase's ``interfaces.py`` modules, handled two
different ways:

* **Behavioral interfaces with real implementations** — ``Daemon``,
  ``DaemonLifecycle``, ``ImageStream``, ``TagStream`` here;
  ``CameraDiscovery``, ``CameraRegistry``, ``Camera`` in
  ``camera/interfaces.py``; ``Directories`` in ``config/interfaces.py``;
  ``Discovery`` in ``client/interfaces.py`` — are **plain base
  classes**. Their method bodies ``raise NotImplementedError(...)``
  instead of ``...``. Implementations derive from them, so calling an
  unimplemented method fails loudly with a clear message at the call
  site instead of an ``AttributeError`` you have to trace back to a
  missing method.

  **Do NOT use ``abc.ABC`` / ``@abstractmethod`` for these — deliberate,
  not an oversight.** The v2 rebuild is staged over many sprints, and
  e.g. ``DaemonService`` implements only 4 of ``Daemon``'s 27 methods on
  purpose; the rest belong to later sprints. ``ABC`` would make the
  class un-instantiable until all 27 existed, forcing 23 hollow
  ``raise NotImplementedError`` stubs onto ``DaemonService`` just to
  satisfy the metaclass — which would make the object *look* complete
  when it is not. A plain base class allows an honest partial
  implementation while still failing loudly on the parts that are not
  built yet. If you are tempted to "fix" this to ``ABC`` because an
  un-instantiable abstract base class feels more rigorous: don't.
  Re-read this paragraph first.

* **Value-shaped interfaces** — ``DaemonStatus`` here; ``DaemonInfo`` in
  ``client/interfaces.py``; ``CameraIdentity`` in
  ``camera/interfaces.py``; ``ConfigDocument``, ``CameraConfig``,
  ``PlayfieldDefinition``, ``Feature`` in ``config/interfaces.py``;
  ``Calibration``, ``CalibrationResult`` in
  ``calibration/interfaces.py`` — are **frozen ``@dataclass``\\ es
  declaring fields**, not properties. Implementations either derive
  from the interface dataclass (adding fields the base does not have)
  or, when their shape is identical, simply *are* the interface
  dataclass — there is no separate "implementation" class at all
  (``camera.identity.CameraIdentity`` is an import of
  ``camera.interfaces.CameraIdentity``, not a second definition of it;
  where a sprint's implementation was previously a same-shaped sibling
  dataclass — e.g. ``daemon.service.SimpleDaemonStatus`` next to this
  module's ``DaemonStatus`` — it has been deleted in favour of
  constructing this class directly).

  **Why fields, never properties, on these dataclasses.** A ``@property``
  is a data descriptor. If a base class declared ``slug`` as a
  ``@property`` and a ``@dataclass`` subclass declared a ``slug``
  field, the property object would become that field's *default
  value* — and because a data descriptor takes precedence over instance
  ``__dict__``, every read of ``instance.slug`` would call the
  property's getter (which typically raises or returns the wrong thing)
  instead of returning the stored field. The dataclass would be
  silently broken, not loudly. Declaring plain fields instead sidesteps
  the hazard entirely: a field and a subclass's same-named field simply
  agree. Regular *methods* (not properties) are unaffected by this and
  are used freely on these dataclasses for real computed behaviour
  (e.g. a distance calculation over two fields) or, for a method with no
  implementation yet, the same ``raise NotImplementedError(...)`` body
  as the behavioral interfaces above.

  All of these dataclasses use ``@dataclass(frozen=True, kw_only=True)``:
  ``kw_only=True`` sidesteps the "non-default field cannot follow a
  default field" ordering problem across a chain of subclasses adding
  fields, at the cost of construction being keyword-only everywhere —
  an accepted, codebase-wide trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from aprilcam.calibration.interfaces import Calibration, CalibrationResult, FlatFieldInfo, FlatFieldResult
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


class ImageStream:
    """A subscription to frames. Closing it unsubscribes.

    Defined here, next to the method that returns it: unsubscription is
    a **daemon-visible event**, not client bookkeeping. When the last
    subscriber leaves, the daemon drops that product to idle — so
    holding a stream open costs the daemon real work and releasing it
    matters.

    A plain base class, not ``Protocol`` — see the module docstring's
    design rule. No implementation exists yet (streaming is a later
    sprint); this is here so whichever sprint adds one derives from it
    rather than duck-typing against the shape.
    """

    def __iter__(self) -> Iterator[ImageFrame]:
        raise NotImplementedError(f"{type(self).__name__} does not implement ImageStream.__iter__")

    def close(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not implement ImageStream.close")


class TagStream:
    """A subscription to tag detections. Closing it unsubscribes.

    A plain base class, not ``Protocol`` — see the module docstring's
    design rule. No implementation exists yet (streaming is a later
    sprint).
    """

    def __iter__(self) -> Iterator[TagFrame]:
        raise NotImplementedError(f"{type(self).__name__} does not implement TagStream.__iter__")

    def close(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not implement TagStream.close")


@dataclass(frozen=True)
class MountRegistration:
    """One :meth:`Daemon.list_tag_parameters` entry (ticket 030-004,
    SUC-004): a tag's registered :class:`~aprilcam.types.MountParameters`
    paired with whether its ``mount_z`` is currently being *applied* —
    computed, not stored, alongside every read of the registry.

    ``mount_z_applied`` is ``False`` only when ``params.mount_z > 0``
    and no camera this daemon knows about has been located yet (no
    ``CameraConfig.camera_position`` anywhere — the same "any camera"
    scope :meth:`Daemon.register_tag`'s ``NotCalibrated`` guard already
    uses; a mobile tag is not bound to one specific camera). It is
    ``True`` in every other case, including ``mount_z <= 0`` — there is
    nothing for the parallax correction to withhold when there is no
    elevation to project in the first place.

    Sprint 029 shipped the parallax math with this precondition
    unannounced: a calibrated-but-never-``locate``\\ d camera silently
    served the raw tag position while ``mount_x``/``mount_y``/
    ``mount_yaw_rad`` kept applying normally. This field, plus
    :attr:`Daemon.register_tag`'s own returned warning, is sprint
    030's fix — see sprint.md's Architecture, Design Rationale
    Decision 4 (why the fix is observability, not a stricter guard).
    """

    params: MountParameters
    mount_z_applied: bool


class Daemon:
    """The control plane every client talks to.

    A plain base class, not ``Protocol`` — see the module docstring's
    design rule. Every method below ``raise``\\ s ``NotImplementedError``
    by default; a concrete subclass overrides only the methods it has
    actually built, and calling an unbuilt one fails loudly rather than
    with a bare ``AttributeError``.

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.list_cameras")

    def probe_cameras(self) -> ProbeReport:
        """Force a deep re-interrogation of all backends (UC-25).

        Returns a report, not a bare list: a backend skipped to protect
        live subscribers, or a wedged subprocess timed out into a
        partial result, is **named in the report** rather than presented
        as a complete probe (UC-25.S-7).
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.probe_cameras")

    def get_camera_config(self, camera: str) -> CameraConfig:
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_camera_config")

    def set_camera_config(self, config: CameraConfig) -> None:
        """Apply a whole camera config, immediately, in memory.

        Targeted by the document's own ``name`` — the caller does not
        name the camera separately (UC-23). Unsupported settings fail the
        **entire** push (UC-03).
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.set_camera_config")

    def reset_camera_config(self, camera: str) -> None:
        """Re-apply the stored on-disk config, discarding live overrides.

        The RPC face of ``Camera.reset()`` — spec §4.1's
        ``ResetCameraConfig``. How an experiment ends without a daemon
        restart: the disk document wins again, immediately.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.reset_camera_config")

    # -- playfields ------------------------------------------------------

    def list_playfields(self) -> tuple[Playfield, ...]:
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.list_playfields")

    def get_playfield(self, name: str) -> Playfield:
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_playfield")

    def set_playfield(self, definition: PlayfieldDefinition) -> None:
        """Adopt a playfield definition, in memory, targeted by its name.

        Rejected whole if malformed, leaving any previous definition
        intact (UC-02.S-2). Replacing a definition that an existing
        calibration was computed against marks that calibration stale.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.set_playfield")

    def find_feature(self, playfield: str, query: str) -> tuple[Feature, ...]:
        """Where is the blue dot (UC-08) — spec §4.1's ``WhereIs``.

        The RPC face of ``FeatureQuery.find``: fuzzy match by name,
        colour, kind, or cardinal, answered from the **surveyed
        definition, not vision** — no camera or calibration required.
        Ambiguity returns every match, never an arbitrary pick
        (UC-08.S-2); no match returns empty (UC-08.S-3).
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.find_feature")

    # -- calibration -----------------------------------------------------

    def calibrate(self, camera: str, *, estimate_intrinsics: bool = False) -> CalibrationResult:
        """Run calibration in the daemon (UC-04).

        All vision happens here; the caller sends a command, not pixels.

        *estimate_intrinsics* (sprint 030 ticket 002, SUC-002) opts into
        also estimating this camera's lens model (``camera_matrix``/
        ``dist_coeffs``) from the same detection burst, over whatever
        ArUco+AprilTag ground truth the linked playfield surveys --
        never re-estimated on an ordinary (``False``, the default) call,
        which instead carries forward whatever lens model this camera's
        prior successful calibration already held.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.calibrate")

    def get_calibration(self, camera: str) -> Optional[Calibration]:
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_calibration")

    # -- flat-field gain map (sprint 037) ---------------------------------

    def capture_flat_field(
        self,
        camera: str,
        *,
        exposure: Optional[float] = None,
        frames: Optional[int] = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> FlatFieldResult:
        """Build and (unless refused or *dry_run*) persist a per-camera
        flat-field gain map from a fresh capture burst.

        All vision/math happens here in the daemon, on this camera's
        current view of its field -- the caller sends a command, not
        pixels, exactly like :meth:`calibrate`. The field-interior mask
        the gain map is built against is derived from whatever this
        daemon already knows about *camera*: its stored
        :class:`~aprilcam.calibration.interfaces.Calibration` when one
        exists, else its linked playfield's required ArUco corners
        detected live. Neither is available:
        :class:`~aprilcam.errors.NoFieldMask` is raised **before any
        capture burst is taken**, mirroring :meth:`calibrate`'s
        ``NoPlayfieldLinked`` precondition discipline exactly.

        *exposure*, when given, is pushed as a temporary override before
        the burst and the camera's prior stored/live configuration is
        restored afterward -- on success, a refusal, or an exception
        alike -- so a capture attempt never leaves a camera parked at the
        low exposure it needed only for the reference frames.

        A failing quality-gate check is **explicit failure, not an
        error**: this returns ``FlatFieldResult(succeeded=False,
        reason=...)``, the same "explicit failure is data" contract
        :class:`~aprilcam.calibration.interfaces.CalibrationResult`
        already uses, never raises for that reason. *force* overrides a
        failing check and still persists the result, naming what it
        overrode via ``FlatFieldResult.forced_checks``. *dry_run*
        evaluates and reports without writing anything to disk or
        changing the live-applied map.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement Daemon.capture_flat_field"
        )

    def get_flatfield(self, camera: str) -> Optional[FlatFieldInfo]:
        """This camera's current flat-field gain-map metadata, or
        ``None`` when it has never had one built (or it was
        :meth:`clear_flat_field`\\ d) -- never a fabricated/default
        record, mirroring :meth:`get_calibration`'s own "``None`` means
        never, not a zero-valued record" discipline.

        Answered from an in-memory cache reloaded at daemon construction
        (UC-01: file-backed state survives a restart, like calibration).
        A pre-existing ``flatfield.npy`` from the manual capture recipe,
        with no ``flatfield.json`` sidecar, reports ``present=True,
        captured_at=None`` -- never a fabricated timestamp.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_flatfield")

    def clear_flat_field(self, camera: str) -> None:
        """Delete *camera*'s stored gain map and metadata sidecar, and
        drop the live-applied map on a held-open camera -- no daemon
        restart, mirroring how a fresh ``flatfield.npy`` takes effect
        live via :meth:`capture_flat_field`. Absent files are a no-op,
        matching :meth:`unregister_tag`'s established
        no-op-on-already-clear convention.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.clear_flat_field")

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_frame")

    def get_tag(self, camera: str, tag: TagId) -> Optional[TagRecord]:
        """One tag by family and id (UC-09).

        Returns ``None`` for not-detected, which must never be confusable
        with a tag detected at world ``(0, 0)`` — hence Optional rather
        than a zero-valued record.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_tag")

    def get_tags(self, camera: str) -> TagFrame:
        """**All** currently detected tags (UC-10).

        No count, no limit, no pagination — hence no truncation ordering
        to define. An empty field returns an empty frame, not an error.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_tags")

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_tag_history")

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.stream_images")

    def stream_tags(self, camera: str) -> TagStream:
        """Subscribe to tag detections (UC-11).

        Change-driven and rate-capped, with a **heartbeat** so a quiet
        field stays distinguishable from a dead stream.

        If calibration goes stale mid-stream the subscriber is told
        rather than silently receiving plausible wrong positions —
        in-band, via ``TagFrame.calibration_stale`` (UC-11.S-3): a
        stream has no side channel, so the flag rides in the payload.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.stream_tags")

    # -- tag parameters --------------------------------------------------

    def register_tag(self, tag: TagId, params: MountParameters) -> Optional[str]:
        """Describe how a tag is mounted on its robot (UC-12).

        Applied per frame so queries and streams report **the robot's**
        pose rather than the tag's. Calibration itself stays
        robot-agnostic — a mount registration is a separate, per-tag
        overlay applied on top of it.

        **Registrations persist across a daemon restart** (ticket
        036-007; v1's ``tags.json`` is the precedent this revives, in a
        new on-disk shape — see ``daemon/service.py``'s
        ``_persist_mounts``/``_load_mounts``). A mount registration
        describes physical reality — where a tag is bolted to a robot —
        not a per-session experiment, so a client registers a tag once
        and it stays registered until explicitly
        :meth:`unregister_tag`\\ d, with no re-registration ritual at
        the start of every session. (Annotations, the other per-session
        state UC-28.S-4 describes, are unaffected by this ticket and
        remain in-memory only.)

        **Returns an optional warning string** (ticket 030-004,
        SUC-004; formerly always ``None``) — non-``None`` exactly when
        a nonzero ``params.mount_z`` was just accepted while no camera
        this daemon knows about has been located, meaning the parallax
        correction has nowhere to apply yet (see
        :class:`MountRegistration`'s ``mount_z_applied`` for the same
        condition, read back later via :meth:`list_tag_parameters`).
        This is purely informational — registration always succeeds
        and never raises for this reason; only the pre-existing
        ``NotCalibrated`` precondition (no calibration anywhere) still
        raises, unchanged.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.register_tag")

    def list_tag_parameters(self) -> dict[TagId, MountRegistration]:
        """Every currently registered tag's mount parameters, paired
        with whether ``mount_z`` is currently applied (ticket 030-004;
        see :class:`MountRegistration`). Returned value type widened
        from ``MountParameters`` alone -- every existing caller reads
        ``entry.params`` for what it held directly before."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.list_tag_parameters")

    def unregister_tag(self, tag: TagId) -> None:
        """Forget a mount registration; the tag reports as itself again.

        Absent key is a no-op. Without this, the registry only grows —
        a tag moved to a different robot would keep the old robot's
        mount indefinitely: registrations persist across a daemon
        restart (ticket 036-007), so a restart no longer clears a stale
        one either. This is the only way to remove a mount registration
        short of hand-editing ``mounts/registry.json``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.unregister_tag")

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.put_annotation")

    def remove_annotation(self, playfield: str, layer: str, id: str) -> None:
        """Delete one annotation. Removing an absent key is a no-op."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.remove_annotation")

    def replace_layer(
        self, playfield: str, layer: str, annotations: tuple[Annotation, ...]
    ) -> None:
        """Atomically swap a layer's entire contents (UC-28.S-2).

        The batch path: a planner redrawing its route makes one call,
        and no consumer ever sees a half-updated layer — frames embed
        the old set or the new one, never a mix.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.replace_layer")

    def clear_annotations(
        self, playfield: str, layer: Optional[str] = None
    ) -> None:
        """Clear one layer, or everything on the playfield.

        Clearing a layer that does not exist is a **no-op, not an
        error** — a known-empty state is the caller's actual goal.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.clear_annotations")

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_annotations")

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.status")

    def get_guide(self, name: Optional[str] = None) -> str:
        """The packaged instructions (UC-27).

        An agent works entirely through tool calls and cannot read repo
        files, so the conventions it needs — world frame, tag addressing,
        bring-up order, what "uncalibrated" means — ship with the
        software. Shipping them with the system is what stops them
        drifting from a document nobody updated.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Daemon.get_guide")


class DaemonLifecycle:
    """Starting and stopping the daemon process (UC-13).

    A plain base class, not ``Protocol`` — see the module docstring's
    design rule. Two independent implementations derive from this (the
    server-side ``daemon.lifecycle.DaemonLifecycle`` and the client-side
    ``client.lifecycle.DaemonLifecycle`` — see their module docstrings
    for why the same name, same base, and genuinely different mechanics
    are correct here), both implementing every method, so there are no
    ``NotImplementedError`` stubs left in practice — the base class
    still exists for the mechanical ``isinstance`` connection and so a
    future third implementation fails loudly on anything it skips.

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
        raise NotImplementedError(f"{type(self).__name__} does not implement DaemonLifecycle.start")

    def stop(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not implement DaemonLifecycle.stop")

    def status(self) -> "DaemonStatus":
        raise NotImplementedError(f"{type(self).__name__} does not implement DaemonLifecycle.status")


@dataclass(frozen=True, kw_only=True)
class DaemonStatus:
    """Whether the system is ready to answer questions (UC-05).

    The diagnostic someone reaches for when positions look wrong, so each
    negative answer must name **which** precondition failed and what to
    do about it — not merely report "not ready".

    A frozen dataclass, directly constructed — see the module docstring's
    design rule. There used to be three same-shaped sibling dataclasses
    (``daemon.service.SimpleDaemonStatus``, ``client.models.
    RemoteDaemonStatus``, ``client.lifecycle._LocalStatus``, plus
    ``daemon.lifecycle._NotRunningStatus``) each merely structurally
    matching this one; since all four had identical fields, they have
    been deleted in favour of every call site constructing
    :class:`DaemonStatus` itself — one class, not four that happen to
    agree.
    """

    running: bool
    cameras: tuple[CameraState, ...]
    config_dir: str
    """Resolved config root, reported so the cascade is never a mystery."""
    state_dir: str
    other_daemon_processes: tuple[int, ...] = ()
    """Pids of other ``python -m aprilcam.daemon``-shaped processes found
    in the local process table that are **not** the current lock holder
    (ticket 031-002, SUC-002) -- an operator-visible symptom of the
    "alive but not serving" orphan the daemon's own flock mechanism
    cannot see (a process that released the lock during a slow shutdown
    without fully exiting, or one that never completed ``start()``'s
    lock acquisition at all). Best-effort **name-matching, not a proof
    of identity** -- report only, per this sprint's Design Rationale
    ("report orphaned daemon processes, never auto-kill them"); nothing
    in this codebase ever signals a pid found only by this scan.

    Always ``()`` here -- the default, and what every RPC-sourced
    ``DaemonStatus`` (``daemon.service.DaemonService.status()``,
    ``client.models.daemon_status_from_proto``) still constructs, since
    a process-table scan has no meaning against a remote host. The one
    call site that ever populates this with a real scan result is
    ``client.lifecycle.DaemonLifecycle.status()`` -- the *local* process
    view -- which is exactly why ``daemon status -d HOST`` and ``daemon
    list`` (both RPC-only) always report this field empty, structurally,
    with no special-casing needed at either call site.
    """
