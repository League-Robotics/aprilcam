"""``DaemonService`` -- the ``Daemon`` base class's RPC-body
implementation for ``list_cameras``, ``probe_cameras``, ``get_frame``,
``status`` (ticket 020-004), plus ``get_tags``/``get_tag``/
``get_tag_history`` (ticket 021-004), plus ``set_playfield``/
``get_playfield``/``list_playfields``/``set_camera_config``/
``get_camera_config`` (ticket 022-001), plus ``calibrate``/
``get_calibration`` (ticket 022-003), plus ``register_tag``/
``unregister_tag``/``list_tag_parameters`` (ticket 025-004, UC-12 --
the mobile-tag registry; see ``_camera_geometry_for``'s docstring for
this ticket's one open, explicitly-flagged design decision), plus
``find_feature`` (ticket 025-002, UC-08) and ``get_guide`` (ticket
025-007, UC-27 -- the last method this sprint adds).

**Calibration persistence** (ticket 022-003, UC-01's "calibration is
file-backed, unlike pushed config"). All vision/homography work is
delegated to ``calibration.calibration.Calibrator`` -- this class's job
is exactly the three things that module boundary reserves for the
daemon: resolving the camera and handing it frames (the ``capture_frames``
callable ``Calibrator`` is constructed with), writing a successful
result to ``<state_dir>/cameras/<camera>/calibration.json`` (atomic
tmp+fsync+rename, mirroring ``camera/registry.py``'s ``_save``), and
reloading every camera's ``calibration.json`` at construction time --
"loaded at daemon startup", the same file-backed rule the module
docstring above already describes for ``config.json``/registry.json.

**Mount-registry persistence** (ticket 036-007, UC-12). The mobile-tag
registry (``register_tag``/``unregister_tag``/``list_tag_parameters``
above) is file-backed the same way: a successful ``register_tag``/
``unregister_tag`` writes the whole registry to ``<state_dir>/mounts/
registry.json`` (:meth:`DaemonService._persist_mounts`, the identical
tmp+fsync+``os.replace`` pattern ``_persist_calibration`` uses), and
:meth:`DaemonService._load_mounts` reloads it at construction time,
tolerating a missing or corrupt file exactly like ``_load_calibrations``
does. A mount registration describes physical reality -- where a tag is
bolted to a robot -- so, like a calibration, it no longer needs
re-registering every session.

Delegates everything camera-shaped to ``camera/registry.py`` (ticket
002) and ``Camera``/``CameraDiscovery`` (ticket 003) -- no identity
derivation, no registry persistence, no probe/backend logic is
duplicated here (sprint architecture's module table: this module's
boundary is "RPC method bodies... Outside: camera hardware access").
Everything vision-shaped -- the ring buffer, detection bursts, corner
handling -- is delegated the same way to ``daemon/detection.py``
(ticket 021-004): no vision logic lives inline here, matching sprint
021's module boundary table exactly as sprint 020's did for cameras.

**Only a subset of ``Daemon``'s methods are implemented.** ``daemon.
interfaces.Daemon`` is a much larger base class -- as of sprint 027
``stream_tags`` (ticket 003) and ``stream_images`` (ticket 004) both
have real, in-process implementations (see their own docstrings below
for the ``daemon/streaming.py`` producers each wires up); the RPC/socket
transport that exposes them to a remote client is ticket 005's job, not
this class's. Whatever of ``Daemon``'s surface remains unbuilt at any
given point is simply absent from :class:`DaemonService`, inherited
as-is from ``Daemon``. Calling one of *those* here raises
``NotImplementedError`` naming the missing method (``daemon.interfaces``'s
module docstring explains why this is a plain base class and not
``abc.ABC``: un-instantiable until all 27 methods existed would force
hollow stubs onto every partial implementation, which is worse than an
honest partial one). ``isinstance(service, Daemon)`` is ``True`` --
:class:`DaemonService` really is a ``Daemon``, just an incomplete one;
the per-method ``NotImplementedError`` is what keeps that honest rather
than pretending full conformance.

**Long-lived ``Camera`` instances, held open.** ``get_frame`` opens a
camera *at most once*: the first successful open is cached in
``self._cameras`` and reused for every subsequent call, which is what
lets ``Camera``'s self-heal (5 consecutive failures -> reopen, capped
at 10) actually accumulate state across calls rather than resetting on
every request. This cache **is** the daemon's live-subscriber table:
:meth:`active_slugs` exposes its keys, and ``daemon/__main__.py`` wires
the same ``self._cameras`` dict object into ``CameraDiscovery`` as the
``active_camera_state`` callback (``camera/discovery.py``'s
constructor; ticket 003 left this defaulting to "nothing is active"
and named this exact wiring as this ticket's job) so
``probe_cameras()``'s non-disturbance guarantee (UC-25.S-7) holds for
real work, not just the mechanism-level unit test ticket 003 could
write without a service to wire into. **Sprint 039 ticket 004**
changed what that callback reports: instead of a bare slug membership
check (which made ``probe()`` hardcode ``usable=True`` for any active
slug), ``active_camera_state`` hands back the held-open ``Camera``'s
own ``.state`` -- the identical ``CameraState`` :meth:`list_cameras`
already reads for a held camera -- so a stalled, self-heal-exhausted
camera probes ``usable=False`` exactly as ``list_cameras`` reports it,
instead of the two methods disagreeing.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2 as cv
import numpy as np

from aprilcam import guides
from aprilcam.calibration import geometry
from aprilcam.calibration.calibration import Calibration, Calibrator
from aprilcam.calibration.deskew import Deskewer
from aprilcam.calibration.flatfield import FlatFieldCapturer
from aprilcam.calibration.interfaces import CalibrationResult, FlatFieldInfo, FlatFieldResult
from aprilcam.calibration.lens import LensStore
from aprilcam.camera.backends import (
    CaptureBackend,
    ControlBackend,
    default_capture_backend_factory,
    resolve_capture_backend,
)
from aprilcam.camera.backends.libcamera import LibcameraDevice
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.backends.uvc_util import UvcUtilBackend
from aprilcam.camera.backends.worker_backend import WorkerBackend
from aprilcam.camera.camera import FLATFIELD_FILENAME, Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.camera.registry import CameraRecord, CameraRegistry
from aprilcam.config.interfaces import CameraConfig, Directories, Feature
from aprilcam.config.interfaces import ConfigLoader as ConfigLoaderIface
from aprilcam.config.interfaces import PlayfieldDefinition as PlayfieldDefinitionIface
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.daemon.broadcast import Broadcaster
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.daemon.interfaces import Daemon, DaemonStatus, ImageStream, MountRegistration, TagStream
from aprilcam.intrinsics.interfaces import IntrinsicsFinishResult, IntrinsicsStatus
from aprilcam.intrinsics.manager import IntrinsicsManager
from aprilcam.daemon.streaming import (
    BroadcasterImageStream,
    BroadcasterTagStream,
    ImageStreamProducer,
    TagStreamProducer,
)
from aprilcam.errors import (
    AnnotationLimitExceeded,
    CameraNotPresent,
    NoFieldMask,
    NotCalibrated,
    UnknownCamera,
    UnknownGuide,
    UnknownPlayfield,
)
from aprilcam.playfield.playfield import Playfield
from aprilcam.playfield.query import FeatureQuery
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    AnnotationShape,
    CameraState,
    ImageFrame,
    MountParameters,
    PathShape,
    Polygon,
    ProbeReport,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    WorldPoint,
)
from aprilcam.vision.detector import TagDetector

__all__ = ["DaemonService"]

#: Name every camera's calibration file gets under
#: ``<state_dir>/cameras/<camera>/`` -- design.md §7's storage layout.
CALIBRATION_FILENAME = "calibration.json"

#: How often the control watchdog (:meth:`DaemonService.start_control_watchdog`)
#: reads every held camera's UVC controls back and re-asserts the applied
#: config when they drifted (2026-09-03 out-of-process fix; see
#: ``Camera.reassert_config``). Each readback is one ``get_property`` per
#: applied key -- a ``uvc-util`` subprocess each on macOS -- so this is
#: deliberately seconds, not frames. **Sprint 038**: this same thread/interval
#: also drives :meth:`DaemonService.check_stalled_cameras` and
#: :meth:`DaemonService._retry_exhausted_cameras` -- see those methods'
#: docstrings and ``camera-DESIGN.md``'s rejected-alternative on a second
#: watchdog thread.
CONTROL_READBACK_INTERVAL_S = 15.0

#: How often (seconds) the watchdog thread gives an *exhausted*
#: worker-backed camera (``Camera.state.usable`` stuck ``False`` because its
#: :data:`~aprilcam.camera.camera.MAX_REOPENS` budget is spent -- see
#: :meth:`Camera.retry_if_exhausted`) one fresh close/open retry, instead of
#: requiring a full daemon restart to try again (sprint 038 ticket 006 --
#: ``camera-DESIGN.md``'s Open Questions explicitly left the exact interval
#: to this ticket). Deliberately independent of, and much larger than,
#: :data:`CONTROL_READBACK_INTERVAL_S`: the watchdog thread still ticks
#: every :data:`CONTROL_READBACK_INTERVAL_S` seconds (no second thread --
#: same rejected-alternative as :meth:`check_stalled_cameras`), but
#: :meth:`_retry_exhausted_cameras` only actually retries a given camera
#: once this much wall-clock time has passed since its last attempt
#: (:attr:`DaemonService._exhausted_retry_at`). One minute is frequent
#: enough that a camera fixed by a replug or an unrelated worker kill comes
#: back within about a minute of being fixed, without hammering a
#: genuinely still-broken camera with a close/open cycle every 15s forever
#: (a real close()+open() cycle against actual hardware is not free, and a
#: camera that is still unplugged will simply fail again every time).
STALLED_CAMERA_RETRY_INTERVAL_S = 60.0

#: Name of the one shared mount-registry file under ``<state_dir>/mounts/``
#: (ticket 036-007) -- ``camera/registry.py``'s ``<state_dir>/cameras/
#: registry.json`` sibling layout, one flat file for the whole registry
#: rather than one file per tag, matching ``register_tag``'s existing
#: upsert-by-tag, read/write-as-a-unit semantics.
MOUNTS_FILENAME = "registry.json"

#: How many frames :meth:`DaemonService._capture_calibration_frames`
#: reads for one :meth:`DaemonService.calibrate` call -- see
#: ``calibration.calibration.DEFAULT_CALIBRATION_FRAMES``'s docstring
#: for provenance (mined from v1's real-camera-tuned default). Frames
#: the camera fails to deliver (``Camera.read()`` returning ``None``)
#: are simply not counted towards this budget's worth of usable frames.
CALIBRATION_FRAME_COUNT = 30

#: Name every camera's flat-field metadata sidecar gets under
#: ``<state_dir>/cameras/<camera>/`` (sprint 037), next to
#: ``camera.camera.FLATFIELD_FILENAME``'s ``flatfield.npy`` -- the same
#: two-file shape ``calibration.json`` already established.
FLATFIELD_INFO_FILENAME = "flatfield.json"

#: How many frames :meth:`DaemonService._capture_flatfield_frames` reads
#: for one :meth:`DaemonService.capture_flat_field` call, absent an
#: explicit ``frames=`` override -- ``docs/knowledge/flat-field-gain-map.md``'s
#: manual recipe used "4-8 frames per camera"; 6 sits in the middle of
#: that confirmed-working range.
FLATFIELD_FRAME_COUNT = 6

logger = logging.getLogger(__name__)

#: What ``get_frame``'s ``encoding`` parameter accepts this sprint --
#: matches ``daemon.interfaces.Daemon.get_frame``'s docstring exactly
#: ("jpeg" default, "png" lossless).
VALID_ENCODINGS = ("jpeg", "png")

_CV_EXTENSION = {"jpeg": ".jpg", "png": ".png"}


def _default_backend_factory(index: int) -> CaptureBackend:
    """``_camera_for``'s default non-libcamera ``CaptureBackend``
    construction (sprint 038 ticket 005).

    Delegates to ``camera.backends.default_capture_backend_factory`` --
    the one place this selection (``WorkerBackend`` by default on
    macOS, ``OpenCVBackend`` elsewhere, either forced by
    ``APRILCAM_CAPTURE_BACKEND``) is decided, so this function and
    ``camera/discovery.py``'s identically-named, identically-delegating
    ``_default_backend_factory`` cannot drift apart the way two
    independently-hardcoded ``OpenCVBackend(index)`` bodies did before
    this ticket. See that function's own docstring for the full
    contract. Every existing test that constructs a ``DaemonService``
    passes its own ``backend_factory=`` (a fake) and never reaches this
    default at all.
    """
    return default_capture_backend_factory(index)


def _default_control_backend_factory(device_name: str) -> Optional[ControlBackend]:
    """The real per-control mechanism for a newly-opened camera's
    settings surface (ticket 023-001, sprint.md Decision 2).

    macOS only, unconditionally: real-hardware validation against camera
    3 (an OV9782) found ``cv2.VideoCapture.set()`` -- ``OpenCVBackend``'s
    settings path -- returns ``False`` for every one of
    ``auto_exposure``/``exposure``/``gain``/``auto_white_balance``/
    ``white_balance_temperature`` on this platform's AVFoundation capture
    session, so ``UvcUtilBackend`` (``camera/backends/uvc_util.py``) is
    unconditionally what a macOS ``Camera`` uses for those controls, not
    a fallback tried only if something else fails first. Deliberately
    **not** wrapped in a try/except that would swallow a missing
    ``uvc-util`` binary into ``None`` -- sprint.md's Migration Concerns
    are explicit that its absence "must surface as a clear, typed
    error... never a silent fallback to a path that... may not actually
    work", so :class:`~aprilcam.camera.backends.uvc_util.UvcUtilBackend`'s
    constructor is left to raise straight through when ``uvc-util`` is
    not on ``PATH``, rather than this factory quietly degrading to the
    already-proven-broken ``OpenCVBackend`` control surface.

    Returns ``None`` on every other platform, unchanged from before this
    ticket -- Linux/Raspberry Pi UVC control is out of this sprint's
    scope (sprint.md Scope); ``Camera`` falls back to *backend* itself
    for settings exactly as it always has (``camera/camera.py``'s
    module docstring).
    """
    if sys.platform != "darwin":
        return None
    return UvcUtilBackend(device_name)


#: Fixed bound on how many annotations one ``(playfield, layer)`` pair
#: may hold at once (ticket 031-010, UC-28/SUC-010, sprint.md Design
#: Rationale). Sprint 024 shipped the annotation store with no bound at
#: all (``types.AnnotationSet``'s own "Open" note); per sprint.md's
#: explicit decision, this is a **fixed constant, not a configurable
#: document field** -- even a fixed bound is new ground this sprint,
#: and a configurable one (per-playfield or global, settable via a
#: pushed document) is a larger design surface not needed yet.
#:
#: Sized against the installed real playfield
#: (``config/aprilcam/playfields/main-playfield.json``, 134.3 x 89.3 cm)
#: hosting a busy multi-robot session: each robot drawing a handful of
#: annotations (a position marker, a planned path, a status label, a
#: keep-out zone) puts a genuinely busy field on the order of 50-100
#: annotations in one layer. 1,000 leaves an order of magnitude of
#: headroom above that, while still bounding memory and the per-frame
#: embedded payload against a runaway or misbehaving client.
_MAX_ANNOTATIONS_PER_LAYER = 1000

#: Fixed bound on ``PathShape.points``/``Polygon.points`` length for any
#: one shape (ticket 031-010, UC-28/SUC-010) -- see
#: :data:`_MAX_ANNOTATIONS_PER_LAYER` above for why this is a fixed
#: constant, not a configurable document field.
#:
#: Sized against the same real playfield's ~447 cm perimeter
#: (2 * (134.3 + 89.3)): a route traced with a waypoint every 2 cm
#: around the whole perimeter is ~224 points; a dense multi-lap session
#: log (ten laps at that same 2 cm spacing) is ~2,240 points. 10,000
#: comfortably covers that with real headroom left above it.
_MAX_POINTS_PER_SHAPE = 10000


def _check_point_bound(shape: AnnotationShape) -> None:
    """Reject *shape* with :class:`~aprilcam.errors.AnnotationLimitExceeded`
    if it is a ``PathShape``/``Polygon`` whose ``points`` tuple exceeds
    :data:`_MAX_POINTS_PER_SHAPE` (ticket 031-010).
    ``Marker``/``Circle``/``Text`` are fixed-parametric -- a handful of
    scalar fields, no caller-sized tuple -- so only the two
    variable-length shapes in ``types.AnnotationShape`` can ever fail
    this check."""
    if isinstance(shape, (PathShape, Polygon)) and len(shape.points) > _MAX_POINTS_PER_SHAPE:
        raise AnnotationLimitExceeded("points", _MAX_POINTS_PER_SHAPE, len(shape.points))


# -- mount registry (de)serialization (ticket 036-007) -----------------------
#
# Neither ``TagId`` nor ``MountParameters`` (``types.py``) has a
# ``to_dict``/``from_dict`` of its own to reuse -- unlike ``Calibration``,
# which already had one before this ticket. These four module-level
# functions are ``mounts/registry.json``'s inline (de)serialization,
# deliberately mirroring ``Calibration.to_dict``/``from_dict``'s shape
# (raise on malformed input; omit an optional field entirely rather than
# writing JSON ``null``) rather than inventing a different convention.


def _mount_registry_key(tag: TagId) -> str:
    """``TagId`` -> its string key in ``mounts/registry.json``, e.g.
    ``"apriltag:7"`` -- the encoding the sprint's storage-location
    decision calls for."""
    return f"{tag.family.value}:{tag.number}"


def _mount_registry_key_to_tag(key: str) -> TagId:
    """The inverse of :func:`_mount_registry_key`. Raises ``ValueError``
    on anything that does not parse as ``"<family>:<number>"`` with a
    known family and an integer number -- :meth:`DaemonService._load_mounts`
    catches this and treats it as one malformed entry to skip, exactly
    like a malformed ``calibration.json`` field."""
    family_str, sep, number_str = key.partition(":")
    if not sep:
        raise ValueError(f"malformed mount registry key {key!r}: missing ':'")
    return TagId(family=TagFamily(family_str), number=int(number_str))


def _mount_params_to_dict(params: MountParameters) -> Dict[str, object]:
    """Serialize one ``MountParameters`` for ``mounts/registry.json`` --
    ``size_cm`` is omitted entirely when ``None``, never written as a
    JSON ``null`` placeholder, mirroring ``Calibration.to_dict``'s
    identical treatment of its own optional fields."""
    data: Dict[str, object] = {
        "mount_x": params.mount_x,
        "mount_y": params.mount_y,
        "mount_z": params.mount_z,
        "mount_yaw_rad": params.mount_yaw_rad,
    }
    if params.size_cm is not None:
        data["size_cm"] = params.size_cm
    return data


def _mount_params_from_dict(data: object) -> MountParameters:
    """The inverse of :func:`_mount_params_to_dict`. Raises
    ``TypeError``/``KeyError``/``ValueError`` on a malformed record --
    :meth:`DaemonService._load_mounts` treats that as one malformed
    entry to skip, never a reason to fail daemon construction (mirrors
    ``Calibration.from_dict``'s contract exactly)."""
    if not isinstance(data, dict):
        raise TypeError(f"mount registry entry is not a JSON object: {data!r}")
    return MountParameters(
        mount_x=float(data.get("mount_x", 0.0)),
        mount_y=float(data.get("mount_y", 0.0)),
        mount_z=float(data.get("mount_z", 0.0)),
        mount_yaw_rad=float(data.get("mount_yaw_rad", 0.0)),
        size_cm=float(data["size_cm"]) if data.get("size_cm") is not None else None,
    )


# -- flat-field metadata sidecar (de)serialization (sprint 037) --------------
#
# ``FlatFieldInfo`` has no ``to_dict``/``from_dict`` of its own by design
# (its own docstring: "persistence itself... is daemon/service.py's job",
# the same division ``Calibration.to_dict``/``from_dict`` establishes for
# ``calibration.json``). These two module-level functions are
# ``flatfield.json``'s inline (de)serialization, mirroring
# ``_mount_params_to_dict``/``_mount_params_from_dict``'s shape immediately
# above (omit an optional field entirely rather than writing JSON ``null``;
# raise on malformed input for the caller to treat as one corrupt record).


def _flatfield_info_to_dict(info: FlatFieldInfo) -> Dict[str, object]:
    """Serialize one :class:`FlatFieldInfo` for ``flatfield.json``."""
    data: Dict[str, object] = {"present": info.present, "stale": info.stale}
    if info.captured_at is not None:
        data["captured_at"] = info.captured_at
    if info.shape is not None:
        data["shape"] = list(info.shape)
    if info.gain_min is not None:
        data["gain_min"] = info.gain_min
    if info.gain_max is not None:
        data["gain_max"] = info.gain_max
    return data


def _flatfield_info_from_dict(data: object) -> FlatFieldInfo:
    """The inverse of :func:`_flatfield_info_to_dict`. Raises
    ``TypeError``/``KeyError``/``ValueError`` on a malformed record --
    :meth:`DaemonService._load_flatfields` treats that the same way
    :meth:`DaemonService._load_mounts` treats a malformed mount entry:
    logged and skipped (here, falling back to the bare "present, no
    metadata" record a missing sidecar itself produces), never a reason
    to fail daemon construction."""
    if not isinstance(data, dict):
        raise TypeError(f"flatfield info entry is not a JSON object: {data!r}")
    shape_raw = data.get("shape")
    return FlatFieldInfo(
        present=bool(data.get("present", True)),
        captured_at=(
            float(data["captured_at"]) if data.get("captured_at") is not None else None
        ),
        shape=tuple(int(x) for x in shape_raw) if shape_raw is not None else None,
        gain_min=float(data["gain_min"]) if data.get("gain_min") is not None else None,
        gain_max=float(data["gain_max"]) if data.get("gain_max") is not None else None,
        stale=bool(data.get("stale", False)),
    )


def _same_lens(calibration: Calibration, stored: Any) -> bool:
    """Whether *calibration* embeds exactly *stored*'s lens model."""
    if calibration.camera_matrix is None or calibration.dist_coeffs is None:
        return False
    return tuple(map(tuple, calibration.camera_matrix)) == tuple(
        map(tuple, stored.camera_matrix)
    ) and tuple(calibration.dist_coeffs) == tuple(stored.dist_coeffs)


class DaemonService(Daemon):
    """Concrete ``daemon.interfaces.Daemon`` (this sprint's slice only).

    Derives from ``Daemon`` (see that class's module docstring for the
    base-class-vs-``Protocol`` design rule): the methods this sprint
    does not implement (streams, tag parameters, guides) are inherited
    as-is, so calling one raises ``NotImplementedError`` naming the
    missing method rather than an ``AttributeError`` -- ``isinstance(
    service, Daemon)`` is also simply true now, mechanically, rather
    than a Protocol structural guess.

    Parameters
    ----------
    registry:
        The persistent identity -> slug/number mapping (ticket 002).
    discovery:
        Passive-watch + active-probe camera discovery (ticket 003).
        Its ``active_camera_state`` callback should be wired (at
        construction, ``daemon/__main__.py`` does this) to read this
        service's own ``cameras`` dict -- see ``camera/discovery.py``'s
        ``active_camera_state`` parameter -- so probe's non-disturbance
        guarantee holds *and* an active camera's reported ``usable``
        agrees with :meth:`list_cameras` (sprint 039 ticket 004).
    directories:
        Reported verbatim by :meth:`status` (``config_dir``/
        ``state_dir``) -- UC-01's "which roots were chosen is never a
        mystery," now answerable over the wire too.
    cameras:
        The held-open-camera cache, keyed by slug. Accepts an
        externally-owned dict (rather than always constructing its own)
        so ``daemon/__main__.py`` can hand the *same* dict object to
        both this service and ``CameraDiscovery``'s
        ``active_camera_state`` closure -- see the module docstring.
        Defaults to a fresh, empty dict when not supplied (tests that
        do not care about this wiring).
    identity_source / backend_factory:
        Resolve a registered camera's *current* OS-probe index on
        demand (only needed the first time a given slug is requested
        via :meth:`get_frame` -- see :meth:`_camera_for`). Overridable
        for headless tests exactly like ``CameraDiscovery``'s own
        constructor; default to real hardware enumeration.
    list_loopback_cameras:
        () -> ``[LibcameraDevice, ...]``. Consulted only for a
        libcamera-identified camera (``record.hardware_key`` prefixed
        ``"libcamera:"``) to resolve the real ``/dev/videoN`` device a
        :class:`~aprilcam.camera.backends.v4l2_loopback.V4l2LoopbackBackend`
        needs -- see :func:`~aprilcam.camera.backends.resolve_capture_backend`
        (ticket 031-006, SUC-006), which :meth:`_camera_for` delegates
        backend selection to. Defaults to the real, subprocess-free
        ``/sys`` scan; overridable for headless tests exactly like
        *backend_factory* above.
    control_backend_factory:
        Builds the optional :class:`~aprilcam.camera.backends.ControlBackend`
        a newly-constructed ``Camera`` uses for ``apply_config``'s
        settings surface (ticket 023-001 -- real UVC control validated
        live on camera 3; see :func:`_default_control_backend_factory`).
        Defaults to that real, macOS-only factory; headless tests
        override with ``lambda device_name: None`` so a ``FakeBackend``
        stands in for settings too, exactly like *backend_factory*
        above.
    detection:
        The per-camera ring buffer plus burst orchestration
        (``daemon/detection.py``, ticket 021-004) that
        :meth:`get_tags`/:meth:`get_tag`/:meth:`get_tag_history`/
        :meth:`get_frame` delegate to. Defaults to a fresh
        ``DetectionEngine()`` (real pipeline, real detector) wired with
        this service's own :meth:`_mount_lookup`/:meth:`_camera_geometry_for`
        (ticket 025-004, UC-12) when not supplied -- tests that need to
        pin exactly how many detection bursts ran, or that want a
        non-default mount/camera-geometry lookup of their own, construct
        their own ``DetectionEngine`` and pass it here instead.
    calibrator:
        The homography-solving engine (``calibration.calibration.
        Calibrator``, ticket 022-003) that :meth:`calibrate` delegates
        all vision/geometry work to. Defaults to a real ``Calibrator``
        wired against this service's own ``_camera_configs``/
        ``_playfields`` stores and :meth:`_capture_calibration_frames`
        -- tests that want a substitutable frame source or camera-config
        lookup construct their own ``Calibrator`` and pass it here
        instead, the same override pattern ``detection`` above uses.
    config_loader:
        The :class:`~aprilcam.config.interfaces.ConfigLoader` every
        newly-constructed ``Camera`` is given (ticket 023-004), for
        :meth:`Camera.reset`/:meth:`Camera.stored_config` to read a
        camera's on-disk ``config_dir/cameras/<slug>/config.json`` back.
        This service never calls it itself -- passing it through to
        ``Camera`` at construction is the one piece of "where configs
        come from" knowledge this service carries, targeted reads only,
        never the boot-time directory scan (that stays in
        ``daemon.__main__.build()``, sprint.md Architecture "Modules"
        point 1). Defaults to a real ``config.loader.ConfigLoader``;
        tests substitute their own when they need to.
    flatfield_capturer:
        The gain-map-building engine (``calibration.flatfield.
        FlatFieldCapturer``, sprint 037) that :meth:`capture_flat_field`
        delegates all pixel math to -- the identical
        orchestration-vs-math split :attr:`calibrator` above establishes
        for ``Calibrator``. Defaults to a real ``FlatFieldCapturer``;
        tests that want pinned tuning constants (e.g. a lower quality-gate
        threshold to force a deterministic refusal) construct their own
        and pass it here instead.
    flatfield_detector_factory:
        ``() -> vision.detector.TagDetector``, used to build the one
        detector :meth:`_mask_from_aruco` runs when a camera has no
        stored calibration to derive its flat-field field-interior mask
        from -- the same "() -> TagDetector" shape ``Calibrator``'s own
        ``detector_factory`` parameter uses. Defaults to a real
        ``TagDetector``; overridable for headless tests exactly like
        every other factory parameter above.
    """

    def __init__(
        self,
        *,
        registry: CameraRegistry,
        discovery: CameraDiscovery,
        directories: Directories,
        cameras: Optional[Dict[str, Camera]] = None,
        identity_source: Callable[[], Dict[int, CameraIdentity]] = resolve_all,
        backend_factory: Callable[[int], CaptureBackend] = _default_backend_factory,
        control_backend_factory: Callable[[str], Optional[ControlBackend]] = (
            _default_control_backend_factory
        ),
        list_loopback_cameras: Optional[Callable[[], List[LibcameraDevice]]] = None,
        detection: Optional[DetectionEngine] = None,
        calibrator: Optional[Calibrator] = None,
        config_loader: Optional[ConfigLoaderIface] = None,
        flatfield_capturer: Optional[FlatFieldCapturer] = None,
        flatfield_detector_factory: Callable[[], TagDetector] = TagDetector,
    ) -> None:
        self._registry = registry
        self._discovery = discovery
        self._directories = directories
        self._cameras: Dict[str, Camera] = cameras if cameras is not None else {}
        #: Control watchdog thread state (:meth:`start_control_watchdog`).
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop: Optional[threading.Event] = None
        #: slug -> ``time.time()`` of this watchdog's last
        #: :meth:`_retry_exhausted_cameras` attempt for that camera (sprint
        #: 038 ticket 006). Purely a scheduling gate for
        #: :data:`STALLED_CAMERA_RETRY_INTERVAL_S` -- **not** a duplicate of
        #: ``Camera``'s own ``_exhausted`` bookkeeping (that stays entirely
        #: inside ``Camera``/:meth:`Camera.retry_if_exhausted`; this service
        #: never reads or sets it directly). Only ever touched from the
        #: single watchdog thread, so it needs no lock of its own -- unlike
        #: :attr:`_cameras`, nothing else in this class reads or writes it.
        self._exhausted_retry_at: Dict[str, float] = {}
        self._identity_source = identity_source
        self._backend_factory = backend_factory
        self._control_backend_factory = control_backend_factory
        self._list_loopback_cameras = list_loopback_cameras
        self._config_loader: ConfigLoaderIface = (
            config_loader if config_loader is not None else JsonConfigLoader()
        )
        # ``calibration_lookup=self.get_calibration``/
        # ``playfield_lookup=self._playfield_for_calibration``/
        # ``mark_stale=self._mark_stale``: bound-method references,
        # resolved at call time, not at this line -- safe to pass before
        # ``self._calibrations``/``self._playfields``/``self._lock`` exist
        # below, since ``DetectionEngine`` never calls any of them during
        # construction, only from a later ``run_burst`` (ticket 022-004's
        # ``calibration_lookup``; ticket 023-007's ``playfield_lookup``/
        # ``mark_stale``, module docstring's "threaded through" section).
        # Callers that pass their own ``detection=`` (tests pinning burst
        # counts) own their engine's callables themselves.
        self._detection = (
            detection
            if detection is not None
            else DetectionEngine(
                calibration_lookup=self.get_calibration,
                playfield_lookup=self._playfield_for_calibration,
                mark_stale=self._mark_stale,
                mount_lookup=self._mount_lookup,
                camera_geometry_lookup=self._camera_geometry_for,
            )
        )
        self._playfields: Dict[str, PlayfieldDefinitionIface] = {}
        self._camera_configs: Dict[str, CameraConfig] = {}
        # playfield -> layer -> id -> Annotation (ticket 024-001, UC-28).
        # A fourth in-memory store alongside the three above -- runtime
        # state, never written to disk, wiped on restart like everything
        # else this class holds. ``_annotation_revisions`` is the
        # sibling monotonic-counter store, one entry per playfield that
        # has ever seen a mutation; both are guarded by ``self._lock``
        # exactly like ``_playfields``/``_camera_configs``/
        # ``_calibrations`` already are.
        self._annotations: Dict[str, Dict[str, Dict[str, Annotation]]] = {}
        self._annotation_revisions: Dict[str, int] = {}
        # TagId -> MountParameters (ticket 025-004, UC-12): a fifth
        # in-memory store, guarded by the same ``self._lock`` -- "one
        # more dict under the existing lock", exactly the pattern ticket
        # 024-001 used for ``_annotations`` above. **Disk-backed as of
        # ticket 036-007**: a mount registration describes physical
        # reality (where a tag is bolted to a robot), not a per-session
        # experiment, so it now survives a restart the same way
        # ``_calibrations`` already does -- see :meth:`_persist_mounts`/
        # :meth:`_load_mounts`, called from :meth:`register_tag`/
        # :meth:`unregister_tag` and from this constructor respectively.
        self._mount_params: Dict[TagId, MountParameters] = {}
        # slug -> its live TagStreamProducer (ticket 027-003, UC-11).
        # Populated lazily by :meth:`stream_tags` on that camera's first
        # call, reused for every subsequent call against the same slug
        # (sprint.md Open Question 3's recommended reuse default,
        # applied here at the in-process layer). Entries are never
        # removed on idle -- a producer with zero subscribers is simply
        # a stopped thread (``TagStreamProducer.running`` is ``False``);
        # keeping the dict entry around means :meth:`close` can still
        # find and (harmlessly, idempotently) stop it, and a later
        # ``stream_tags`` call for the same camera reuses the same
        # producer object rather than constructing a second one.
        self._tag_stream_producers: Dict[str, TagStreamProducer] = {}
        # (slug, deskewed) -> its live ImageStreamProducer (ticket 027-004,
        # UC-07). Keyed one dimension wider than ``_tag_stream_producers``
        # above -- raw and deskewed are independent producers/streams for
        # the same camera (sprint.md's per-(camera, product) idle gating) --
        # but otherwise populated/reused/torn-down identically: lazily on
        # :meth:`stream_images`'s first call for a given key, reused for
        # every subsequent call against the same key, never removed on
        # idle (a producer with zero subscribers is just a stopped thread;
        # :meth:`close` still finds and stops it, harmlessly, if it was
        # never restarted).
        self._image_stream_producers: Dict[Tuple[str, bool], ImageStreamProducer] = {}
        self._lock = threading.Lock()
        self._lens_store = LensStore(self._directories.state_dir)
        self._calibrator = calibrator if calibrator is not None else Calibrator(
            get_camera_config=self._camera_config_for_calibration,
            get_playfield=self._playfield_for_calibration,
            capture_frames=self._capture_calibration_frames,
            # Fixes a confirmed carry-forward-after-restart bug (sprint
            # 030 ticket 002): without this, an ordinary recalibration
            # right after a daemon restart would read Calibrator's own
            # empty in-process cache instead of this service's
            # disk-loaded self._calibrations, silently wiping any
            # previously-estimated lens model back to None. Bound to
            # self.get_calibration -- resolved at call time, safe to
            # pass before self._calibrations exists below, the same
            # pattern calibration_lookup=self.get_calibration already
            # uses a few lines up. See Calibrator.__init__'s own
            # get_prior_calibration docstring for the full story and the
            # test that specifically exercises the restart path.
            get_prior_calibration=self.get_calibration,
            # Sprint 044: the per-camera lens store is consulted before
            # the prior calibration's embedded lens. Read-only here --
            # nothing in calibrate()/set_playfield() ever writes it.
            get_lens_model=self._lens_store.get_lens_model,
            # Sprint 044 ticket 010: why the store was ignored (resolution
            # mismatch), reported on the CalibrationResult.
            get_lens_warning=lambda slug, size: self._lens_store.get(slug, size)[1],
        )
        # Sprint 044: live ChArUco intrinsics sessions. Thin wiring only --
        # frames come from the held-open Camera.read(), and an installed
        # model goes into the same LensStore the Calibrator reads, so the
        # next calibrate() picks it up with no restart.
        self._intrinsics = IntrinsicsManager(
            frame_source_for=lambda slug: self._camera_for(slug).read,
            install=self._lens_store.put,
        )
        self._calibrations: Dict[str, Calibration] = {}
        self._load_calibrations()
        self._load_mounts()
        self._flatfield_capturer = (
            flatfield_capturer if flatfield_capturer is not None else FlatFieldCapturer()
        )
        self._flatfield_detector = flatfield_detector_factory()
        # slug -> FlatFieldInfo (sprint 037): the eighth in-memory store,
        # guarded by the same self._lock -- disk-backed the same way
        # self._calibrations is (see _persist_flatfield/_load_flatfields).
        # Only ever holds an entry for a camera that currently has a
        # flatfield.npy on disk; get_flatfield/clear_flat_field's "None
        # means never built, or cleared" contract falls out of that
        # invariant directly rather than needing a separate flag.
        self._flatfields: Dict[str, FlatFieldInfo] = {}
        self._load_flatfields()

    # -- beyond the interface: CameraDiscovery's active_camera_state wiring --

    def active_slugs(self) -> frozenset:
        """Slugs this service currently holds a ``Camera`` open for.

        A convenience read of ``self._cameras.keys()`` under the lock,
        independent of ``CameraDiscovery``'s own ``active_camera_state``
        callback (``daemon/__main__.py`` wires that directly against the
        same shared ``cameras`` dict object rather than through this
        method -- see the module docstring and sprint 039 ticket 004,
        which changed that callback from a bare membership check into a
        per-slug ``CameraState`` accessor so ``probe_cameras()`` reports
        real held-open health instead of assuming it (UC-25.S-7)).
        """
        with self._lock:
            return frozenset(self._cameras.keys())

    # -- daemon.interfaces.Daemon (this sprint's slice) --------------------

    def list_cameras(self) -> Tuple[CameraState, ...]:
        """See ``daemon.interfaces.Daemon.list_cameras``.

        Answered from what is already known -- the registry (every
        camera ever seen) plus ``discovery.known_present()`` (updated by
        passive watching and by the last ``probe()``) -- never a fresh
        probe. A slug this service already holds open reports its
        *live* ``Camera.state`` (which reflects self-heal exhaustion)
        rather than the coarser present/usable-from-enumeration guess
        used for everything else. ``playfield`` is overlaid from
        whatever ``CameraConfig`` :meth:`set_camera_config` last held
        for that slug (``None`` if never configured) -- SUC-001's "``
        camera list`` shows the camera linked" -- rather than stored on
        ``Camera`` itself, keeping the config-service link a pure
        ``DaemonService``-level concern (ticket 022-001).

        ``calibrated``/``calibration_stale`` are overlaid the same way,
        from :meth:`get_calibration` -- **not** read off ``Camera.state``
        (``Camera`` has no notion of calibration; that state lives here,
        in ``self._calibrations``, per the module boundary that keeps
        calibration a daemon-level concern rather than something
        ``camera/camera.py`` knows about). Bug fix, ticket 022-007: this
        method previously hardcoded ``calibrated=False`` unconditionally
        in both branches below (and, in the ``held`` branch, inherited a
        second independent hardcoded ``False`` from ``Camera.state``
        itself) -- a genuinely calibrated camera reported
        ``uncalibrated`` forever in ``camera list``/``daemon status``,
        even though ``get_frame(deskewed=True)``/``get_tags``/
        ``playfield view`` all read the real calibration correctly. Pure
        display/state-reporting bug: the calibration itself was never
        wrong, only what this method told a caller about it.

        ``flatfield_present``/``flatfield_stale`` (sprint 037) are
        overlaid the same way, from :meth:`get_flatfield` -- the same
        "daemon-level concern, not something ``Camera.state`` knows
        about" division ``calibrated``/``calibration_stale`` already
        establish, applied to the ``self._flatfields`` store instead.
        """
        present = self._discovery.known_present()
        states = []
        for record in self._registry.records():
            playfield = self._playfield_link(record.slug)
            calibrated, calibration_stale = self._calibration_flags(record.slug)
            lens_model, lens_pending = self._lens_flags(record.slug)
            cal_method, cal_confidence = self._calibration_method_flags(record.slug)
            flatfield_present, flatfield_stale = self._flatfield_flags(record.slug)
            held = self._cameras.get(record.slug)
            if held is not None:
                states.append(
                    replace(
                        held.state,
                        playfield=playfield,
                        calibrated=calibrated,
                        calibration_stale=calibration_stale,
                        flatfield_present=flatfield_present,
                        flatfield_stale=flatfield_stale,
                        lens_model=lens_model,
                        lens_pending=lens_pending,
                        calibration_method=cal_method,
                        calibration_confidence=cal_confidence,
                    )
                )
                continue
            is_present = record.slug in present
            states.append(
                CameraState(
                    name=record.slug,
                    present=is_present,
                    usable=is_present,
                    playfield=playfield,
                    calibrated=calibrated,
                    calibration_stale=calibration_stale,
                    flatfield_present=flatfield_present,
                    flatfield_stale=flatfield_stale,
                    lens_model=lens_model,
                    lens_pending=lens_pending,
                    calibration_method=cal_method,
                    calibration_confidence=cal_confidence,
                    number=record.number,
                )
            )
        return tuple(states)

    def _calibration_flags(self, slug: str) -> Tuple[bool, bool]:
        """``(calibrated, calibration_stale)`` for *slug*, from whatever
        :meth:`get_calibration` currently holds -- ``(False, False)``
        for a camera that has never been calibrated. The single overlay
        point :meth:`list_cameras` uses for both its branches, so a
        camera's reported calibration status can never drift between
        the "held open" and "not held open" code paths."""
        calibration = self.get_calibration(slug)
        if calibration is None:
            return False, False
        return True, calibration.stale

    def _calibration_method_flags(self, slug: str) -> Tuple[str, str]:
        """``(calibration_method, calibration_confidence)`` for *slug*
        (sprint 045 ticket 005), from the held calibration; ``("", "")``
        when never calibrated or the file predates the pose solve."""
        calibration = self.get_calibration(slug)
        if calibration is None:
            return "", ""
        return calibration.method or "", calibration.confidence or ""

    def _lens_flags(self, slug: str) -> Tuple[str, bool]:
        """``(lens_model, lens_pending)`` for *slug* (sprint 044 ticket
        010). ``lens_model`` is the lens-store model's source when one is
        stored (it is what the next :meth:`calibrate` applies), else the
        provenance of the lens embedded in the current calibration
        (``legacy`` for a pre-044 file), else ``"absent"``.
        ``lens_pending`` is true only when a calibration *and* a stored
        model both exist and the stored model differs from the embedded
        one -- a stored calibration keeps the lens it was solved with
        until ``calibrate`` runs again (sprint.md Decision 2)."""
        stored = self._lens_store.load(slug)
        calibration = self.get_calibration(slug)
        embedded = (
            calibration.lens_source
            if calibration is not None and calibration.camera_matrix is not None
            else None
        )
        if stored is not None:
            lens_model = stored.source
        elif embedded is not None:
            lens_model = embedded
        else:
            lens_model = "absent"
        pending = (
            stored is not None
            and calibration is not None
            and not _same_lens(calibration, stored)
        )
        return lens_model, pending

    def _flatfield_flags(self, slug: str) -> Tuple[bool, bool]:
        """``(flatfield_present, flatfield_stale)`` for *slug*, from
        whatever :meth:`get_flatfield` currently holds -- ``(False,
        False)`` for a camera with no gain map built yet, mirroring
        :meth:`_calibration_flags` exactly (sprint 037)."""
        info = self.get_flatfield(slug)
        if info is None:
            return False, False
        return info.present, info.stale

    def probe_cameras(self) -> ProbeReport:
        """See ``daemon.interfaces.Daemon.probe_cameras``. Pure
        delegation to ``CameraDiscovery.probe()`` -- no business logic
        here (sprint architecture's module table)."""
        return self._discovery.probe()

    def get_frame(
        self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg"
    ) -> ImageFrame:
        """See ``daemon.interfaces.Daemon.get_frame``.

        An unrecognised ``encoding`` is a plain ``ValueError`` (not one
        of ``errors.py``'s named types -- it is a caller-side argument
        mistake, not a domain failure the RPC boundary needs client
        code to dispatch on), checked before either branch below touches
        a camera or calibration lookup.

        ``deskewed=True`` delegates to :meth:`_get_deskewed_frame`
        (ticket 022-005): :class:`NotCalibrated` for an uncalibrated
        camera, a real warped frame for a calibrated one -- see that
        method's docstring. The raw (``deskewed=False``) branch is
        unchanged from sprint 021: ``tags`` embeds *camera*'s latest
        ring-buffer ``TagFrame`` when one exists
        (``daemon.detection.DetectionEngine.latest`` -- a pure read) and
        stays ``None`` when it does not -- asking for pixels never
        triggers a detection burst of its own (sprint.md Decision 4,
        ticket 021-004). ``calibration_stale`` (ticket 023-007) is
        overlaid from :meth:`get_calibration` on both branches -- ``ImageFrame``'s
        in-band carrier, so a client fetching pixels sees real staleness
        state without a second round trip.
        """
        if encoding not in VALID_ENCODINGS:
            raise ValueError(
                f"unsupported encoding {encoding!r}; expected one of {VALID_ENCODINGS}"
            )
        if deskewed:
            return self._get_deskewed_frame(camera, encoding=encoding)

        cam = self._camera_for(camera)
        frame = cam.read()
        if frame is None:
            raise RuntimeError(f"no frame available from camera {camera!r}")

        ok, buf = cv.imencode(_CV_EXTENSION[encoding], frame)
        if not ok:
            raise RuntimeError(f"failed to encode frame from {camera!r} as {encoding}")

        height, width = frame.shape[:2]
        calibration = self.get_calibration(cam.slug)
        return ImageFrame(
            data=buf.tobytes(),
            width=width,
            height=height,
            encoding=encoding,
            timestamp=time.time(),
            deskewed=False,
            tags=self._detection.latest(cam.slug),
            calibration_stale=calibration.stale if calibration is not None else False,
        )

    def _get_deskewed_frame(self, camera: str, *, encoding: str) -> ImageFrame:
        """``get_frame(deskewed=True)``'s real body (ticket 022-005;
        was an unconditional :class:`NotCalibrated` since sprint 020).

        **Calibration is checked, and this raises, before
        :meth:`_camera_for` is ever called** -- exactly the same
        "precondition fails before any hardware access" discipline
        :meth:`calibrate` already gets from ``Calibrator`` (module
        docstring). A deskewed request on an uncalibrated camera must
        never substitute a raw frame (``daemon.interfaces.Daemon.get_frame``'s
        base-class docstring) -- checking first, not merely returning
        early, is what makes "no raw frame is substituted" true by
        construction rather than by an easily-broken ordering.

        *camera* is resolved to its canonical slug via
        :meth:`_resolve_slug_if_known` up front (SUC-005 number-or-slug
        addressing -- the same hotfix :meth:`calibrate`/
        :meth:`get_calibration` already apply) *before* the calibration
        lookup, so a numbered camera's calibration is found the same way
        a slug-addressed one is.

        The playfield dimensions the warp needs come from
        ``calibration.playfield`` (the definition this *homography* was
        actually solved against) via :meth:`_playfield_for_calibration`,
        not from the camera's *current* live playfield link -- those can
        diverge (a camera re-linked to a different playfield after
        calibrating), and the homography's own world coordinates are
        only meaningful against the definition it was solved from.

        **Embeds annotations** (ticket 024-001, UC-28, Architecture
        resolution 1): :meth:`get_annotations` for the resolved
        ``calibration.playfield`` is always set here, never ``None`` --
        even an empty set (revision 0) -- since a deskewed frame off a
        known, calibrated playfield has only "nothing drawn" to
        represent, not an on/off state the way ``tags`` has. Safe to
        call unconditionally: by this point ``definition is not None``
        already proved the playfield is currently held, so this can
        never raise ``UnknownPlayfield``. The raw branch of
        :meth:`get_frame` is untouched -- it never sets ``annotations``,
        by contract (UC-16, "raw means raw").
        """
        slug = self._resolve_slug_if_known(camera)
        calibration = self.get_calibration(slug)
        if calibration is None:
            raise NotCalibrated(f"{camera!r} is not calibrated")

        definition = self._playfield_for_calibration(calibration.playfield)
        if definition is None:
            raise NotCalibrated(
                f"{camera!r} was calibrated against playfield "
                f"{calibration.playfield!r}, which this daemon no longer holds"
            )

        cam = self._camera_for(camera)
        frame = cam.read()
        if frame is None:
            raise RuntimeError(f"no frame available from camera {camera!r}")

        deskewer = Deskewer(
            calibration=calibration,
            width_cm=definition.width_cm,
            height_cm=definition.height_cm,
        )
        warped = deskewer.deskew(frame)

        ok, buf = cv.imencode(_CV_EXTENSION[encoding], warped)
        if not ok:
            raise RuntimeError(f"failed to encode deskewed frame from {camera!r} as {encoding}")

        height, width = warped.shape[:2]
        return ImageFrame(
            data=buf.tobytes(),
            width=width,
            height=height,
            encoding=encoding,
            timestamp=time.time(),
            deskewed=True,
            px_per_cm=deskewer.px_per_cm,
            world_extent=deskewer.world_extent,
            tags=self._detection.latest(cam.slug),
            annotations=self.get_annotations(calibration.playfield),
            calibration_stale=calibration.stale,
        )

    def get_tags(self, camera: str) -> TagFrame:
        """See ``daemon.interfaces.Daemon.get_tags``.

        Resolves *camera* via :meth:`_camera_for` (SUC-005's
        number-or-slug addressing, and ``UnknownCamera``/
        ``CameraNotPresent`` both apply, inherited for free -- sprint.md
        Decision 5) and delegates the actual burst entirely to
        ``daemon.detection.DetectionEngine.run_burst`` -- no vision logic
        lives here (module docstring). Exactly one detection burst runs
        per call; nothing here caches or shares a result across calls
        (SUC-002's "twice with nothing in view = exactly two bursts"
        pin).
        """
        cam = self._camera_for(camera)
        return self._detection.run_burst(cam.slug, cam)

    def get_tag(self, camera: str, tag: TagId) -> Optional[TagRecord]:
        """See ``daemon.interfaces.Daemon.get_tag``.

        Runs the same single burst :meth:`get_tags` would (via
        :meth:`get_tags` itself, so the "exactly one burst per call"
        contract is inherited rather than re-implemented) and filters it
        for *tag*. Returns ``None`` -- never a zero-valued record -- when
        *tag* is absent from this burst's results (UC-09.S-4).
        """
        tag_frame = self.get_tags(camera)
        for record in tag_frame.tags:
            if record.tag == tag:
                return record
        return None

    def get_tag_history(
        self, camera: str, tag: Optional[TagId] = None, frames: Optional[int] = None
    ) -> Tuple[TagFrame, ...]:
        """See ``daemon.interfaces.Daemon.get_tag_history``.

        A pure buffer read: *camera* is still resolved via
        :meth:`_camera_for` (SUC-005 addressing and
        ``UnknownCamera``/``CameraNotPresent`` apply here too -- sprint.md
        Decision 5), but no detection burst runs
        (``DetectionEngine.history`` never calls ``run_burst``) -- the
        buffer only grows from prior :meth:`get_tags`/:meth:`get_tag`
        calls (sprint.md Decision 1).

        A buffer with fewer entries than *frames* requested returns
        everything available, not an error. Filtering by *tag* preserves
        every frame's real ``timestamp``/``frame_index`` even where that
        frame's ``.tags`` is empty for the requested tag -- gaps are
        explicit, never dropped (UC-26.S-4).
        """
        cam = self._camera_for(camera)
        buffered = self._detection.history(cam.slug)

        if frames is not None:
            buffered = buffered[-frames:] if frames > 0 else ()

        if tag is not None:
            buffered = tuple(
                replace(frame, tags=tuple(r for r in frame.tags if r.tag == tag))
                for frame in buffered
            )

        return buffered

    # -- streams: tags (ticket 027-003, UC-11/SUC-001) ----------------------

    def stream_tags(self, camera: str) -> TagStream:
        """See ``daemon.interfaces.Daemon.stream_tags``.

        **In-process only, this ticket.** No socket or gRPC endpoint is
        allocated here -- this method hands back a ``daemon.interfaces.
        TagStream`` any in-process caller (a test, or a future
        ``daemon/stream_transport.py`` connection handler, ticket 005)
        can iterate directly. ``daemon/rpc.py``'s ``GetTagStream``
        RPC (endpoint allocation) and the raw-socket bridge that calls
        this method per accepted connection are ticket 005's job, not
        this one's -- exactly the production/transport split
        ``sprint.md`` Design Rationale 1 draws.

        Resolves *camera* via :meth:`_camera_for`, exactly like
        :meth:`get_tags` -- SUC-005 number-or-slug addressing, and
        ``UnknownCamera``/``CameraNotPresent`` both apply, inherited for
        free. Lazily builds (and this service tracks, keyed by slug) one
        ``daemon.streaming.TagStreamProducer`` per camera the first time
        it is asked for, reusing it on every subsequent call for the
        same camera rather than constructing a second, independent one
        (sprint.md Open Question 3's recommended reuse default, applied
        here at the in-process layer ticket 005's endpoint allocation
        will sit on top of). The producer's own idle-gating (ticket 003)
        starts/stops its background thread on *this* camera's
        ``Broadcaster``'s first-subscriber/last-unsubscriber transition
        -- this method does no lifecycle management of its own beyond
        constructing the producer once and subscribing to it.
        """
        cam = self._camera_for(camera)
        producer = self._tag_stream_producer_for(cam.slug, cam)
        subscriber_queue = producer.broadcaster.subscribe()
        return BroadcasterTagStream(producer.broadcaster, subscriber_queue)

    def _tag_stream_producer_for(self, slug: str, cam: Camera) -> TagStreamProducer:
        """Lazily builds (and this service tracks) one
        ``TagStreamProducer`` per camera slug, reused across every
        subsequent caller for that slug -- factored out of
        :meth:`stream_tags` so :meth:`_build_deskewed_image_producer`'s
        internal keep-alive subscription (see that method's docstring)
        shares the *identical* detection loop a real ``stream_tags``
        caller for the same camera would get, rather than each starting
        an independent one.
        """
        with self._lock:
            producer = self._tag_stream_producers.get(slug)
            if producer is None:
                producer = TagStreamProducer(slug, cam, self._detection)
                self._tag_stream_producers[slug] = producer
            return producer

    # -- streams: images (ticket 027-004, UC-07/SUC-002) ---------------------

    def stream_images(self, camera: str, *, deskewed: bool = False) -> ImageStream:
        """See ``daemon.interfaces.Daemon.stream_images``.

        **In-process only, this ticket** -- same split as
        :meth:`stream_tags`: ``daemon/rpc.py``'s ``GetImageStream`` RPC
        and the raw-socket bridge are ticket 005's job (see that
        ticket's Completion Notes reconciliation for the full
        three-way corroboration this reading rests on, restated here
        for this method).

        **Calibration is checked, and this raises, before any producer
        is constructed or reused** -- exactly the same two-step
        precondition :meth:`_get_deskewed_frame` already applies to the
        one-shot path (that method's own docstring), so a deskewed
        stream request on an uncalibrated camera -- or one calibrated
        against a playfield this daemon no longer holds -- fails
        synchronously in the caller's own thread with
        :class:`~aprilcam.errors.NotCalibrated`, never by silently
        substituting a raw stream (UC-06.S-3's rule, extended here to
        streams). A raw stream (``deskewed=False``) needs no
        calibration, exactly like ``get_frame(deskewed=False)``.

        Lazily builds (and this service tracks, keyed by ``(slug,
        deskewed)``) one ``daemon.streaming.ImageStreamProducer`` per
        pair the first time it is asked for, reusing it on every
        subsequent call for the same pair -- the same reuse discipline
        :meth:`stream_tags` already established, one key dimension
        wider (sprint.md Open Question 3's recommended reuse default).
        Raw and deskewed are independent producers: subscribing to one
        never starts, or is affected by, the other. The producer's own
        idle-gating (ticket 004, mirroring ticket 003) starts/stops its
        background thread on *this* (camera, deskewed) pair's
        ``Broadcaster``'s first-subscriber/last-unsubscriber transition
        -- this method does no lifecycle management of its own beyond
        constructing the producer once and subscribing to it, **except**
        for a deskewed request: :meth:`_build_deskewed_image_producer`
        wires that one producer's transitions to *also*
        acquire/release a keep-alive subscription to the camera's
        ``TagStreamProducer`` (see that method's docstring for why --
        confirmed real-hardware bug: without this, a deskewed image
        stream's embedded ``tags`` never updates unless some unrelated
        caller happens to be polling ``get_tags``/``stream_tags`` on the
        same camera at the same time). A raw stream does not need this
        -- nothing reads a raw frame's embedded ``tags`` today
        (``view.viewer``'s "raw means raw" contract) -- so it keeps the
        original, simpler wiring.
        """
        cam = self._camera_for(camera)
        if deskewed:
            calibration = self.get_calibration(cam.slug)
            if calibration is None:
                raise NotCalibrated(f"{camera!r} is not calibrated")
            definition = self._playfield_for_calibration(calibration.playfield)
            if definition is None:
                raise NotCalibrated(
                    f"{camera!r} was calibrated against playfield "
                    f"{calibration.playfield!r}, which this daemon no longer holds"
                )

        key = (cam.slug, deskewed)
        with self._lock:
            producer = self._image_stream_producers.get(key)
            if producer is None:
                producer = (
                    self._build_deskewed_image_producer(cam)
                    if deskewed
                    else ImageStreamProducer(cam.slug, cam, self._detection)
                )
                self._image_stream_producers[key] = producer
        subscriber_queue = producer.broadcaster.subscribe()
        return BroadcasterImageStream(producer.broadcaster, subscriber_queue)

    def _build_deskewed_image_producer(self, cam: Camera) -> ImageStreamProducer:
        """Builds the deskewed ``ImageStreamProducer`` for *cam*, wired
        so *cam*'s slug's ``TagStreamProducer`` -- the only thing that
        ever calls ``DetectionEngine.run_burst`` for a streamed camera
        (``daemon/streaming.py``'s module docstring; ``daemon/
        detection.py``'s "demand-driven, no background thread" note) --
        stays alive for as long as this deskewed image stream has at
        least one subscriber.

        **Why this exists: a confirmed bug, not speculative
        hardening.** ``ImageStreamProducer`` embeds tags via
        ``DetectionEngine.latest``, a pure buffer read (ticket 027-004's
        completion notes: "used here read-only ... to embed tags
        without ever calling run_burst itself") -- deliberately, so two
        independent producers publishing detection results never double
        the vision workload. That design assumed *something else* kept
        the buffer warm: originally the pre-streaming viewer's own
        ``get_tags`` call before every ``get_frame`` (sprint 022's
        ``_fetch_playfield``), and after ticket 027-007's migration, a
        ``stream_tags`` subscriber. But ticket 027-007 deliberately
        dropped the former -- ``view.viewer.run_playfield_view``
        subscribes to ``stream_images`` only, by its own acceptance
        criteria -- without anything supplying the latter. The result:
        a `playfield view` session run on its own drives zero
        ``run_burst`` calls, so every frame's embedded ``tags`` freezes
        at whatever ``DetectionEngine.latest`` last held (calibration-
        time state, or ``None``) for the entire life of the stream,
        even while a genuinely new physical tag is confirmed and
        visible to a concurrent ``get_tags``/``camera tags`` poll on
        the same camera. Confirmed against real hardware
        (``arducam-ov9281-usb-camera``) and reproduced headlessly in
        ``tests/aprilcam/daemon/test_service.py``'s
        ``test_stream_images_deskewed_alone_keeps_detection_running_and_tags_update``.

        The fix, applied here rather than inside ``ImageStreamProducer``
        itself (preserving that class's "never calls run_burst" design):
        this deskewed producer's own 0->1/1->0 subscriber transitions --
        the identical idle-gating hook ``ImageStreamProducer`` already
        uses to start/stop its own thread -- also acquire/release an
        internal, never-drained subscription to *cam*'s slug's
        ``TagStreamProducer`` via :meth:`_tag_stream_producer_for`, the
        same per-slug instance a real ``stream_tags`` caller for this
        camera would get. A concurrent real ``stream_tags`` subscriber
        and this keep-alive therefore share one detection loop rather
        than each starting an independent one -- unchanged "one
        computation, N subscribers" contract, just extended from warps
        (ticket 027-004's own headline claim) to detection bursts. The
        keep-alive queue is subscribed and never read:
        ``Broadcaster.publish``'s silent-drop-on-full backpressure
        (``daemon/broadcast.py``) makes an undrained queue harmless to
        every other subscriber, so this costs nothing beyond keeping
        the background thread itself alive.
        """
        slug = cam.slug
        keepalive: Dict[str, "queue.Queue[TagFrame]"] = {}

        def on_active() -> None:
            tag_producer = self._tag_stream_producer_for(slug, cam)
            keepalive["queue"] = tag_producer.broadcaster.subscribe()
            image_producer.start()

        def on_idle() -> None:
            image_producer.stop()
            tag_queue = keepalive.pop("queue", None)
            if tag_queue is not None:
                tag_producer = self._tag_stream_producers.get(slug)
                if tag_producer is not None:
                    tag_producer.broadcaster.unsubscribe(tag_queue)

        broadcaster: "Broadcaster[ImageFrame]" = Broadcaster(on_active=on_active, on_idle=on_idle)
        image_producer = ImageStreamProducer(
            slug,
            cam,
            self._detection,
            broadcaster,
            deskewed=True,
            calibration_lookup=self.get_calibration,
            playfield_lookup=self._playfield_for_calibration,
            annotations_lookup=self.get_annotations,
        )
        return image_producer

    def status(self) -> DaemonStatus:
        """See ``daemon.interfaces.Daemon.status``.

        ``running`` is always ``True`` here: this method only executes
        inside a live daemon process answering a live RPC, so by
        construction the daemon *is* running (UC-05's remote-readable
        service view -- ``DaemonLifecycle.status()`` is the separate,
        local-only "is anything running at all" check).
        """
        return DaemonStatus(
            running=True,
            cameras=self.list_cameras(),
            config_dir=str(self._directories.config_dir),
            state_dir=str(self._directories.state_dir),
        )

    def get_guide(self, name: Optional[str] = None) -> str:
        """See ``daemon.interfaces.Daemon.get_guide`` (ticket 025-007,
        UC-27).

        A thin wrapper over ``aprilcam.guides.read_guide`` -- the exact
        same shared leaf module ``client.guides.Guides.get`` reads
        (ticket 025-006), not a redundant reimplementation of the guide
        text or its name-to-filename mapping. That shared leaf is what
        makes this method and ``client.guides.Guides.get`` serve
        byte-identical text *by construction* (sprint 025's Design
        Rationale), not merely because both happen to agree today.

        An omitted or ``None`` *name* defaults to ``aprilcam.guides.
        DEFAULT_GUIDE`` (``"agent"``, UC-27's primary actor). An unknown
        name raises :class:`~aprilcam.errors.UnknownGuide`, naming the
        guides that actually exist (UC-27.S-3) -- the identical error
        shape ``client.guides.Guides.get`` raises for the same input,
        since both build it from the same ``guides.AVAILABLE_GUIDES``
        tuple.
        """
        chosen = name if name is not None else guides.DEFAULT_GUIDE
        text = guides.read_guide(chosen)
        if text is None:
            raise UnknownGuide(chosen, guides.AVAILABLE_GUIDES)
        return text

    # -- playfields / camera config (ticket 022-001; reset, ticket 023-004) -
    #
    # The minimal config-service slice sprint.md's Architecture Decision 1
    # scopes for this sprint: playfields in full (held zero-or-more,
    # keyed by name -- Decision 4), plus a camera-config slice carrying
    # only the playfield link. Both stores are plain in-memory dicts,
    # never written to disk (UC-01's ephemeral rule is structural here,
    # not a policy this class has to remember -- there is simply no
    # write path). ``reset_camera_config`` (below, ticket 023-004) is the
    # one deliberate exception to "never written to disk" -- it *reads*
    # disk, never writes it, so the ephemeral rule for RPC-pushed state
    # is undisturbed.

    def set_playfield(self, definition: PlayfieldDefinitionIface) -> None:
        """See ``daemon.interfaces.Daemon.set_playfield``.

        A single dict assignment, keyed by the document's own ``name``
        -- atomic by construction, so there is no partial-apply case to
        guard against here. Whole-document *validation* (malformed
        JSON, no name, duplicate ArUco ids, field-size inconsistency)
        already happened client-side in ``config.loader.ConfigLoader``
        before *definition* was ever built (UC-23.S-4) -- by the time an
        object reaches here it is a well-formed ``PlayfieldDefinition``.
        Replacing an existing definition of the same name does not
        (yet) mark any calibration against it stale -- no calibration
        subsystem exists until ticket 003.
        """
        with self._lock:
            self._playfields[definition.name] = definition

    def get_playfield(self, name: str) -> Playfield:
        """See ``daemon.interfaces.Daemon.get_playfield``. Raises
        :class:`~aprilcam.errors.UnknownPlayfield`, naming every
        playfield this daemon currently holds, for a name it does not
        recognise."""
        with self._lock:
            definition = self._playfields.get(name)
            camera_configs = tuple(self._camera_configs.values())
        if definition is None:
            raise UnknownPlayfield(name, tuple(sorted(self._playfields.keys())))
        return Playfield(name=definition.name, definition=definition, camera_configs=camera_configs)

    def list_playfields(self) -> Tuple[Playfield, ...]:
        """See ``daemon.interfaces.Daemon.list_playfields``. Every held
        definition (Decision 4: no single-slot limit), each wrapped
        with the *same* camera-config snapshot so every entry's
        ``linked_cameras()`` reflects one consistent moment."""
        with self._lock:
            definitions = tuple(self._playfields.values())
            camera_configs = tuple(self._camera_configs.values())
        return tuple(
            Playfield(name=d.name, definition=d, camera_configs=camera_configs) for d in definitions
        )

    def find_feature(self, playfield: str, query: str) -> Tuple[Feature, ...]:
        """See ``daemon.interfaces.Daemon.find_feature``. Ticket 025-002.

        Delegates to ticket 025-001's ``playfield.query.FeatureQuery``,
        built **fresh on every call** against the named playfield's
        *current* ``PlayfieldDefinition`` -- cheap (a tuple of already-
        materialized ``Feature``s, no vision, no camera), so there is
        nothing to cache and no staleness to reason about, the same
        "derived at serve time" discipline ``Playfield.linked_cameras()``
        already uses. Raises :class:`~aprilcam.errors.UnknownPlayfield`,
        naming every playfield this daemon currently holds, for a name
        it does not recognise -- the same precedent :meth:`get_playfield`
        established. Ambiguity returns every match (UC-08.S-2); no
        match returns an empty tuple (UC-08.S-3) -- neither is an error
        at this layer, matching ``FeatureQuery.find``'s own contract.
        """
        with self._lock:
            definition = self._playfields.get(playfield)
            available = tuple(sorted(self._playfields.keys()))
        if definition is None:
            raise UnknownPlayfield(playfield, available)
        return FeatureQuery(features=definition.features()).find(query)

    def get_camera_config(self, camera: str) -> CameraConfig:
        """See ``daemon.interfaces.Daemon.get_camera_config``.

        Answers from whatever :meth:`set_camera_config` last held for
        *camera* when one exists -- UC-03.S-2's "re-fetching the config
        returns the pushed values," exactly the document, never a live
        hardware readback merged in. This is deliberate, not an
        omission: the dump -> install -> restart -> dump round trip
        (UC-24.S-1) must reproduce exactly what was pushed or installed
        on disk, and the blank-template case just below must report
        ``None`` for every unset key even when the hardware itself has
        real factory-default values in force -- a live readback would
        get both of those wrong. Capturing what a camera's hardware is
        *actually* doing right now (as opposed to what document is on
        record for it) is a different, unbuilt capability -- ticket
        023-005 (this ticket) deliberately leaves it that way; see that
        ticket's notes.

        A camera with no stored document but **held open** in this
        process (:attr:`_cameras` -- populated by ``get_frame``/
        ``tags``/a config-push attempt/... via :meth:`_camera_for`)
        falls back to that camera's own introspected-plus-blank
        template (:meth:`~aprilcam.camera.camera.Camera.effective_config`,
        made real per-camera by ticket 023-002) -- UC-03.S-1's "freshly
        enumerated... never configured" scenario and UC-24.S-3's
        dump-as-template scenario, ticket 023-005's whole reason for
        depending on 023-002. A camera merely *registered* (known from
        a previous session or a probe, but never opened by this
        process) still reports :class:`~aprilcam.errors.UnknownCamera`
        here, matching UC-24.S-4 and every restart test's existing
        expectation -- config, pushed or blank-template, is exactly as
        ephemeral as everything else UC-01/UC-02 already establish;
        only calibration survives a restart.

        *camera* accepts a persistent number or a slug (SUC-005),
        resolved via :meth:`_resolve_slug_if_known` before either
        lookup -- both ``_camera_configs`` and ``_cameras`` are keyed by
        slug only, so an unresolved number would otherwise never match
        a real entry in either. The error still names whatever *camera*
        the caller actually typed, not the resolved slug.
        """
        slug = self._resolve_slug_if_known(camera)
        with self._lock:
            config = self._camera_configs.get(slug)
            held = self._cameras.get(slug)
            available = tuple(sorted(self._camera_configs.keys()))
        if config is not None:
            return config
        if held is not None:
            return held.effective_config()
        raise UnknownCamera(camera, available)

    def set_camera_config(self, config: CameraConfig) -> None:
        """See ``daemon.interfaces.Daemon.set_camera_config``.

        Stores *config* whole, keyed by its own ``name``, so
        :meth:`get_camera_config`/:meth:`list_cameras`/a linked
        playfield's ``linked_cameras()`` all see the update immediately.
        Accepts a config for a camera this daemon has never enumerated
        (a config outliving, or arriving before, its hardware is normal
        -- UC-25) and for a camera not currently held open -- there is
        nothing to reconfigure on a closed handle, only the link to
        record (ticket 023-001 AC1). A config stored here for a camera
        not yet open still takes effect the moment that camera is first
        opened, via :meth:`_camera_for`'s apply-on-open step (ticket
        023-003) -- the same mechanism that also applies a
        boot-time-loaded config (``daemon.__main__.build()``), so a
        pushed-while-absent config and a config installed on disk before
        the daemon ever started are handled identically once the camera
        actually appears.

        **Applies to hardware when the camera is held open** (ticket
        023-001, closing sprint 022's Decision 1 deferral -- UC-03's
        "applies... immediately, not at next open"). ``Camera.
        apply_config`` is called **before** the store, deliberately:
        its whole-push, hard-error contract
        (``camera.interfaces.Camera.apply_config``'s docstring) means an
        ``UnsupportedSetting`` propagates straight out of this method
        too, leaving *both* the hardware **and** the stored document
        exactly as they were (UC-03.S-3's "re-fetching the config
        returns the previous state") -- storing first and applying
        after would leave the stored document ahead of the hardware on
        exactly the failure path this rule exists to prevent.

        **Flags a resolution-changing push as flat-field-stale (sprint
        037).** A flat-field gain map is pixel-for-pixel tied to the
        resolution it was captured at (``Camera._apply_flatfield``
        already skips, with a warning, a map whose shape does not match
        the live frame) -- comparing *config*'s ``frame_width``/
        ``frame_height`` against whatever this daemon previously held for
        this camera (:meth:`_is_resolution_change`) and calling
        :meth:`_mark_flatfield_stale` on a real change is the same
        "advisory, still applied" staleness signal :meth:`_on_camera_reopen`
        already raises for a reopen -- never a refusal to serve frames,
        just a flag a client can act on.
        """
        with self._lock:
            cam = self._cameras.get(config.name)
            prior = self._camera_configs.get(config.name)
        if cam is not None:
            cam.apply_config(config)
        if self._is_resolution_change(prior, config):
            self._mark_flatfield_stale(config.name)
        with self._lock:
            self._camera_configs[config.name] = config

    @staticmethod
    def _is_resolution_change(prior: Optional[CameraConfig], new: CameraConfig) -> bool:
        """Whether *new* changes ``frame_width``/``frame_height`` from
        whatever *prior* held (sprint 037) -- ``False`` when *prior* is
        ``None`` (nothing to change from, a first-ever push) or when
        neither config states a real value for either key (nothing
        comparable)."""
        if prior is None:
            return False
        for key in ("frame_width", "frame_height"):
            old_value = prior.settings.get(key)
            new_value = new.settings.get(key)
            if old_value is not None and new_value is not None and old_value != new_value:
                return True
        return False

    def reset_camera_config(self, camera: str) -> None:
        """See ``daemon.interfaces.Daemon.reset_camera_config`` --
        "the RPC face of ``Camera.reset()``" (that base-class docstring's
        own words). Ticket 023-004.

        Opens *camera* if it is not already held (:meth:`_camera_for`,
        the same on-demand-open discipline every other camera-addressed
        method here uses -- ``UnknownCamera``/``CameraNotPresent``
        propagate exactly as they would from ``get_frame``/``calibrate``).
        Every ``Camera`` :meth:`_camera_for` constructs already carries
        the ``config_loader``/``config_path`` :meth:`Camera.reset` needs
        (wired at construction, see :meth:`_config_path_for`), so this
        method never touches ``config.loader`` or ``self._directories``
        itself -- disk access for reset stays entirely inside ``Camera``,
        this service only orchestrates (sprint.md Architecture "Modules"
        point 1: this service stays ignorant of disk beyond handing the
        already-resolved path/loader to ``Camera`` at construction).

        :meth:`Camera.reset` re-reads and re-applies whatever is at
        `config_dir/cameras/<slug>/config.json` (or the blank
        introspected-plus-blank template when nothing is installed --
        sprint.md Decision 3), discarding whatever live override this
        service held. The resulting document
        (:meth:`Camera.stored_config`) then replaces this service's
        ``_camera_configs`` entry for the camera, so
        :meth:`get_camera_config`/:meth:`list_cameras` reflect the reset
        immediately -- exactly the way a live :meth:`set_camera_config`
        push already would (SUC-003's postcondition: fetching the config
        after a reset returns the on-disk document, or the blank
        template, never the discarded override).
        """
        cam = self._camera_for(camera)
        cam.reset()
        config = cam.stored_config()
        with self._lock:
            self._camera_configs[cam.slug] = config

    def _playfield_link(self, slug: str) -> Optional[str]:
        """The playfield *slug* is currently linked to, per the last
        :meth:`set_camera_config` push -- ``None`` if never configured.
        """
        with self._lock:
            config = self._camera_configs.get(slug)
        return config.playfield if config is not None else None

    # -- annotations (ticket 024-001) ---------------------------------------
    #
    # The shared drawing surface (spec section 9 decision 8, UC-28):
    # ``self._annotations`` (playfield -> layer -> id -> Annotation) and
    # ``self._annotation_revisions`` (playfield -> monotonic counter),
    # both declared alongside ``_playfields``/``_camera_configs`` in
    # ``__init__`` and guarded the same way, by ``self._lock``. Runtime
    # state, record-level, gone on restart, never written to disk --
    # there is simply no write-to-disk path here, the same structural
    # guarantee ``_playfields``/``_camera_configs`` already have.
    # (The mobile-tag registry below used to be this same shape too,
    # but is disk-backed as of ticket 036-007 -- see that section's own
    # header comment.)
    #
    # Unknown playfield is rejected on all five methods below
    # (``UnknownPlayfield``, naming every playfield currently held) --
    # matching ``get_playfield``'s own precedent exactly (sprint.md
    # Architecture, resolved UC-28 open question 2). An unknown *layer*
    # within a *known* playfield is never an error one level down: a
    # no-op for ``remove_annotation``/``clear_annotations``, an empty
    # result for ``get_annotations``.

    def _known_playfields_locked(self) -> Tuple[str, ...]:
        """Every playfield name currently held, sorted -- the
        ``UnknownPlayfield`` ``available`` list every method below
        raises with. Assumes :attr:`_lock` is already held by the
        caller (mirrors :meth:`get_playfield`'s own inline check, just
        factored out since five methods need it instead of one)."""
        return tuple(sorted(self._playfields.keys()))

    def _require_known_playfield_locked(self, playfield: str) -> None:
        """Raise :class:`~aprilcam.errors.UnknownPlayfield` for a
        *playfield* this daemon does not hold. Assumes :attr:`_lock` is
        already held -- every one of the five annotation methods below
        calls this as its very first act inside its own ``with
        self._lock:`` block, so the known-playfield check and the
        mutation/read it guards happen under one uninterrupted critical
        section (no other thread's ``set_playfield``/annotation call can
        interleave between the check and the effect)."""
        if playfield not in self._playfields:
            raise UnknownPlayfield(playfield, self._known_playfields_locked())

    def _bump_annotation_revision_locked(self, playfield: str) -> None:
        """Increment *playfield*'s annotation revision by one. Assumes
        :attr:`_lock` is already held. The single mutation point every
        actual annotation change goes through, so "revision increments
        on every real mutation, never on a no-op" is true by
        construction: a method that decides nothing actually changed
        simply never calls this."""
        self._annotation_revisions[playfield] = self._annotation_revisions.get(playfield, 0) + 1

    def put_annotation(self, playfield: str, annotation: Annotation) -> None:
        """See ``daemon.interfaces.Daemon.put_annotation``.

        Upserts by *annotation*'s own ``(layer, id)`` -- last write
        wins on a colliding key, no merge, no error (UC-28.S-6): the
        dict assignment below simply replaces whatever was there.
        Always bumps *playfield*'s revision -- a put is definitionally
        a real mutation, never a no-op.

        Rejects with :class:`~aprilcam.errors.AnnotationLimitExceeded`
        (ticket 031-010), before touching ``self._annotations`` at all,
        when *annotation*'s shape exceeds :data:`_MAX_POINTS_PER_SHAPE`,
        or when *annotation*'s id is genuinely new to the target layer
        and that layer already holds :data:`_MAX_ANNOTATIONS_PER_LAYER`
        annotations -- an upsert of an id already present never counts
        against the count bound, since it does not grow the layer.
        """
        with self._lock:
            self._require_known_playfield_locked(playfield)
            _check_point_bound(annotation.shape)
            layer = self._annotations.get(playfield, {}).get(annotation.layer, {})
            if annotation.id not in layer and len(layer) >= _MAX_ANNOTATIONS_PER_LAYER:
                raise AnnotationLimitExceeded(
                    "annotations", _MAX_ANNOTATIONS_PER_LAYER, len(layer) + 1
                )
            self._annotations.setdefault(playfield, {}).setdefault(
                annotation.layer, {}
            )[annotation.id] = annotation
            self._bump_annotation_revision_locked(playfield)

    def remove_annotation(self, playfield: str, layer: str, id: str) -> None:
        """See ``daemon.interfaces.Daemon.remove_annotation``.

        A no-op -- not an error, and no revision bump -- when *playfield*
        has nothing stored at all, when *layer* does not exist on it, or
        when *id* is not present in that layer. Only an actual removal
        bumps the revision.
        """
        with self._lock:
            self._require_known_playfield_locked(playfield)
            layers = self._annotations.get(playfield)
            if not layers:
                return
            annotations = layers.get(layer)
            if not annotations or id not in annotations:
                return
            del annotations[id]
            self._bump_annotation_revision_locked(playfield)

    def replace_layer(
        self, playfield: str, layer: str, annotations: Tuple[Annotation, ...]
    ) -> None:
        """See ``daemon.interfaces.Daemon.replace_layer``.

        A single dict-assignment swap of *layer*'s entire contents,
        atomic under :attr:`_lock` -- no consumer ever observes a
        half-updated layer; every frame embed or ``get_annotations``
        call sees either the layer's old contents or its new ones,
        never a mix (UC-28.S-2). Unlike ``remove``/``clear``, this
        always bumps the revision, including replacing an absent or
        already-empty layer with an empty tuple -- a replace is a
        deliberate whole-layer redraw, not a conditional mutation.

        Rejects with :class:`~aprilcam.errors.AnnotationLimitExceeded`
        (ticket 031-010), before touching ``self._annotations`` at all,
        when *annotations* itself exceeds
        :data:`_MAX_ANNOTATIONS_PER_LAYER`, or when any one shape in it
        exceeds :data:`_MAX_POINTS_PER_SHAPE` -- the layer's *previous*
        contents are left completely unchanged on rejection, matching
        this method's existing atomicity guarantee (UC-28.S-2).
        """
        with self._lock:
            self._require_known_playfield_locked(playfield)
            if len(annotations) > _MAX_ANNOTATIONS_PER_LAYER:
                raise AnnotationLimitExceeded(
                    "annotations", _MAX_ANNOTATIONS_PER_LAYER, len(annotations)
                )
            for one in annotations:
                _check_point_bound(one.shape)
            self._annotations.setdefault(playfield, {})[layer] = {
                a.id: a for a in annotations
            }
            self._bump_annotation_revision_locked(playfield)

    def clear_annotations(self, playfield: str, layer: Optional[str] = None) -> None:
        """See ``daemon.interfaces.Daemon.clear_annotations``.

        ``layer=None`` clears every layer on *playfield*; a named
        *layer* clears just that one. Either way, clearing something
        that is already empty -- a nonexistent layer, an
        already-cleared layer, or a playfield with nothing stored at
        all -- is a no-op: not an error, and no revision bump. Only a
        clear that actually removes at least one annotation bumps the
        revision.
        """
        with self._lock:
            self._require_known_playfield_locked(playfield)
            layers = self._annotations.get(playfield)
            if not layers:
                return
            if layer is None:
                if not any(layers.values()):
                    return
                self._annotations[playfield] = {}
                self._bump_annotation_revision_locked(playfield)
                return
            if not layers.get(layer):
                return
            del layers[layer]
            self._bump_annotation_revision_locked(playfield)

    def get_annotations(self, playfield: str, layer: Optional[str] = None) -> AnnotationSet:
        """See ``daemon.interfaces.Daemon.get_annotations``.

        ``layer=None`` flattens every layer's annotations into one
        set (each :class:`~aprilcam.types.Annotation` still carries its
        own ``layer`` field, so the flattening loses no information); a
        named *layer* filters to just that one. ``revision`` is always
        *playfield*'s whole-store revision regardless of the ``layer``
        filter -- a caller reading one layer still learns whether
        *anything* on the playfield changed since its last read, which
        the (deliberately not offered) alternative of a per-layer
        revision counter could not give it for free. A playfield that
        has never seen a mutation reports revision 0 -- the same value
        a freshly restarted daemon reports (UC-28.S-4), by construction:
        ``self._annotation_revisions`` simply has no entry yet.
        """
        with self._lock:
            self._require_known_playfield_locked(playfield)
            layers = self._annotations.get(playfield, {})
            revision = self._annotation_revisions.get(playfield, 0)
            if layer is None:
                annotations = tuple(
                    annotation
                    for layer_annotations in layers.values()
                    for annotation in layer_annotations.values()
                )
            else:
                annotations = tuple(layers.get(layer, {}).values())
        return AnnotationSet(annotations=annotations, revision=revision, timestamp=time.time())

    # -- mobile-tag registry (ticket 025-004, UC-12) -------------------------
    #
    # ``self._mount_params`` (``TagId -> MountParameters``), guarded by the
    # same ``self._lock`` as every other store this class holds --
    # **disk-backed as of ticket 036-007**, unlike the annotation store
    # above. A mount registration describes physical reality (where a
    # tag is bolted to a robot), not a per-session experiment, so it now
    # survives a daemon restart the same way ``_calibrations`` already
    # does: :meth:`register_tag`/:meth:`unregister_tag` write through to
    # ``<state_dir>/mounts/registry.json`` (:meth:`_persist_mounts`) and
    # :meth:`_load_mounts` reloads it at construction time -- the
    # identical tmp+fsync+``os.replace`` write and tolerant-of-corruption
    # load stance as :meth:`_persist_calibration`/:meth:`_load_calibrations`
    # below, reused deliberately rather than inventing a new pattern. The
    # actual correction math lives in ``calibration.geometry``/
    # ``daemon.detection`` (this class only holds the registrations and
    # hands two lookups to ``DetectionEngine`` --
    # :meth:`_mount_lookup`/:meth:`_camera_geometry_for` below -- so it
    # never duplicates that math itself).

    def register_tag(self, tag: TagId, params: MountParameters) -> Optional[str]:
        """See ``daemon.interfaces.Daemon.register_tag``.

        Upserts by *tag* -- a second registration for the same tag
        replaces the first outright, no merge, mirroring
        :meth:`set_camera_config`'s own whole-document-replace
        precedent.

        UC-12.S-5: a **nonzero** ``params.mount_z`` (there is an
        elevation for the parallax projection to actually apply) is
        rejected with :class:`~aprilcam.errors.NotCalibrated` when this
        daemon holds **no calibration for any camera at all** --
        ``mount_z`` set with no calibration anywhere the tag could be
        observed would otherwise be silently stored but permanently
        inert, never applying to anything (this ticket's own acceptance
        criterion). Read literally as "any camera," not one specific
        camera, since a mobile tag is not tied to a single camera the
        way ``get_calibration``'s query is -- it may be seen by whichever
        camera happens to be watching. A *zero* ``mount_z`` (the
        ``MountParameters`` default -- no elevation at all) never
        triggers this check, calibrated or not, since there is nothing
        for parallax to project in that case.

        A partial spec (``mount_z`` set, ``mount_x``/``mount_y`` left at
        their own ``0.0`` default) is accepted without complaint --
        ``MountParameters``' own fields already default to zero offset,
        so there is nothing to validate here (UC-12's own "common case"
        failure mode). A registration for a tag never yet detected is
        accepted unconditionally -- the tag may appear later (UC-12's
        own failure-mode table) -- and takes effect the moment
        ``DetectionEngine.run_burst`` next asks :meth:`_mount_lookup`
        about it.

        **Ticket 030-004, SUC-004**: this guard is deliberately left
        exactly as it was (sprint.md's Architecture, Design Rationale
        Decision 4) -- registering a mount before running ``camera
        locate`` on the camera that will observe it is a legitimate,
        order-independent workflow (UC-12) and must keep working. What
        changed is the *return value*: when a nonzero ``mount_z`` was
        just accepted but no camera this daemon knows about has been
        located yet (:meth:`_any_camera_located`), a warning string
        naming ``aprilcam camera locate`` as the fix is returned --
        purely informational, never raised, never blocking. ``None``
        means either ``mount_z`` was zero or at least one camera is
        already located.

        **Ticket 036-007**: a successful registration is now persisted
        to ``<state_dir>/mounts/registry.json`` (:meth:`_persist_mounts`)
        before this method returns, so it survives a daemon restart --
        no separate save step, no ``--session-only`` opt-out. The write
        happens while :attr:`_lock` is still held (unlike
        :meth:`calibrate`'s persist-after-release ordering), because
        :meth:`_persist_mounts` re-reads the *entire* current
        ``self._mount_params`` dict rather than a single already-captured
        value -- serializing it under the same lock that guards every
        mutation of that dict is what keeps two concurrent registrations
        from a lost-update race against this one shared file.
        """
        with self._lock:
            if params.mount_z != 0.0 and not self._calibrations:
                raise NotCalibrated(
                    f"cannot register mount_z for {tag!r}: this daemon holds no "
                    "calibration for any camera, so the parallax projection "
                    "would be meaningless -- calibrate a camera first"
                )
            self._mount_params[tag] = params
            self._persist_mounts()

        if params.mount_z > 0.0 and not self._any_camera_located():
            return (
                f"mount_z={params.mount_z:g} for {tag.family.value} {tag.number} is "
                "currently inert: no camera this daemon knows about has been "
                "located yet (CameraConfig.camera_position), so the parallax "
                "correction has nowhere to apply -- mount_x/mount_y/mount_yaw_rad "
                "still apply normally. Ask your operator to run `aprilcam camera "
                "locate` against the camera(s) that will observe this tag, then "
                "re-check with list_tag_parameters (mount_z_applied)."
            )
        return None

    def unregister_tag(self, tag: TagId) -> None:
        """See ``daemon.interfaces.Daemon.unregister_tag``.

        An absent *tag* is a no-op, matching :meth:`clear_annotations`'s
        established no-op-on-absent convention (sprint 024) -- there is
        nothing to undo for a tag that was never registered, or already
        unregistered. :meth:`_persist_mounts` still runs on that no-op
        path (ticket 036-007) -- a harmless rewrite of an unchanged
        registry file rather than special-cased away, exactly like
        removing an entry that *was* present rewrites the file to drop
        it from disk, not just from memory.
        """
        with self._lock:
            self._mount_params.pop(tag, None)
            self._persist_mounts()

    def list_tag_parameters(self) -> Dict[TagId, MountRegistration]:
        """See ``daemon.interfaces.Daemon.list_tag_parameters``.

        A snapshot, safe for a caller to mutate freely without touching
        this service's own store -- the same snapshot-not-live-reference
        discipline every other collection this class hands back
        (``list_cameras``, ``list_playfields``) already follows.

        **Ticket 030-004, SUC-004**: each entry now pairs its stored
        ``MountParameters`` with a computed ``mount_z_applied`` (see
        :meth:`_mount_z_applied`) -- the observable flag this ticket
        exists to add, so a stored-but-inert ``mount_z`` (a calibrated
        but never-``locate``\\ d camera) is discoverable here instead of
        only by cross-referencing ``get_camera_config`` by hand.
        """
        with self._lock:
            snapshot = dict(self._mount_params)
        return {
            tag: MountRegistration(
                params=params, mount_z_applied=self._mount_z_applied(params.mount_z)
            )
            for tag, params in snapshot.items()
        }

    def _mount_lookup(self, tag: TagId) -> Optional[MountParameters]:
        """``DetectionEngine``'s ``mount_lookup`` callable (ticket
        025-004) -- a locked read of :attr:`_mount_params`, the same
        lock-guarded-dict-read pattern :meth:`_camera_config_for_calibration`/
        :meth:`_playfield_for_calibration` already establish."""
        with self._lock:
            return self._mount_params.get(tag)

    def _camera_geometry_for(self, slug: str) -> Tuple[WorldPoint, float]:
        """``DetectionEngine``'s ``camera_geometry_lookup`` callable
        (ticket 025-004; wired to a real source by sprint 029 ticket
        001 -- no longer explicitly provisional).

        ``calibration.geometry.project_tag_to_field_plane`` needs a
        camera's own ground position and height above the field to
        perform its parallax projection. This reads it from that
        camera's configured ``config.interfaces.CameraConfig.
        camera_position`` (``config.interfaces.CameraPosition``:
        ``nadir``/``height_cm``) -- the same in-memory
        :attr:`_camera_configs` store ``set_camera_config``/
        ``get_camera_config`` already hold, set by an operator (by hand,
        or via ``aprilcam camera locate``, sprint 029 ticket 003)
        exactly like every other camera setting.

        A camera with no known configuration at all (``config is
        None``) or one that has never been located (``config.
        camera_position is None``) falls back to the exact pre-029
        stopgap: ground position ``(0, 0)``, height ``0.0`` --
        ``project_tag_to_field_plane``'s own documented "nothing to
        correct" sentinel (height not strictly positive short-circuits
        to the identity). A registered ``mount_z`` for such a camera
        remains fully wired end-to-end and covered by unit tests with
        hand-picked geometry (``tests/aprilcam/daemon/test_detection.py``),
        but has no observable effect until that camera is located --
        only ``mount_x``/``mount_y``/``mount_yaw_rad``'s offset+yaw
        composition (``calibration.geometry.apply_mount_offset``, which
        needs no camera geometry at all) moves a reported position
        either way.

        See ``daemon/detection.py``'s ``_apply_mount_correction`` for
        the companion guard: a registered ``mount_z`` at or above a
        configured camera's own ``height_cm`` skips correction rather
        than extrapolating past the camera's own nadir.
        """
        with self._lock:
            config = self._camera_configs.get(slug)
        if config is None or config.camera_position is None:
            return WorldPoint(x=0.0, y=0.0), 0.0
        return config.camera_position.nadir, config.camera_position.height_cm

    def _any_camera_located(self) -> bool:
        """Whether **any** camera this service knows about currently
        has real, solved geometry rather than :meth:`_camera_geometry_for`'s
        pre-029 stopgap (ticket 030-004, SUC-004).

        Reuses :meth:`_camera_geometry_for` itself, once per configured
        camera -- no new guard logic, just the same "known vs. stopgap"
        read :meth:`_camera_geometry_for`'s own docstring already
        documents (height strictly positive means real; ``0.0`` is the
        stopgap sentinel, matching
        ``calibration.geometry.project_tag_to_field_plane``'s own
        ``camera_height_cm <= 0.0`` identity short-circuit).

        Deliberately "any camera," not one specific camera -- the same
        scope :meth:`register_tag`'s ``NotCalibrated`` guard already
        uses (Decision 4, sprint.md's Architecture): a mobile tag is
        not bound to a single observing camera, so this asks whether
        *any* camera in the fleet is located, not whether one
        particular camera is.
        """
        with self._lock:
            slugs = tuple(self._camera_configs.keys())
        return any(self._camera_geometry_for(slug)[1] > 0.0 for slug in slugs)

    def _mount_z_applied(self, mount_z: float) -> bool:
        """Whether a registered ``mount_z`` of this magnitude is
        currently being applied by the parallax correction (ticket
        030-004, SUC-004; see :class:`~aprilcam.daemon.interfaces.
        MountRegistration`). ``mount_z <= 0.0`` is vacuously ``True`` --
        there is nothing for parallax to project, so nothing is being
        withheld; the check otherwise mirrors :meth:`_any_camera_located`
        exactly, matching ``project_tag_to_field_plane``'s own
        ``mount_z_cm <= 0.0`` no-op sentinel."""
        if mount_z <= 0.0:
            return True
        return self._any_camera_located()

    def _mounts_path(self) -> Path:
        """``<state_dir>/mounts/registry.json`` (ticket 036-007) --
        mirrors :meth:`_calibration_path`'s shape and ``camera/
        registry.py``'s ``<state_dir>/cameras/registry.json`` sibling
        layout. ``state_dir``, not ``config_dir``: mount registrations
        are written by the daemon itself in response to an RPC, exactly
        like calibration, never hand-edited by an operator."""
        return self._directories.state_dir / "mounts" / MOUNTS_FILENAME

    def _persist_mounts(self) -> None:
        """Atomically write the **entire** current ``self._mount_params``
        to ``mounts/registry.json``: tmp file, ``fsync``, ``os.replace``
        -- the identical tmp+fsync+``os.replace`` pattern
        :meth:`_persist_calibration` uses (ticket 036-007's decision to
        reuse that pattern exactly rather than inventing a new one).

        Always called with :attr:`_lock` already held, by
        :meth:`register_tag`/:meth:`unregister_tag`. This differs from
        :meth:`_persist_calibration`, which persists one
        already-captured value into a per-camera file and can safely
        run *after* the lock is released (a slow disk write should not
        hold up an unrelated concurrent caller) -- :meth:`_persist_mounts`
        instead re-reads the entire current ``self._mount_params`` dict
        itself, because the mount registry is one shared file, not one
        per tag. Running it under the same lock that guards every
        mutation of that dict is what keeps two concurrent
        registrations from a lost-update race against that one file.
        """
        path = self._mounts_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"cannot create mounts directory {path.parent}: {exc}") from exc

        payload = {
            _mount_registry_key(tag): _mount_params_to_dict(params)
            for tag, params in self._mount_params.items()
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise OSError(f"cannot write mounts file {path}: {exc}") from exc
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

    def _load_mounts(self) -> None:
        """Reload ``mounts/registry.json`` at construction time (ticket
        036-007) -- mirrors :meth:`_load_calibrations`'s stance exactly:
        a missing file (first run) or a corrupt/malformed record is
        tolerated by skipping it (logged as a warning), never by
        failing daemon construction.

        Populates ``self._mount_params`` **directly**, never by calling
        :meth:`register_tag` -- a reload must bypass that method's
        ``NotCalibrated`` precondition the same way :meth:`_load_calibrations`
        bypasses ``Calibrator.calibrate``'s own validation on load. A
        persisted nonzero-``mount_z`` registration reloaded into a
        daemon with no calibration at all must not crash startup and
        must not silently vanish -- it behaves exactly like a fresh
        registration made into that same daemon state today: the
        registration stands, height correction stays inert until a
        camera is calibrated *and* located, and ``mount_z_applied``
        (:meth:`_mount_z_applied`) keeps reporting the truth dynamically
        at query time, since that computation already reads current
        calibration/location state, never anything stored on the
        registration itself.
        """
        path = self._mounts_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("corrupt mount registry at %s: %s -- ignoring", path, exc)
            return
        if not isinstance(raw, dict):
            logger.warning(
                "corrupt mount registry at %s: expected a JSON object, got %s "
                "-- ignoring",
                path,
                type(raw).__name__,
            )
            return
        for key, value in raw.items():
            try:
                tag = _mount_registry_key_to_tag(key)
                params = _mount_params_from_dict(value)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "malformed mount registry entry %r at %s: %s -- skipping",
                    key,
                    path,
                    exc,
                )
                continue
            self._mount_params[tag] = params

    # -- calibration (ticket 022-003) --------------------------------------

    def calibrate(self, camera: str, *, estimate_intrinsics: bool = False) -> CalibrationResult:
        """See ``daemon.interfaces.Daemon.calibrate``.

        A thin wrapper: every vision/geometry decision (multi-frame
        averaging, id matching, all-or-nothing acceptance, homography
        solve, degenerate-geometry rejection, and -- sprint 030 ticket
        002 -- intrinsics estimation) lives in
        ``calibration.calibration.Calibrator``, injected as
        ``self._calibrator`` -- this method's only job is to run it and,
        on success, persist the result. ``NoPlayfieldLinked`` propagates
        straight through from the calibrator (raised there before it
        ever calls :meth:`_capture_calibration_frames`, so an unlinked
        camera never reaches the camera backend at all -- UC-15.S-2).

        *camera* accepts a persistent number or a slug (SUC-005) --
        resolved to the canonical slug via :meth:`_resolve_slug_if_known`
        **before** anything else happens, and that resolved slug is what
        gets passed to the calibrator and used as the
        ``self._calibrations``/``calibration.json`` key. Without this,
        a number token never matches ``_camera_configs`` (keyed by slug
        only) and every numbered camera reports the misleading
        ``NoPlayfieldLinked`` regardless of whether it is actually
        linked -- ticket 022-003's addressing regression, fixed here so
        every other camera-config-addressed method inherits the same
        resolution rather than re-implementing it (see
        :meth:`_resolve_slug_if_known`'s docstring).

        *estimate_intrinsics* is passed straight through to
        ``self._calibrator.calibrate`` -- this method makes no decision
        of its own about it, matching the "pure carry a bool from
        caller to Calibrator" framing of this parameter throughout the
        stack (sprint.md Architecture, responsibility 4).

        A failed attempt (``result.succeeded is False``) leaves
        ``calibration.json`` and this service's in-memory
        ``self._calibrations`` entry for *camera* completely untouched
        -- there is simply no write path taken on that branch, so "a
        prior-good calibration survives a failed retry" (UC-04.S-2,
        UC-15.S-3) holds by construction rather than by an explicit
        guard.
        """
        slug = self._resolve_slug_if_known(camera)
        result = self._calibrator.calibrate(slug, estimate_intrinsics=estimate_intrinsics)
        if result.succeeded:
            calibration = self._calibrator.get_calibration(slug)
            assert calibration is not None, (
                "Calibrator.calibrate() reported succeeded=True but "
                "get_calibration() has nothing stored -- a Calibrator bug"
            )
            with self._lock:
                self._calibrations[slug] = calibration
            self._persist_calibration(slug, calibration)
        return result

    def get_calibration(self, camera: str) -> Optional[Calibration]:
        """See ``daemon.interfaces.Daemon.get_calibration``.

        Answers from this service's in-memory cache -- populated by a
        successful :meth:`calibrate` call in this process, or by
        :meth:`_load_calibrations` at construction time from whatever
        ``calibration.json`` files already existed on disk (UC-01: a
        calibrated camera stays calibrated across a daemon restart).
        ``None`` for a camera that has never been calibrated -- never a
        fabricated/default ``Calibration``, mirroring
        :meth:`get_tag`'s "``None`` means not detected, never a
        zero-valued record" discipline.

        *camera* accepts a persistent number or a slug (SUC-005),
        resolved via :meth:`_resolve_slug_if_known` -- ``self.
        _calibrations`` is always keyed by the resolved slug (see
        :meth:`calibrate`), so looking it up by the raw token would
        silently report "never calibrated" for a numbered camera that
        actually is.
        """
        slug = self._resolve_slug_if_known(camera)
        with self._lock:
            calibration = self._calibrations.get(slug)
        if calibration is None or calibration.lens_source != "measured":
            return calibration
        # Sprint 044 ticket 010: summarise a measured lens (views/rms/...)
        # from the store, but only while the store still holds the very
        # model this calibration embedded -- otherwise the numbers would
        # describe a different lens.
        stored = self._lens_store.load(slug)
        if stored is None or not _same_lens(calibration, stored):
            return calibration
        return replace(
            calibration,
            lens_views=stored.views,
            lens_rms_px=stored.rms_px,
            lens_residual_px=stored.residual_px,
            lens_installed_at=stored.installed_at or None,
        )

    def _mark_stale(self, slug: str) -> None:
        """Flip *slug*'s currently-held calibration's ``stale`` bit, if
        it has one (ticket 023-007) -- the single mutation point both
        staleness triggers share: wired as ``camera.camera.Camera``'s
        ``on_reopen`` callback in :meth:`_camera_for` (sprint.md
        Decision 6), and as ``daemon.detection.DetectionEngine``'s
        ``mark_stale`` callable, called from its opportunistic drift
        check (Decision 5). *slug* is always an already-resolved
        registry slug in both callers -- never a raw number-or-slug
        token -- so, unlike :meth:`calibrate`/:meth:`get_calibration`,
        this method does not call :meth:`_resolve_slug_if_known` itself.

        Takes :attr:`_lock` exactly like every other ``self._calibrations``
        mutation already does (:meth:`calibrate`) -- pinned explicitly by
        the ticket: two concurrent bursts (or a burst racing a reopen) on
        the same camera must not corrupt the store. A no-op for a slug
        with no held calibration (never calibrated -- nothing to flag as
        wrong) or one already marked stale (idempotent, and avoids a
        redundant disk write on repeated triggers for the same still-bad
        camera).

        Persists the change to ``calibration.json`` (:meth:`_persist_calibration`,
        the same atomic write :meth:`calibrate` already uses) so a
        staleness flip survives a daemon restart -- ``Calibration`` is
        file-backed (UC-01), and a restart silently un-flagging a bumped
        camera as "not stale" would reintroduce exactly the failure mode
        this ticket exists to close. Persisted *after* releasing
        :attr:`_lock`, mirroring :meth:`calibrate`'s own ordering, so a
        slow disk write never holds up a concurrent caller's unrelated
        camera.
        """
        with self._lock:
            calibration = self._calibrations.get(slug)
            if calibration is None or calibration.stale:
                return
            updated = replace(calibration, stale=True)
            self._calibrations[slug] = updated
        self._persist_calibration(slug, updated)

    def _on_camera_reopen(self, slug: str) -> None:
        """``Camera``'s ``on_reopen`` callback, wired in :meth:`_camera_for`
        (sprint 037): a successful self-heal/explicit reopen is the same
        real-world "camera may have moved" event for a flat-field gain
        map as it already is for a calibration (``Camera.__init__``'s own
        ``on_reopen`` docstring), so it flags both -- :meth:`_mark_stale`
        unchanged from before this ticket, plus :meth:`_mark_flatfield_stale`
        as the new sibling trigger."""
        self._mark_stale(slug)
        self._mark_flatfield_stale(slug)

    def _camera_config_for_calibration(self, camera: str) -> Optional[CameraConfig]:
        """``Calibrator``'s ``get_camera_config`` callable -- a locked
        read of :attr:`_camera_configs`, the same store
        :meth:`get_camera_config`/:meth:`_playfield_link` already read
        under :attr:`_lock`."""
        with self._lock:
            return self._camera_configs.get(camera)

    def _playfield_for_calibration(self, name: str) -> Optional[PlayfieldDefinitionIface]:
        """``Calibrator``'s ``get_playfield`` callable -- a locked read
        of :attr:`_playfields`."""
        with self._lock:
            return self._playfields.get(name)

    def _capture_calibration_frames(self, camera: str) -> Tuple[np.ndarray, ...]:
        """``Calibrator``'s ``capture_frames`` callable -- the one place
        calibration touches a camera backend (the module boundary's
        "camera frame access, handed in" rule). Resolves *camera* via
        :meth:`_camera_for` (opening it on first use, exactly like
        :meth:`get_frame`/:meth:`get_tags`) and reads up to
        :data:`CALIBRATION_FRAME_COUNT` frames, skipping any individual
        read that returns ``None`` (a transient capture miss) rather
        than failing the whole capture -- ``Calibrator.calibrate`` is
        the one that decides whether the frames actually collected are
        enough (it raises if this returns nothing at all).

        Never called for a camera with no playfield link: ``Calibrator.
        calibrate`` raises ``NoPlayfieldLinked`` before this callable is
        invoked, so :meth:`_camera_for` -- which can open a backend --
        is never reached on that path (UC-15.S-2's "zero calls to the
        camera backend").
        """
        cam = self._camera_for(camera)
        frames: List[np.ndarray] = []
        for _ in range(CALIBRATION_FRAME_COUNT):
            frame = cam.read()
            if frame is not None:
                frames.append(frame)
        return tuple(frames)

    def _calibration_path(self, camera: str) -> Path:
        """``<state_dir>/cameras/<camera>/calibration.json`` -- design.md
        §7's storage layout, matching ``camera/registry.py``'s
        ``<state_dir>/cameras/registry.json`` sibling layout (a
        per-camera subdirectory, not a shared file, so one camera's
        calibration can never collide with another's)."""
        return self._directories.state_dir / "cameras" / camera / CALIBRATION_FILENAME

    def _persist_calibration(self, camera: str, calibration: Calibration) -> None:
        """Atomically write *calibration* to *camera*'s
        ``calibration.json``: tmp file, ``fsync``, ``os.replace`` --
        the same tmp+fsync+rename pattern ``camera/registry.py``'s
        ``_save`` uses, so a crash between those steps leaves the prior
        file (if any) exactly as it was, never half-written."""
        path = self._calibration_path(camera)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(
                f"cannot create calibration directory {path.parent}: {exc}"
            ) from exc

        text = json.dumps(calibration.to_dict(), indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise OSError(f"cannot write calibration file {path}: {exc}") from exc
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

    def _load_calibrations(self) -> None:
        """Reload every camera's ``calibration.json`` at construction
        time (UC-01: "reloads it at startup like any other file").
        Scans ``<state_dir>/cameras/*/calibration.json`` directly rather
        than going through the camera registry, so a calibration is
        found regardless of whether this particular ``DaemonService``
        instance has re-enumerated that camera yet -- the file, not the
        registry, is the source of truth here. A missing ``state_dir``
        (first run) or a corrupt/malformed calibration file is tolerated
        by skipping it (logged as a warning), never by failing daemon
        construction -- mirrors ``camera.registry.CameraRegistry``'s
        "a corrupt file must never crash the daemon" stance.
        """
        cameras_dir = self._directories.state_dir / "cameras"
        if not cameras_dir.is_dir():
            return
        for entry in sorted(cameras_dir.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / CALIBRATION_FILENAME
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                calibration = Calibration.from_dict(data)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "corrupt calibration file for %s at %s: %s -- ignoring",
                    entry.name,
                    path,
                    exc,
                )
                continue
            self._calibrations[entry.name] = calibration

    # -- ChArUco intrinsics (sprint 044) ---------------------------------------
    #
    # Orchestration only: session lifecycle lives in
    # ``intrinsics.manager.IntrinsicsManager`` and all ChArUco math in the
    # ``intrinsics`` package.

    def start_intrinsics(self, camera: str, board: Optional[str] = None) -> IntrinsicsStatus:
        """See ``daemon.interfaces.Daemon.start_intrinsics``."""
        cam = self._camera_for(camera)  # opens on demand; UnknownCamera etc. propagate
        return self._intrinsics.start(cam.slug, board)

    def get_intrinsics_status(self, camera: str) -> IntrinsicsStatus:
        """See ``daemon.interfaces.Daemon.get_intrinsics_status``."""
        return self._intrinsics.status(self._resolve_slug_if_known(camera))

    def finish_intrinsics(
        self, camera: str, install: bool, force: bool = False
    ) -> IntrinsicsFinishResult:
        """See ``daemon.interfaces.Daemon.finish_intrinsics``."""
        return self._intrinsics.finish(self._resolve_slug_if_known(camera), install, force)

    def abort_intrinsics(self, camera: str) -> IntrinsicsStatus:
        """See ``daemon.interfaces.Daemon.abort_intrinsics``."""
        return self._intrinsics.abort(self._resolve_slug_if_known(camera))

    # -- flat-field gain map (sprint 037) ------------------------------------
    #
    # ``capture_flat_field``/``get_flatfield``/``clear_flat_field`` are the
    # thin orchestrator this daemon owns -- the identical ``calibrate``/
    # ``Calibrator`` split (module docstring): this class resolves the
    # camera, derives the field-interior mask, pushes/restores a temporary
    # exposure override, and persists the result; ``calibration.flatfield.
    # FlatFieldCapturer`` (``self._flatfield_capturer``) does the actual
    # per-pixel math. ``self._flatfields`` (``str -> FlatFieldInfo``) is the
    # eighth in-memory store, disk-backed the same way ``self._calibrations``
    # is (``_persist_flatfield``/``_load_flatfields``, called from the
    # constructor and from every successful capture respectively).

    def capture_flat_field(
        self,
        camera: str,
        *,
        exposure: Optional[float] = None,
        frames: Optional[int] = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> FlatFieldResult:
        """See ``daemon.interfaces.Daemon.capture_flat_field``.

        Opens *camera* (:meth:`_camera_for`, on-demand-open discipline
        every other camera-addressed method uses -- ``UnknownCamera``/
        ``CameraNotPresent`` propagate exactly as they would from
        ``get_frame``/``calibrate``) and derives the field-interior mask
        (:meth:`_field_mask_for`, raising :class:`~aprilcam.errors.NoFieldMask`
        when neither a stored calibration nor a live ArUco-corner
        detection can produce one) **before** anything about the K-frame
        capture burst happens -- mirrors :meth:`calibrate`'s "precondition
        fails before any hardware disturbance" discipline.

        *exposure*, when given, is pushed as a temporary override on top
        of whatever this daemon currently holds for *camera*
        (:meth:`get_camera_config`'s existing stored-or-effective
        contract) -- the exact prior document is restored, unconditionally,
        in a ``finally`` covering the capture burst and the
        ``FlatFieldCapturer.build`` call alike, so a quality-gate refusal
        or an exception mid-capture never leaves the camera parked at the
        override.

        On a successful (or forced) result, persists ``flatfield.npy`` +
        ``flatfield.json`` (:meth:`_persist_flatfield`) and reloads the
        live-applied map on the held-open ``Camera`` -- no daemon restart.
        ``dry_run=True`` returns the evaluated result without writing
        anything or touching the live map; an unforced quality-gate
        refusal does the same, structurally (there is simply no write
        path taken on that branch).
        """
        cam = self._camera_for(camera)
        slug = cam.slug
        mask = self._field_mask_for(cam)

        prior_config = self.get_camera_config(slug)
        pushed_override = False
        try:
            if exposure is not None:
                override_settings = dict(prior_config.settings)
                override_settings["exposure"] = exposure
                cam.apply_config(replace(prior_config, settings=override_settings))
                pushed_override = True

            frame_count = frames if frames is not None else FLATFIELD_FRAME_COUNT
            burst = self._capture_flatfield_frames(cam, frame_count)
            result = self._flatfield_capturer.build(burst, mask, force=force)
        finally:
            if pushed_override:
                cam.apply_config(prior_config)

        if dry_run or not result.succeeded:
            return result

        self._persist_flatfield(slug, result)
        return result

    def get_flatfield(self, camera: str) -> Optional[FlatFieldInfo]:
        """See ``daemon.interfaces.Daemon.get_flatfield``.

        *camera* accepts a persistent number or a slug (SUC-005),
        resolved via :meth:`_resolve_slug_if_known` -- ``self._flatfields``
        is always keyed by the resolved slug (see :meth:`_persist_flatfield`),
        mirroring :meth:`get_calibration`'s identical addressing.
        """
        slug = self._resolve_slug_if_known(camera)
        with self._lock:
            return self._flatfields.get(slug)

    def clear_flat_field(self, camera: str) -> None:
        """See ``daemon.interfaces.Daemon.clear_flat_field``.

        Deletes both on-disk files (a missing file is a no-op, not an
        error -- clearing an already-clear camera is a legitimate,
        idempotent call), drops *camera*'s entry from ``self._flatfields``,
        and -- when this daemon currently holds *camera* open -- reloads
        its live map (:meth:`Camera._load_flatfield`, the same reload
        mechanism a fresh capture uses), which naturally clears to "no
        correction" now that the file is gone. Never opens *camera* itself
        for a not-currently-held one: there is nothing to reload on a
        closed handle, only the on-disk state to clean up.
        """
        slug = self._resolve_slug_if_known(camera)
        for path in (self._flatfield_path_for(slug), self._flatfield_info_path_for(slug)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        with self._lock:
            self._flatfields.pop(slug, None)
            cam = self._cameras.get(slug)
        if cam is not None:
            cam._load_flatfield()

    def _field_mask_for(self, cam: Camera) -> np.ndarray:
        """The boolean field-interior mask :meth:`capture_flat_field`
        builds *cam*'s gain map against (sprint 037): from *cam*'s stored
        ``Calibration`` when one exists (:meth:`_mask_from_calibration`),
        else from *cam*'s linked playfield's required ArUco corners
        detected live in one probe frame (:meth:`_mask_from_aruco`), else
        raises :class:`~aprilcam.errors.NoFieldMask` -- before any
        capture burst, matching this method's only caller's own
        precondition discipline.

        The probe frame this reads is also what supplies the mask's
        ``(height, width)`` and, on the ArUco fallback path, the pixels
        the detector runs against -- one read serves both, at *cam*'s
        current (pre-override) exposure.
        """
        slug = cam.slug
        probe = cam.read()
        if probe is None:
            raise NoFieldMask(slug)

        calibration = self.get_calibration(slug)
        if calibration is not None:
            definition = self._playfield_for_calibration(calibration.playfield)
            if definition is not None:
                return self._mask_from_calibration(calibration, definition, probe.shape[:2])

        playfield_name = self._playfield_link(slug)
        if playfield_name is not None:
            definition = self._playfield_for_calibration(playfield_name)
            if definition is not None:
                mask = self._mask_from_aruco(probe, definition)
                if mask is not None:
                    return mask

        raise NoFieldMask(slug)

    def _mask_from_calibration(
        self,
        calibration: Calibration,
        definition: PlayfieldDefinitionIface,
        shape: Tuple[int, int],
    ) -> np.ndarray:
        """The field rectangle's four world corners
        (:func:`~aprilcam.calibration.geometry.world_corners`, the shared
        helper this ticket promoted out of ``calibration/deskew.py``'s own
        ``_world_corners``), mapped through *calibration*'s already-solved
        homography (:meth:`Calibration.to_pixel`) and filled as a polygon
        into a boolean ``shape``-sized mask."""
        height, width = shape
        corners_world = geometry.world_corners(definition.width_cm, definition.height_cm)
        pixel_corners = np.array(
            [[p.x, p.y] for p in (calibration.to_pixel(w) for w in corners_world)],
            dtype=np.float64,
        )
        filled: np.ndarray = np.zeros((height, width), dtype=np.uint8)
        cv.fillPoly(filled, [np.round(pixel_corners).astype(np.int32)], (1,))
        return filled.astype(bool)

    def _mask_from_aruco(
        self, frame: np.ndarray, definition: PlayfieldDefinitionIface
    ) -> Optional[np.ndarray]:
        """*definition*'s required ArUco corner ids, detected directly in
        *frame* (``self._flatfield_detector``, a real ``vision.detector.
        TagDetector`` by default), filled as the convex hull of every
        detected corner point. ``None`` when *definition* names no
        required ArUco ids, or none of them are actually detected in
        *frame* -- :meth:`_field_mask_for` treats that as "this fallback
        also failed," not an error of its own."""
        required_ids = set(definition.required_aruco_ids())
        if not required_ids:
            return None
        gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        detections = self._flatfield_detector.detect(gray)
        points = [
            d.corners for d in detections if d.family == TagFamily.ARUCO and d.number in required_ids
        ]
        if not points:
            return None
        all_points = np.concatenate(points, axis=0).astype(np.float32)
        if len(all_points) < 3:
            return None
        hull = cv.convexHull(all_points)
        filled: np.ndarray = np.zeros(frame.shape[:2], dtype=np.uint8)
        cv.fillConvexPoly(filled, np.round(hull).astype(np.int32), (1,))
        return filled.astype(bool)

    def _capture_flatfield_frames(self, cam: Camera, count: int) -> Tuple[np.ndarray, ...]:
        """Read up to *count* **raw** frames from *cam* (``Camera.read_raw``
        -- the live flat-field map is deliberately bypassed, see the inline
        comment), skipping any individual read that returns ``None`` (a
        transient capture miss) rather than failing the whole burst --
        otherwise mirrors :meth:`_capture_calibration_frames`; ``FlatFieldCapturer.build`` is what decides whether the
        frames actually collected are enough (it raises ``ValueError`` on
        an empty burst)."""
        frames: List[np.ndarray] = []
        for _ in range(count):
            # read_raw, never read(): the burst is the *reference* a new
            # gain map is built from, so it must not have the currently
            # loaded map already multiplied in (a recapture would otherwise
            # persist a map of the residual only and lose the correction).
            frame = cam.read_raw()
            if frame is not None:
                frames.append(frame)
        return tuple(frames)

    def _flatfield_info_path_for(self, slug: str) -> Path:
        """``<state_dir>/cameras/<slug>/flatfield.json`` -- the metadata
        sidecar next to ``_flatfield_path_for``'s ``flatfield.npy``."""
        return self._directories.state_dir / "cameras" / slug / FLATFIELD_INFO_FILENAME

    def _persist_flatfield(self, slug: str, result: FlatFieldResult) -> None:
        """Atomically write *result*'s gain array to ``flatfield.npy`` and
        its derived :class:`FlatFieldInfo` to ``flatfield.json`` (tmp file,
        ``fsync``, ``os.replace`` -- the identical pattern
        :meth:`_persist_calibration` uses), update ``self._flatfields``,
        then reload the live-applied map on *slug*'s ``Camera`` when this
        daemon currently holds it open.

        Reload uses :meth:`Camera._load_flatfield` directly -- the exact
        mechanism ``camera/camera.py``'s own module docstring already
        documents as how a replaced/deleted map takes effect live (via
        ``Camera.reset()``); calling it here instead of going through a
        full ``reset()`` avoids also re-pushing every other camera
        setting just to pick up a new gain map.
        """
        assert result.gain is not None, (
            "DaemonService._persist_flatfield called with a refused "
            "(gain=None) FlatFieldResult -- a capture_flat_field bug"
        )
        gain = result.gain
        npy_path = self._flatfield_path_for(slug)
        info = FlatFieldInfo(
            present=True,
            captured_at=time.time(),
            shape=tuple(int(x) for x in gain.shape),
            gain_min=float(gain.min()),
            gain_max=float(gain.max()),
            stale=False,
        )

        try:
            npy_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"cannot create flatfield directory {npy_path.parent}: {exc}") from exc

        tmp_npy = npy_path.with_suffix(npy_path.suffix + ".tmp")
        try:
            with open(tmp_npy, "wb") as fh:
                np.save(fh, gain)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_npy, npy_path)
        except OSError as exc:
            raise OSError(f"cannot write flatfield file {npy_path}: {exc}") from exc
        finally:
            try:
                if tmp_npy.exists():
                    tmp_npy.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

        self._persist_flatfield_info(slug, info)

        with self._lock:
            self._flatfields[slug] = info
            cam = self._cameras.get(slug)
        if cam is not None:
            cam._load_flatfield()

    def _persist_flatfield_info(self, slug: str, info: FlatFieldInfo) -> None:
        """Atomically write *info* to *slug*'s ``flatfield.json`` -- the
        same tmp+fsync+``os.replace`` pattern :meth:`_persist_calibration`
        uses, factored out so :meth:`_mark_flatfield_stale` (which only
        ever changes the ``stale`` bit, never the gain array itself) can
        rewrite the sidecar alone without touching ``flatfield.npy``."""
        path = self._flatfield_info_path_for(slug)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"cannot create flatfield directory {path.parent}: {exc}") from exc

        text = json.dumps(_flatfield_info_to_dict(info), indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise OSError(f"cannot write flatfield info file {path}: {exc}") from exc
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

    def _mark_flatfield_stale(self, slug: str) -> None:
        """Flip *slug*'s currently-held flat-field info's ``stale`` bit,
        if it has one (sprint 037) -- the sibling of :meth:`_mark_stale`,
        sharing both of that method's trigger points
        (:meth:`_on_camera_reopen`, a resolution-changing
        :meth:`set_camera_config` push) rather than inventing new ones.
        A no-op for a slug with no flat-field entry (nothing built yet --
        nothing to flag) or one already marked stale (idempotent, avoids
        a redundant disk write on repeated triggers). Unlike
        ``calibration.stale``, this never withholds the map from
        :meth:`~aprilcam.camera.camera.Camera._apply_flatfield` -- purely
        advisory, surfaced through :meth:`get_flatfield`/:meth:`list_cameras`.
        """
        with self._lock:
            info = self._flatfields.get(slug)
            if info is None or info.stale:
                return
            updated = replace(info, stale=True)
            self._flatfields[slug] = updated
        self._persist_flatfield_info(slug, updated)

    def _load_flatfields(self) -> None:
        """Reload every camera's flat-field state at construction time
        (sprint 037), mirroring :meth:`_load_calibrations` exactly:
        scans ``<state_dir>/cameras/*/`` directly, tolerates a missing
        ``state_dir`` or a corrupt sidecar by skipping/degrading rather
        than failing daemon construction.

        Only a directory whose ``flatfield.npy`` actually exists gets an
        entry -- the file is the source of truth for "present," never the
        sidecar alone. A present ``flatfield.npy`` with a missing or
        corrupt ``flatfield.json`` still gets an entry, just with no
        metadata beyond ``present=True`` -- the acceptance criterion for
        a pre-sprint-037 map installed via the manual recipe: ``present``
        is real, ``captured_at`` stays ``None`` rather than a fabricated
        timestamp.
        """
        cameras_dir = self._directories.state_dir / "cameras"
        if not cameras_dir.is_dir():
            return
        for entry in sorted(cameras_dir.iterdir()):
            if not entry.is_dir():
                continue
            npy_path = entry / FLATFIELD_FILENAME
            if not npy_path.exists():
                continue
            json_path = entry / FLATFIELD_INFO_FILENAME
            info = FlatFieldInfo(present=True)
            if json_path.exists():
                try:
                    data = json.loads(json_path.read_text(encoding="utf-8"))
                    info = _flatfield_info_from_dict(data)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "corrupt flatfield info for %s at %s: %s -- treating as "
                        "present with no metadata",
                        entry.name,
                        json_path,
                        exc,
                    )
                    info = FlatFieldInfo(present=True)
            self._flatfields[entry.name] = info

    # -- beyond the interface: shutdown cleanup ----------------------------

    def close(self) -> None:
        """Stop every tracked ``TagStreamProducer``/``ImageStreamProducer``,
        then close every held-open camera. Not part of the ``Daemon``
        base class -- ``daemon/lifecycle.py``'s ``on_stop`` hook calls
        this during a clean shutdown so no backend handle, and no
        background stream thread, outlives the process that opened it
        (sprint.md Migration Concerns, "Clean daemon shutdown" -- ticket
        027-003's own acceptance criterion, extended to image producers
        by ticket 027-004's identical one).

        **Producers stop first, cameras close second, deliberately.**
        Both producer classes' ``stop()`` join their background thread
        with a bounded timeout before returning -- stopping every
        producer before any camera is closed guarantees no producer
        thread is still mid-``Camera.read()`` when that camera's
        backend handle is torn down. Both are already safe, idempotent
        no-ops for a producer with no live thread (an idle one), so this
        loop does not need to distinguish active from idle producers
        first.
        """
        self.stop_control_watchdog()
        self._intrinsics.abort_all()
        with self._lock:
            tag_producers = list(self._tag_stream_producers.values())
            self._tag_stream_producers.clear()
            image_producers = list(self._image_stream_producers.values())
            self._image_stream_producers.clear()
            held = list(self._cameras.values())
            self._cameras.clear()
        for producer in tag_producers:
            try:
                producer.stop()
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "error stopping tag stream producer %s during shutdown", producer.slug
                )
        for image_producer in image_producers:
            try:
                image_producer.stop()
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "error stopping image stream producer %s (deskewed=%s) during shutdown",
                    image_producer.slug,
                    image_producer.deskewed,
                )
        for cam in held:
            try:
                cam.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("error closing camera %s during shutdown", cam.slug)

    # -- internals -----------------------------------------------------

    def _camera_for(self, token: str) -> Camera:
        """The held-open ``Camera`` addressed by *token*, opening it on
        first use.

        *token* is either form ``camera list``/``camera probe`` print
        (ticket 021-002, SUC-005): an **all-digits token resolves by
        persistent registry number** (``CameraRegistry.get_by_number``);
        anything else resolves by slug, exactly as before
        (``CameraRegistry.get``). No ambiguity is possible between the
        two forms -- ``camera/identity.py``'s ``device_name_slug``
        guarantees every slug contains at least one non-digit character,
        so an all-digits token can never *be* a real slug.

        Raises :class:`UnknownCamera` (naming every known camera as
        ``"{number} {slug}"`` pairs, teaching the addressing scheme
        rather than just rejecting) for a token -- number or slug -- the
        registry has never seen. Raises :class:`CameraNotPresent`
        (naming the resolved slug) for a token that *does* resolve to a
        registry record but cannot currently be located among enumerated
        backends (unplugged, or a transient enumeration miss) -- a
        registry record outliving its hardware is normal (UC-25), so
        this is distinct from "never registered," not folded into
        :class:`UnknownCamera` the way it was before this ticket.

        **Backend selection (ticket 031-006, SUC-006).** The freshly
        opened camera's :class:`~aprilcam.camera.backends.CaptureBackend`
        is chosen by :func:`~aprilcam.camera.backends.resolve_capture_backend`
        against *record*'s ``hardware_key``, not unconditionally
        ``self._backend_factory(index)`` as before this ticket: a
        libcamera-identified camera (``hardware_key`` prefixed
        ``"libcamera:"``) opens through
        :class:`~aprilcam.camera.backends.v4l2_loopback.V4l2LoopbackBackend`
        instead, since ``OpenCVBackend`` cannot read a v4l2loopback device
        reliably (that class's own module docstring). Every other
        identity source is unaffected.

        **Identity-safe reopen (sprint 039, corrected).** When the
        backend :func:`~aprilcam.camera.backends.resolve_capture_backend`
        returns is an :class:`~aprilcam.camera.backends.opencv_backend.
        OpenCVBackend` *or* a :class:`~aprilcam.camera.backends.
        worker_backend.WorkerBackend` -- see ``camera-DESIGN.md``'s
        "Identity-safe reopen" section -- it is rebuilt with its
        ``index_resolver``/``expected_hardware_key`` wired: a closure
        over *this exact call's* ``record`` that re-invokes
        :meth:`_index_for` fresh every time it is called, plus
        ``record.hardware_key`` for the post-open identity check (both
        backends verify it the same way -- see
        :meth:`~aprilcam.camera.backends.worker_backend.WorkerBackend.
        _can_verify_identity`). This ticket's own file originally scoped
        ``WorkerBackend`` out as "gated off by default" -- wrong:
        :func:`~aprilcam.camera.backends.default_capture_backend_factory`
        selects ``WorkerBackend`` *unconditionally* on macOS, which is
        exactly the backend the source incident hit (a respawn at a
        shifted index served a different physical camera's frames), so
        both branches are needed, not just the ``OpenCVBackend`` one.
        Rebuilding (rather than mutating the instance
        ``resolve_capture_backend`` returned) is safe and cheap --
        constructing either backend performs no I/O, the spawn/open only
        happens in :meth:`Camera.open` below -- and this is what makes
        *every later* self-heal/stall reopen of this ``Camera``
        (``Camera._attempt_self_heal``/``check_stall``, which only ever
        call ``close()``/``open()`` on this one backend object) re-resolve
        the index fresh too, not just this first open: the resolver
        closure is called again inside the backend's own ``open()``,
        every time it runs. Reuses :meth:`_index_for` -- the same helper
        that already resolved *index* just above -- rather than a second
        implementation of "find the current index for this hardware
        key". A camera resolved to any other backend
        (``V4l2LoopbackBackend``, or a test's injected fake) is untouched
        by this branch -- ``camera/discovery.py``'s ``_probe_openable``
        needs no equivalent wiring, since it constructs a fresh backend
        (and a fresh, discarded ``Camera``) on every single probe call
        rather than holding one open across a reopen -- there is no
        persistent object for a stale index to survive in.

        **Apply-on-open (ticket 023-003).** A freshly-opened camera
        checks :attr:`_camera_configs` for a config already held under
        its slug -- populated either by a live :meth:`set_camera_config`
        push that arrived before this camera did, or by
        ``daemon.__main__.build()``'s boot-time load from ``config_dir``
        -- and applies it via :meth:`Camera.apply_config` *before* the
        camera is cached into :attr:`_cameras` and handed back to the
        caller. This is what makes a config installed on disk (or
        pushed while the camera was absent) take effect the instant the
        camera is actually opened, not only on a subsequent
        :meth:`set_camera_config` call -- closing UC-01.S-4's
        previously-broken playfield-link half. An ``UnsupportedSetting``
        from a stored-but-now-invalid config propagates straight through,
        exactly like a live push would; :attr:`_cameras` is only updated
        after this succeeds, so a camera that fails to apply its stored
        config is not left half-registered as held-open.
        """
        with self._lock:
            existing = self._cameras.get(token)
        if existing is not None:
            return existing

        record = self._record_for(token)
        if record is None:
            raise UnknownCamera(token, self._available())

        with self._lock:
            existing = self._cameras.get(record.slug)
        if existing is not None:
            return existing

        index = self._index_for(record)
        if index is None:
            raise CameraNotPresent(record.slug)

        capture_backend = resolve_capture_backend(
            record.hardware_key,
            index,
            backend_factory=self._backend_factory,
            list_loopback_cameras=self._list_loopback_cameras,
        )
        if isinstance(capture_backend, OpenCVBackend):
            # Sprint 039 identity-safe reopen -- see this method's own
            # docstring's "Identity-safe reopen" section for why this is
            # a rebuild rather than a mutation, and why closing over
            # `record` here is what makes every later self-heal/stall
            # reopen of this Camera re-resolve fresh too.
            capture_backend = OpenCVBackend(
                index,
                index_resolver=lambda: self._index_for(record),
                expected_hardware_key=record.hardware_key,
            )
        elif isinstance(capture_backend, WorkerBackend):
            # Sprint 039 ticket 003 (corrected): WorkerBackend is the
            # backend default_capture_backend_factory actually selects
            # on macOS -- the exact backend the source incident hit --
            # so it needs the identical identity-safe-reopen wiring the
            # OpenCVBackend branch above gets, not just OpenCVBackend.
            # Same rebuild-not-mutate reasoning applies: constructing a
            # WorkerBackend does no I/O (the spawn happens in .open(),
            # called below), so discarding the instance
            # resolve_capture_backend returned is cheap, and closing
            # over `record` here is what makes every later self-heal/
            # stall respawn of this Camera (which only ever calls
            # close()/open() on this one backend object) re-resolve the
            # index fresh too, not just this first open.
            capture_backend = WorkerBackend(
                index,
                index_resolver=lambda: self._index_for(record),
                expected_hardware_key=record.hardware_key,
            )

        cam = Camera(
            record.slug,
            capture_backend,
            control_backend=self._control_backend_factory(record.device_name),
            device_name=record.device_name,
            number=record.number,
            config_loader=self._config_loader,
            config_path=self._config_path_for(record.slug),
            on_reopen=lambda: self._on_camera_reopen(record.slug),
            flatfield_path=self._flatfield_path_for(record.slug),
        )
        cam.open()
        with self._lock:
            config = self._camera_configs.get(record.slug)
        if config is not None:
            cam.apply_config(config)
        with self._lock:
            self._cameras[record.slug] = cam
        return cam

    def _config_path_for(self, slug: str) -> Path:
        """``config_dir/cameras/<slug>/config.json`` -- sprint.md
        Decision 3's fixed reset target (``ResetCameraConfig`` always
        means re-reading exactly this location, never whatever was last
        pushed), matching ``configuration.md``'s documented per-camera
        layout and ``config.loader.ConfigLoader.load_config_dir``'s own
        boot-time scan path for the same file. Computed fresh per call
        from :attr:`_directories`, set once at construction -- every
        ``Camera`` :meth:`_camera_for` builds gets this wired in
        (ticket 023-004), whether or not anything is actually installed
        there yet."""
        return self._directories.config_dir / "cameras" / slug / "config.json"

    # -- UVC control drift watchdog (2026-09-03, out of process) -----------

    def reassert_controls(self) -> Dict[str, Tuple[str, ...]]:
        """One watchdog pass: ``Camera.reassert_config`` on every held-open
        camera. Returns ``{slug: drifted_keys}`` for the cameras whose
        hardware had drifted and were re-asserted (empty when all agree).
        A camera whose re-assert raises is logged and skipped -- a
        watchdog must never take the daemon down."""
        with self._lock:
            held = list(self._cameras.items())
        drifted: Dict[str, Tuple[str, ...]] = {}
        for slug, cam in held:
            try:
                keys = cam.reassert_config()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("%s: control re-assert failed: %s", slug, exc)
                continue
            if keys:
                drifted[slug] = keys
        return drifted

    def check_stalled_cameras(self) -> Tuple[str, ...]:
        """One watchdog pass: ``Camera.check_stall()`` on every held-open
        camera (sprint 038 ticket 006). Returns the slugs this pass found
        stalled and attempted to respawn (empty when every held camera's
        heartbeat is healthy, or has no heartbeat concept at all -- an
        ``OpenCVBackend``-backed camera makes :meth:`Camera.check_stall`
        an unconditional no-op, so this pass is fully backward compatible
        with every camera that predates the worker-backend feature gate).

        Same snapshot-under-lock-then-iterate-outside-lock shape as
        :meth:`reassert_controls`, and the same per-camera ``try``/
        ``except`` discipline: one camera's ``check_stall()`` raising an
        unexpected exception is logged and skipped, never allowed to stop
        the pass from reaching every other held-open camera -- a watchdog
        must never take the daemon down (mirrors :meth:`reassert_controls`
        exactly).
        """
        with self._lock:
            held = list(self._cameras.items())
        stalled: List[str] = []
        recovered: List[str] = []
        failed: List[str] = []
        for slug, cam in held:
            try:
                respawned = cam.check_stall()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("%s: stall check failed: %s", slug, exc)
                continue
            if respawned:
                stalled.append(slug)
                # ``check_stall`` returning True means a respawn was
                # *attempted*; whether it worked is what ``usable`` says.
                (recovered if cam.state.usable else failed).append(slug)
        if recovered:
            logger.warning("stall watchdog respawned: %s", ", ".join(recovered))
        if failed:
            logger.error(
                "stall watchdog respawn FAILED (camera left unusable, retried every %.0fs): %s",
                STALLED_CAMERA_RETRY_INTERVAL_S,
                ", ".join(failed),
            )
        return tuple(stalled)

    def _retry_exhausted_cameras(self) -> Tuple[str, ...]:
        """One watchdog pass giving every *exhausted* held-open camera
        (``Camera.state.usable`` stuck ``False`` -- its
        :data:`~aprilcam.camera.camera.MAX_REOPENS` budget spent) a fresh
        close/open retry via :meth:`Camera.retry_if_exhausted`, at most
        once every :data:`STALLED_CAMERA_RETRY_INTERVAL_S` seconds per
        camera (sprint 038 ticket 006 -- see that constant's own
        docstring for why this interval, independent of this thread's own
        tick rate, needs no second thread of its own).

        Same snapshot-under-lock-then-iterate-outside-lock shape and
        per-camera ``try``/``except`` discipline as :meth:`reassert_controls`/
        :meth:`check_stalled_cameras`. A camera that is currently usable
        is skipped outright (nothing to retry) and its scheduling entry in
        :attr:`_exhausted_retry_at` is cleared, so a camera that recovers
        on its own (a normal demand-driven self-heal, or a prior retry
        from this same pass) does not carry a stale "last attempted" time
        that would otherwise delay a *future* exhaustion's first retry.
        :meth:`Camera.retry_if_exhausted` itself is what decides whether
        there is actually anything exhausted to retry -- this method adds
        no second "is this camera exhausted" flag of its own, only the
        per-slug retry-scheduling timestamp the ticket's acceptance
        criteria call out as *not* being that duplicate bookkeeping.

        Returns the slugs whose retry this pass attempted *and* which
        came back usable afterwards (empty when nothing was due for a
        retry, or every attempted retry failed).
        """
        now = time.time()
        with self._lock:
            held = list(self._cameras.items())
        recovered: List[str] = []
        for slug, cam in held:
            if cam.state.usable:
                self._exhausted_retry_at.pop(slug, None)
                continue
            last_attempt = self._exhausted_retry_at.get(slug)
            if last_attempt is not None and now - last_attempt < STALLED_CAMERA_RETRY_INTERVAL_S:
                continue
            self._exhausted_retry_at[slug] = now
            try:
                succeeded = cam.retry_if_exhausted()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("%s: exhausted-camera retry failed: %s", slug, exc)
                continue
            if succeeded:
                recovered.append(slug)
        if recovered:
            logger.info("exhausted-camera retry recovered: %s", ", ".join(recovered))
        return tuple(recovered)

    def start_control_watchdog(self, interval_s: float = CONTROL_READBACK_INTERVAL_S) -> None:
        """Start the background thread that calls :meth:`reassert_controls`,
        :meth:`check_stalled_cameras`, and :meth:`_retry_exhausted_cameras`
        every *interval_s* seconds. Idempotent.

        **One thread, three passes (sprint 038 ticket 006).** ``camera-
        DESIGN.md``'s rejected-alternative: a second thread dedicated to
        stall detection would duplicate this exact "iterate every
        held-open camera on a fixed interval, take a bounded per-camera
        corrective action, never let one camera's failure stop the pass"
        shape for no isolation benefit, since all three passes already
        tolerate one camera's exception independently of the others.
        :meth:`_retry_exhausted_cameras` runs on every tick of *this*
        thread too, but throttles itself internally to
        :data:`STALLED_CAMERA_RETRY_INTERVAL_S` per camera -- see that
        method's docstring.
        """
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(interval_s):
                self.reassert_controls()
                self.check_stalled_cameras()
                self._retry_exhausted_cameras()

        thread = threading.Thread(target=_loop, name="aprilcam-control-watchdog", daemon=True)
        self._watchdog_stop = stop
        self._watchdog_thread = thread
        thread.start()
        logger.info("control watchdog started (every %.0fs)", interval_s)

    def stop_control_watchdog(self) -> None:
        """Stop the watchdog thread, if running. Idempotent."""
        if self._watchdog_stop is not None:
            self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=5.0)
        self._watchdog_thread = None
        self._watchdog_stop = None

    def _flatfield_path_for(self, slug: str) -> Path:
        """``state_dir/cameras/<slug>/flatfield.npy`` -- the optional
        per-camera flat-field gain map ``camera.camera.Camera`` applies
        to every frame it reads (``FLATFIELD_FILENAME``). ``state_dir``,
        not ``config_dir``, and next to ``calibration.json``: like a
        calibration it is a measured, machine-generated artefact tied
        to this camera's current mounting and lighting, not an
        operator-authored setting. Wired into every ``Camera``
        :meth:`_camera_for` builds; the file is optional and its
        absence means no correction."""
        return self._directories.state_dir / "cameras" / slug / FLATFIELD_FILENAME

    def _record_for(self, token: str) -> Optional[CameraRecord]:
        """Resolve *token* against the registry: an all-digits token by
        number, anything else by slug. See :meth:`_camera_for`."""
        if token.isdigit():
            return self._registry.get_by_number(int(token))
        return self._registry.get(token)

    def _resolve_slug_if_known(self, token: str) -> str:
        """*token* (a persistent number or a slug, SUC-005 addressing)
        resolved to its canonical registry slug when the registry
        recognises it -- otherwise *token* itself, unchanged.

        The shared number-or-slug resolution point every
        camera-**config**-addressed method needs (:meth:`get_camera_config`,
        :meth:`calibrate`, :meth:`get_calibration`) -- built on the same
        :meth:`_record_for` :meth:`_camera_for` already uses, so a token
        resolves identically everywhere rather than each method
        re-deciding "is this a number" for itself.

        **Hotfix, ticket 022-003.** ``calibrate``/``get_calibration``/
        ``get_camera_config`` originally looked up ``self._camera_configs``/
        ``self._calibrations`` keyed by the *raw* token -- both stores
        are keyed by slug only (``set_camera_config``'s ``self.
        _camera_configs[config.name] = config``, ``config.name`` always
        a slug; :meth:`_persist_calibration`/:meth:`_load_calibrations`
        likewise). A valid persistent number therefore matched nothing,
        and ``calibrate`` reported the misleading ``NoPlayfieldLinked``
        for a camera that *was* linked, addressed the only way that
        happened not to work.

        Deliberately **soft**: unlike :meth:`_camera_for` (which raises
        :class:`~aprilcam.errors.UnknownCamera` for an unresolvable
        token, appropriate once hardware access follows), this never
        raises. A token the registry has never seen at all is passed
        through unchanged, so a downstream not-found error still names
        exactly what the caller typed and existing "unknown token"
        error shapes are undisturbed -- e.g. :meth:`get_camera_config`'s
        ``available`` lists cameras with a pushed config, not every
        registered camera, and stays that way for a token this method
        cannot resolve either.
        """
        record = self._record_for(token)
        return record.slug if record is not None else token

    def _available(self) -> Tuple[str, ...]:
        """Every known camera, formatted ``"{number} {slug}"`` for
        :class:`UnknownCamera`'s ``available`` list -- falls back to the
        bare slug for a record somehow missing a number (not reachable
        through normal construction, since ``CameraRecord.number`` is a
        required field, but defensive rather than assuming a stored
        record can never be malformed)."""
        return tuple(
            f"{r.number} {r.slug}" if r.number is not None else r.slug
            for r in self._registry.records()
        )

    def _index_for(self, record: CameraRecord) -> Optional[int]:
        """The current OS-probe index for *record*, or ``None`` if it is
        not enumerated right now. Re-resolves fresh every call (like
        ``CameraDiscovery.probe()`` does) rather than caching -- an
        index is only ever used once, immediately, to open a backend
        (it is explicitly unstable across replugs)."""
        for index, identity in self._identity_source().items():
            if identity.hardware_key == record.hardware_key:
                return index
        return None
