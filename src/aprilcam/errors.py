"""The client-visible error taxonomy.

Every distinction a use case asserts about failure is a **type here,
not a message string**. The scenarios are explicit that failing must be
distinguishable from finding nothing (UC-13.S-5), that an unknown name
must list the alternatives (UC-27.S-3, UC-28.S-5), and that a rejected
push must name what was wrong (UC-03.S-3, UC-23.S-4). An adapter can
only honour those contracts if the client library raises something it
can dispatch on — so the interfaces raise these, and no adapter parses
message text.

Empty results are **not** errors and never raise: a field with no
daemons (UC-13), no tags (UC-10), or no annotations (UC-28.S-4) returns
empty. These types exist for the cases where the operation itself could
not be carried out or the request itself was wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class AprilCamError(Exception):
    """Base for every error the client library raises deliberately."""


class DiscoveryFailed(AprilCamError):
    """Discovery itself could not run — e.g. multicast blocked.

    Deliberately distinct from discovery finding nothing, which is an
    empty result (UC-13.S-5). Collapsing the two sends someone hunting
    for a dead daemon instead of a network problem.
    """


class ConnectionFailed(AprilCamError):
    """A named daemon did not answer.

    Carries the host so the error can say *which* daemon — during
    fan-out, one unreachable daemon is reported alongside the results
    that did come back, not allowed to fail the whole command.
    """

    def __init__(self, host: str, reason: Optional[str] = None) -> None:
        self.host = host
        self.reason = reason
        super().__init__(f"daemon at {host!r} did not answer"
                         + (f": {reason}" if reason else ""))


class DaemonVersionMismatch(AprilCamError):
    """A daemon answered, but does not speak this client's wire protocol
    (UC-13, SUC-001, ticket 031-001).

    Distinct from :class:`ConnectionFailed`: something *is* listening at
    *target* -- it just is not a compatible v2 daemon. Observed
    concretely when a v1 daemon (from a sibling checkout) shares this
    project's conventional control-plane socket path with a v2 daemon --
    a v2 client's first RPC against it (``GetStatus``) comes back
    ``UNIMPLEMENTED`` with no ``aprilcam-error-type`` trailing metadata,
    since v1 never implemented ``GetStatus`` -- or any RPC in
    ``aprilcam_v2.proto`` -- at all. Raised instead of the generic
    ``ConnectionFailed`` so the operator sees "wrong protocol," not the
    indistinguishable-from-nothing-listening message that used to reach
    them.
    """

    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(
            f"daemon at {target!r} speaks an incompatible protocol "
            "-- this looks like a v1 aprilcam daemon (or another "
            "process) sharing the control-plane socket, not a "
            "compatible v2 daemon"
        )


class UnknownTarget(AprilCamError):
    """A name the daemon does not know, with the names it does.

    The contract every unknown-name scenario asserts: the error lists
    the valid alternatives rather than leaving the caller to guess
    (UC-27.S-3, UC-28.S-5, UC-14's absent-camera distinction).
    """

    def __init__(self, requested: str, available: tuple[str, ...]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"unknown {self.kind} {requested!r}; available: "
            + (", ".join(available) or "(none)")
        )

    kind = "target"


class UnknownCamera(UnknownTarget):
    kind = "camera"


class UnknownPlayfield(UnknownTarget):
    kind = "playfield"


class UnknownGuide(UnknownTarget):
    kind = "guide"


class CameraNotPresent(AprilCamError):
    """A camera the registry knows -- addressed by slug or by its
    persistent number -- is not currently enumerated among live
    backends (UC-14, UC-25).

    Distinct from :class:`UnknownCamera`: the registry has a record for
    this camera, it simply is not present right now (unplugged, or a
    transient enumeration miss). A registry record outliving its
    hardware is the normal case (UC-25), not a rejection -- this type
    lets a caller distinguish "never registered" from "registered, but
    not here," which ``UnknownCamera`` alone could not (UC-14.S-4-ish
    reasoning, extended to number addressing by ticket 021-002).
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"camera {slug!r} is registered but not currently present")


class CameraInUse(AprilCamError):
    """``close_camera`` was refused: the camera has a live stream
    subscriber or a read in flight (sprint 047 ticket 007).

    Closing it would pull the device out from under a consumer, so the
    operator must opt in (``force``). Carries the camera ``slug`` for
    the wire (``aprilcam-slug`` trailing metadata).
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(
            f"camera {slug!r} is in use (a stream subscriber or a read is active); "
            "pass --force to close it anyway"
        )


class NotCalibrated(AprilCamError):
    """A deskewed or world-coordinate product was asked of an
    uncalibrated camera (UC-19).

    Raised instead of silently substituting a raw frame or pixel
    coordinates — a plausible-looking wrong answer is the failure mode
    this type exists to kill.
    """


class UnsupportedSetting(AprilCamError):
    """A config push named a key the hardware does not support or
    rejected (UC-03).

    The **whole push** fails with this, naming the offending key; the
    camera is left exactly as it was. Never a partial apply.

    **``reason`` (ticket 030-005).** The bare "unsupported or rejected
    setting" message is a poor diagnostic on its own -- it cannot tell
    a caller whether the key is simply not a setting this camera
    exposes, the value it was given is out of the control's supported
    range, or the control is still gated by an auto-mode setting that
    was never switched to manual. ``camera.camera.Camera.apply_config``
    now determines which of those (when it can) and passes it here;
    *reason* is optional and additive -- every existing raise site and
    every existing ``UnsupportedSetting(key)`` call keeps working
    unchanged, and ``.key`` is unaffected either way.
    """

    def __init__(self, key: str, reason: Optional[str] = None) -> None:
        self.key = key
        self.reason = reason
        message = f"unsupported or rejected setting: {key!r}"
        if reason:
            message += f" -- {reason}"
        super().__init__(message)


class MalformedDocument(AprilCamError):
    """A configuration document was rejected whole (UC-02.S-2,
    UC-23.S-4).

    Includes the self-naming rule: a document without a ``name`` is
    malformed — the target is never inferred from the filename. Raised
    client-side by the loader where possible, so a bad file never even
    reaches the daemon.
    """


class InvalidTagId(AprilCamError):
    """A tag id outside its family's dictionary range (UC-21.S-3).

    Names the valid range; nothing is generated or written.
    """

    def __init__(self, family: str, tag_id: int, valid: range) -> None:
        self.family = family
        self.tag_id = tag_id
        self.valid = valid
        super().__init__(
            f"{family} id {tag_id} outside valid range "
            f"{valid.start}..{valid.stop - 1}"
        )


class UnwritableOutputPath(AprilCamError):
    """A ``tags generate`` output directory the process cannot write to
    (UC-21, SUC-003).

    Raised before any image is rendered or encoded -- checked once, up
    front, exactly like :class:`InvalidTagId`'s whole-batch id
    validation, so a batch is never partially written to disk.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"cannot write to output directory: {path}")


class AlreadyRunning(AprilCamError):
    """The singleton lock is held; names the holder (UC-13)."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        super().__init__(f"a daemon is already running: {holder}")


class NoPlayfieldLinked(AprilCamError):
    """A calibration was requested for a camera with no playfield link
    (UC-15.S-2).

    A **precondition failure**, distinct from a failed
    ``calibration.interfaces.CalibrationResult`` -- raised before any
    frame is captured or camera backend touched at all.
    ``calibration.calibration.Calibrator.calibrate`` checks the camera's
    playfield link *before* ever calling its injected frame source, so a
    camera with no link never causes a single call into the camera
    backend (the acceptance criterion this type exists to make provable,
    not just documented).
    """

    def __init__(self, camera: str) -> None:
        self.camera = camera
        super().__init__(
            f"camera {camera!r} has no playfield link; nothing to calibrate "
            "-- push a camera config naming a playfield first "
            "(aprilcam camera config <file>)"
        )


class NoFieldMask(AprilCamError):
    """Flat-field capture was requested for a camera with no derivable
    field-interior mask (sprint 037).

    A **precondition failure**, distinct from a failed
    ``calibration.interfaces.FlatFieldResult`` -- raised before any
    capture burst is taken, mirroring :class:`NoPlayfieldLinked`'s
    "raised before any frame is captured" discipline exactly.
    ``daemon.service.DaemonService.capture_flat_field`` (ticket 002)
    derives the mask two ways -- from an existing ``Calibration``, or
    by detecting the linked playfield's required ArUco corners directly
    -- and raises this only when *neither* path produces one: no stored
    calibration **and** the corners are not currently detectable (dark
    field, camera unplugged, wrong exposure).
    """

    def __init__(self, camera: str) -> None:
        self.camera = camera
        super().__init__(
            f"camera {camera!r} has no calibration and no detectable playfield "
            "corners; cannot derive a field-interior mask for flat-field capture "
            "-- calibrate the camera first, or ensure its playfield's ArUco "
            "corners are visible"
        )


class MissingOptionalDependency(AprilCamError):
    """A subcommand needs a package this environment does not actually
    have -- e.g. ``aprilcam mcp`` needs the ``mcp`` SDK's
    ``mcp.server.fastmcp``.

    Distinct from every other error in this module: it is not a
    daemon-reported precondition or protocol failure, it is a local
    import failure -- the requested feature's own code could not even
    be loaded. Raised so the affected subcommand can report a clear,
    actionable message through the same ``report_error`` path every
    other typed error already uses, instead of letting a raw
    ``ModuleNotFoundError`` traceback reach the operator.

    Critically, this must only ever be raised from *inside* the one
    subcommand that needs the package, at the moment that subcommand
    actually runs -- never from module-level code that ``cli/__init__``
    imports unconditionally at CLI startup (``cli/mcp.py``'s module
    docstring has the fuller story for that command). A dependency
    that is merely optional for the CLI as a whole must never make the
    whole CLI unusable just because one subcommand cannot import it.
    """

    def __init__(self, feature: str, package: str, *, install_hint: str) -> None:
        self.feature = feature
        self.package = package
        self.install_hint = install_hint
        super().__init__(
            f"{feature} requires the {package!r} package, which is not "
            f"available in this environment -- {install_hint}"
        )


class AnnotationLimitExceeded(AprilCamError):
    """A ``put_annotation``/``replace_layer`` request would push a
    playfield's annotation store past a fixed bound (UC-28, SUC-010,
    ticket 031-010).

    Sprint 024 shipped the annotation store with **no** bound at all
    (``types.AnnotationSet``'s own "Open" note: "maximum annotations
    per playfield/layer and points per path are unbounded here") -- a
    misbehaving client could otherwise grow the store, and the
    per-frame embedded payload every deskewed ``GetFrame`` call
    carries, without limit. This closes that gap with two independent
    checks, distinguished by ``kind``:

    * ``"annotations"`` -- an addition (``put_annotation``) or a whole
      replacement (``replace_layer``) would leave a single
      ``(playfield, layer)`` holding more than the fixed per-layer
      count bound.
    * ``"points"`` -- a ``PathShape``/``Polygon`` shape's ``points``
      tuple exceeds the fixed per-shape point-count bound.
      ``Marker``/``Circle``/``Text`` are fixed-parametric (a handful of
      scalar fields, no caller-sized tuple) and never raise this.

    Both checks run, and this is raised, **before** either mutation
    method touches the store -- a rejected request leaves the store's
    previous contents completely unchanged, extending
    ``replace_layer``'s existing "no consumer ever observes a
    half-updated layer" atomicity guarantee (UC-28.S-2) to this new
    failure path too.
    """

    def __init__(self, kind: str, bound: int, actual: int) -> None:
        self.kind = kind
        self.bound = bound
        self.actual = actual
        super().__init__(
            f"{kind} limit exceeded: {actual} exceeds the bound of {bound}"
        )
