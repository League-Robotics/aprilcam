"""Per-camera ring buffer plus one-burst-per-call detection orchestration.

The daemon-internal home for **when** detection runs and **what** it
remembers -- not vision logic itself (that stays in ``vision/pipeline.py``
and ``vision/detector.py``, both stateless and camera-agnostic). This
module is the first thing in the codebase that imports both ``camera/``
and ``vision/`` (sprint 021 architecture's "first time anything imports
[``vision/``]").

**Demand-driven, no background thread this sprint** (sprint.md
Architecture Decision 1). :meth:`DetectionEngine.run_burst` is called
directly by ``daemon.service.DaemonService.get_tags``/``get_tag`` --
once per call, synchronously -- and nothing else in this module ever
calls it. There is no loop, no timer, no thread: an idle daemon runs
zero detections by construction, not because a scheduler decided not to
run one. :meth:`DetectionEngine.history` and :meth:`DetectionEngine.latest`
are pure reads of already-buffered state and never call :meth:`run_burst`.

**One ``TagDetector`` per camera, reused across bursts.** Ticket 003's
handoff is explicit: building a ``TagDetector``'s per-family
``ArucoDetector`` objects is not free, so this engine builds at most one
per camera slug (lazily, on that camera's first burst) and reuses it for
every subsequent burst against that camera -- never one per call.

**Corner reordering happens here, and turns out to be a no-op.**
``vision/detector.py``'s module docstring records the empirically
verified finding (sprint.md Open Question 2): OpenCV's native corner
order for both AprilTag 36h11 and ArUco 4x4 is *already* identical,
index for index, to ``design.md`` Sec.6's boundary/hat order
(front-left, front-right, back-right, back-left) under the
canonical-print-orientation mounting convention. ``_pixel_corners``
below therefore carries ``Detection.corners`` straight into
``TagRecord.pixel_corners`` with no permutation -- the "reordering" this
module is responsible for is verified to require zero data movement,
not skipped by oversight.

**No ``-(N+1)`` encoding, anywhere.** ``TagId`` is built directly from
``Detection.family`` (already a typed ``TagFamily``) and
``Detection.number`` (already the family's native non-negative
dictionary index) -- sprint.md Architecture Decision 2. No code in this
module ever constructs or interprets a negative tag id.

**World coordinates, when a calibration exists (ticket 022-004).**
:meth:`DetectionEngine.run_burst` asks its injected
``calibration_lookup`` for *slug*'s current
``calibration.interfaces.Calibration`` on every burst -- never cached
across bursts, so a camera that becomes calibrated (or whose
calibration goes stale) mid-session is reflected on the very next call,
not just the next process restart. When one exists, :func:`_build_tag_record`
converts ``pixel``/``pixel_corners`` through ``Calibration.to_world``
to populate ``world``/``world_corners`` (preserving corner order --
never independently recomputed), derives ``yaw_rad`` from the observed
front edge (``world_corners[0]`` -> ``world_corners[1]``, the same
``atan2``, 0=east, CCW-positive convention as heading), and sets
``calibrated=True``. :meth:`run_burst` then hands this tag's
world-position history -- the ring buffer's already-buffered entries for
this exact tag, plus this burst's own new sample -- to
``vision.motion.compute_motion`` (sprint 021, inert until now) to fill in
``heading_rad``/``world_velocity``/``speed``, which stay ``None`` until a
second calibrated sample exists for that tag. **An uncalibrated camera's
path is completely untouched**: ``calibration_lookup`` returning ``None``
(the default, and every case before ticket 003's calibration engine
existed) reproduces sprint 021's output byte-for-byte -- ``world``,
``heading_rad``, ``world_velocity``, ``speed``, ``yaw_rad``, and
``world_corners`` all ``None``, ``calibrated=False``, only
``pixel``/``pixel_corners`` populated. This is a conditional branch
added around sprint 021's code, not a rewrite of it.

**Accepted scope boundary: the ring buffer is query-driven, not
wall-clock-bounded, this sprint** (sprint.md Open Question 3). Because a
buffer only grows when :meth:`run_burst` is called, and nothing calls it
except a live ``get_tags``/``get_tag`` request, "roughly 10 seconds"
(UC-26) is not a wall-clock guarantee here -- a camera queried once a
minute has a buffer that spans 300 minutes, not 300 frames' worth of
seconds, before it fills. This is an accepted, honest consequence of
shipping burst detection before streaming (Decision 1), not a defect;
revisit once a background/streaming producer can genuinely fill the
buffer at frame rate.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import replace
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

from aprilcam.calibration.interfaces import Calibration
from aprilcam.camera.interfaces import Camera
from aprilcam.types import PixelPoint, TagFrame, TagId, TagRecord, WorldPoint
from aprilcam.vision.detector import Detection, TagDetector
from aprilcam.vision.motion import compute_motion
from aprilcam.vision.pipeline import PipelineConfig, run_pipeline

__all__ = ["RING_BUFFER_SIZE", "DetectionEngine"]

#: v1's ``RingBuffer(maxlen=300)`` sizing (``archive/src/aprilcam/core/
#: detection.py``) -- roughly 10 seconds at 30 fps *when something fills
#: it at frame rate*. See the module docstring's scope-boundary note:
#: this sprint's buffer only grows on demand, so that "~10 seconds"
#: framing is v1's, not a guarantee this sprint makes.
RING_BUFFER_SIZE = 300

#: A callable that builds one ``TagDetector`` (or, in tests, a
#: duck-typed substitute exposing the same ``detect(gray, scale=...)``
#: method -- see ``tests/aprilcam/daemon/test_detection.py``'s
#: burst-counting doubles). Injectable so tests can pin exactly how many
#: times detection actually runs without needing real pixel data.
DetectorFactory = Callable[[], TagDetector]

#: A callable that resolves a camera slug -> its currently-held
#: ``Calibration``, or ``None`` if that camera has never been
#: calibrated -- ``daemon.service.DaemonService.get_calibration`` in
#: production (module docstring's "threaded through" section), a fixture
#: lookup or a plain fake in tests. Called fresh on every
#: :meth:`DetectionEngine.run_burst`, never cached, so a newly-completed
#: calibration takes effect on the very next burst.
CalibrationLookup = Callable[[str], Optional[Calibration]]


def _default_detector_factory() -> TagDetector:
    return TagDetector()


def _default_calibration_lookup(slug: str) -> Optional[Calibration]:
    """No calibration subsystem wired in -- every camera is uncalibrated.

    The default for both :class:`DetectionEngine`'s own constructor and
    every pre-ticket-004 test/caller that never passed
    ``calibration_lookup`` at all: reproduces sprint 021's
    always-uncalibrated behaviour exactly (module docstring's regression
    guarantee).
    """
    return None


def _pixel_corners(
    corners: np.ndarray,
) -> Tuple[PixelPoint, PixelPoint, PixelPoint, PixelPoint]:
    """``Detection.corners`` (an ``(4, 2)`` array, native detector order)
    to ``TagRecord.pixel_corners`` -- verified equal to the boundary/hat
    order already (module docstring), so this is a plain type
    conversion, not a permutation."""
    pts = [PixelPoint(x=float(x), y=float(y)) for x, y in corners]
    return (pts[0], pts[1], pts[2], pts[3])


def _front_edge_yaw(
    world_corners: Tuple[WorldPoint, WorldPoint, WorldPoint, WorldPoint]
) -> float:
    """``yaw_rad`` from the observed front edge, in world space.

    ``design.md`` Sec.6 / ``types.TagRecord``'s corner-order contract:
    ``world_corners[0]`` is front-left, ``world_corners[1]`` is
    front-right, and the ``0 -> 1`` edge is the front edge the hat sits
    on. ``atan2`` over that edge vector, 0=east CCW-positive -- the same
    numeric convention ``vision.motion.compute_motion`` uses for
    ``heading_rad`` (``types.py``'s module docstring), applied here to
    orientation rather than motion.
    """
    front_left, front_right = world_corners[0], world_corners[1]
    return math.atan2(front_right.y - front_left.y, front_right.x - front_left.x)


def _build_tag_record(
    detection: Detection, *, timestamp: float, calibration: Optional[Calibration]
) -> TagRecord:
    """One raw ``Detection`` -> one ``TagRecord``.

    *calibration* ``None`` reproduces sprint 021's honestly-uncalibrated
    output exactly: ``world``/``heading_rad``/``world_velocity``/
    ``speed``/``yaw_rad``/``world_corners`` all ``None``, ``calibrated``
    ``False``, only ``pixel``/``pixel_corners`` populated.

    A real *calibration* populates ``world`` (via ``Calibration.to_world``
    on the detection's centre), ``world_corners`` (the same conversion
    applied to each of the four ``pixel_corners`` in order -- never
    independently recomputed), and ``yaw_rad`` (:func:`_front_edge_yaw`
    on those ``world_corners``), and sets ``calibrated=True``.
    ``heading_rad``/``world_velocity``/``speed`` are left ``None`` here
    regardless -- :meth:`DetectionEngine.run_burst` fills those in once
    it has assembled this tag's world-position history, which this
    module-level function has no access to (it builds one record from
    one detection, not a tag's history across bursts).
    """
    tag = TagId(family=detection.family, number=detection.number)
    pixel_corners = _pixel_corners(detection.corners)

    if calibration is None:
        return TagRecord(
            tag=tag,
            world=None,
            heading_rad=None,
            world_velocity=None,
            speed=None,
            timestamp=timestamp,
            pixel=detection.center,
            calibrated=False,
            yaw_rad=None,
            world_corners=None,
            pixel_corners=pixel_corners,
        )

    world = calibration.to_world(detection.center)
    world_corners = (
        calibration.to_world(pixel_corners[0]),
        calibration.to_world(pixel_corners[1]),
        calibration.to_world(pixel_corners[2]),
        calibration.to_world(pixel_corners[3]),
    )
    return TagRecord(
        tag=tag,
        world=world,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=timestamp,
        pixel=detection.center,
        calibrated=True,
        yaw_rad=_front_edge_yaw(world_corners),
        world_corners=world_corners,
        pixel_corners=pixel_corners,
    )


class DetectionEngine:
    """Owns every camera's ring buffer and runs one-shot detection bursts on demand.

    Keyed throughout by camera **slug** (``Camera.slug``), matching the
    key ``daemon.service.DaemonService`` already uses for its held-open
    camera cache -- callers pass the slug of an already-resolved,
    already-open ``Camera``; resolving *which* camera is
    ``DaemonService._camera_for``'s job, not this class's.

    Parameters
    ----------
    buffer_size:
        Per-camera ring buffer capacity. Defaults to
        :data:`RING_BUFFER_SIZE` (v1's sizing).
    pipeline_config:
        Passed straight through to ``vision.pipeline.run_pipeline`` on
        every burst. ``None`` uses that function's own default
        (``PipelineConfig()``).
    detector_factory:
        Builds one detector per camera, lazily, on that camera's first
        burst (see the module docstring's "one ``TagDetector`` per
        camera" section). Overridable so tests can substitute a
        call-counting double without needing real pixel data or a real
        ``cv2.aruco`` build.
    calibration_lookup:
        Resolves a camera slug -> its currently-held ``Calibration``, or
        ``None`` if uncalibrated (module docstring's "world coordinates,
        when a calibration exists" section). Defaults to
        :func:`_default_calibration_lookup` (always ``None``), which is
        exactly sprint 021's behaviour -- every existing caller that
        constructs a ``DetectionEngine`` without this parameter keeps
        producing honestly-uncalibrated records. ``daemon.service.
        DaemonService`` wires its own ``get_calibration`` in here for a
        real daemon process.
    """

    def __init__(
        self,
        *,
        buffer_size: int = RING_BUFFER_SIZE,
        pipeline_config: Optional[PipelineConfig] = None,
        detector_factory: DetectorFactory = _default_detector_factory,
        calibration_lookup: CalibrationLookup = _default_calibration_lookup,
    ) -> None:
        self._buffer_size = buffer_size
        self._pipeline_config = pipeline_config
        self._detector_factory = detector_factory
        self._calibration_lookup = calibration_lookup
        self._buffers: Dict[str, Deque[TagFrame]] = {}
        self._detectors: Dict[str, TagDetector] = {}
        self._frame_counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def run_burst(self, slug: str, camera: Camera) -> TagFrame:
        """Run **exactly one** detection burst against *camera* and append it to *slug*'s buffer.

        The full pipeline: read one frame, run
        ``vision.pipeline.run_pipeline``, run *slug*'s (lazily built,
        reused) ``TagDetector``, build one ``TagRecord`` per hit, wrap
        into a ``TagFrame``, append to the ring buffer, and return that
        same ``TagFrame`` -- the one ``get_tags``/``get_tag`` need.

        Raises ``RuntimeError`` if the camera yields no frame this call
        (mirrors ``DaemonService.get_frame``'s existing "no frame
        available" contract exactly, for the same reason: a capture
        failure is not "zero tags detected" and must not be recorded as
        one -- no buffer entry is appended when this raises).

        When ``calibration_lookup(slug)`` returns a real ``Calibration``
        (module docstring), each hit's ``world``/``world_corners``/
        ``yaw_rad``/``calibrated`` are populated by :func:`_build_tag_record`,
        and this method then assembles that tag's world-position history
        -- every already-buffered entry for the same ``TagId`` with a
        non-``None`` ``world``, oldest to newest, plus this burst's own
        new sample last -- and hands it to
        ``vision.motion.compute_motion`` to fill in
        ``heading_rad``/``world_velocity``/``speed``. A tag with no prior
        calibrated history (its first calibrated sighting) gets a
        one-entry history, so ``compute_motion`` correctly reports all
        three as ``None`` rather than a fabricated zero (its own
        "insufficient history" contract, sprint 021). An uncalibrated
        camera skips this step entirely: ``record.world is None`` short-
        circuits before any history is gathered or ``compute_motion`` is
        ever called, leaving sprint 021's path untouched.
        """
        frame = camera.read()
        if frame is None:
            raise RuntimeError(f"no frame available from camera {slug!r}")

        gray, rescale = run_pipeline(frame, self._pipeline_config)
        detector = self._detector_for(slug)
        detections = detector.detect(gray, scale=rescale.scale)

        timestamp = time.time()
        calibration = self._calibration_lookup(slug)
        records = tuple(
            self._finish_record(
                _build_tag_record(detection, timestamp=timestamp, calibration=calibration),
                slug=slug,
            )
            for detection in detections
        )
        tag_frame = TagFrame(
            tags=records,
            timestamp=timestamp,
            frame_index=self._next_frame_index(slug),
        )
        self._append(slug, tag_frame)
        return tag_frame

    def _finish_record(self, record: TagRecord, *, slug: str) -> TagRecord:
        """Fill in *record*'s motion fields from its tag's world-position
        history, when it has one (``run_burst``'s docstring).

        A no-op (returns *record* unchanged) for an uncalibrated hit --
        ``record.world is None`` is exactly ``_build_tag_record``'s
        signal that no calibration applied, and motion is meaningless
        without world positions.
        """
        if record.world is None:
            return record

        history = self._history_for_tag(slug, record.tag)
        history.append((record.timestamp, record.world))
        velocity, speed, heading_rad = compute_motion(history)
        return replace(record, heading_rad=heading_rad, world_velocity=velocity, speed=speed)

    def _history_for_tag(self, slug: str, tag: TagId) -> List[Tuple[float, WorldPoint]]:
        """*tag*'s already-buffered ``(timestamp, world)`` samples for
        *slug*, oldest to newest -- the ring buffer as it stands
        **before** this burst's new ``TagFrame`` is appended (``_append``
        runs after every record in the new burst is finished). Only
        entries with a non-``None`` ``world`` count: a buffered frame
        from before this camera was calibrated (or a frame where this
        tag was uncalibrated for some other reason) cannot contribute a
        world position to a finite-difference velocity.
        """
        with self._lock:
            buf = self._buffers.get(slug)
            frames = tuple(buf) if buf is not None else ()
        return [
            (frame_record.timestamp, frame_record.world)
            for frame in frames
            for frame_record in frame.tags
            if frame_record.tag == tag and frame_record.world is not None
        ]

    def history(self, slug: str) -> Tuple[TagFrame, ...]:
        """Every buffered ``TagFrame`` for *slug*, oldest to newest.

        A pure read -- never calls :meth:`run_burst`. A camera with no
        buffer yet (never had a burst run against it) returns an empty
        tuple, not an error -- UC-26's "buffer not yet full" failure
        mode, degenerate case n=0.
        """
        with self._lock:
            buf = self._buffers.get(slug)
            return tuple(buf) if buf is not None else ()

    def latest(self, slug: str) -> Optional[TagFrame]:
        """The most recent buffered ``TagFrame`` for *slug*, or ``None``
        if its buffer is empty or does not exist yet.

        Used by ``DaemonService.get_frame`` to embed the latest
        detection result without itself triggering a burst (sprint.md
        Decision 4) -- the ``None``/populated distinction here is
        exactly ``ImageFrame.tags``'s "``None`` means detection is off"
        contract.
        """
        with self._lock:
            buf = self._buffers.get(slug)
            if not buf:
                return None
            return buf[-1]

    # -- internals -------------------------------------------------------

    def _detector_for(self, slug: str) -> TagDetector:
        with self._lock:
            detector = self._detectors.get(slug)
            if detector is None:
                detector = self._detector_factory()
                self._detectors[slug] = detector
            return detector

    def _append(self, slug: str, tag_frame: TagFrame) -> None:
        with self._lock:
            buf = self._buffers.get(slug)
            if buf is None:
                buf = deque(maxlen=self._buffer_size)
                self._buffers[slug] = buf
            buf.append(tag_frame)

    def _next_frame_index(self, slug: str) -> int:
        with self._lock:
            index = self._frame_counts.get(slug, 0)
            self._frame_counts[slug] = index + 1
            return index
