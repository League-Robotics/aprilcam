"""aprilcam.client.control — DaemonControl: typed gRPC stub wrapper.

All RPC methods return Pydantic models from ``aprilcam.client.models``.
Proto-generated types are confined to this module.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import cv2
import grpc

from aprilcam.proto import aprilcam_pb2, aprilcam_pb2_grpc
from aprilcam.client.models import (
    CameraInfo,
    ImageFrame,
    StreamEndpoint,
    TagFrame,
    TagRecord,
)
from aprilcam.client.stream import ImageStreamConsumer, TagStreamConsumer

if TYPE_CHECKING:
    from aprilcam.config import Config


# ---------------------------------------------------------------------------
# DaemonControl
# ---------------------------------------------------------------------------


class DaemonControl:
    """Typed gRPC stub wrapper for the AprilCam daemon.

    Usage::

        with DaemonControl(unix_path="/tmp/aprilcam/control.sock") as dc:
            cameras = dc.list_cameras()

    Constructor keyword arguments:
      - ``unix_path`` — connect via Unix socket if provided (takes precedence).
      - ``host`` — TCP host (default ``"localhost"``).
      - ``port`` — TCP port (default ``5280``).
    """

    def __init__(
        self,
        unix_path: str | None = None,
        host: str = "localhost",
        port: int = 5280,
    ) -> None:
        self._unix_path = unix_path
        self._host = host
        self._port = port
        self._channel: grpc.Channel | None = None
        self._stub: aprilcam_pb2_grpc.AprilCamStub | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @classmethod
    def connect_default(
        cls,
        config: "Config",
        log_level: str | None = None,
        unix_path: str | None = None,
        tcp_port: int | None = None,
    ) -> "DaemonControl":
        """Return a connected DaemonControl, spawning the daemon if needed.

        Mirrors the behaviour of the legacy ``ensure_running()`` function:

        1. Build the gRPC target from *unix_path* / *tcp_port* or the
           defaults derived from *config*.
        2. Attempt an immediate gRPC probe (``ListCameras``).  If it
           succeeds, return the connected instance.
        3. Acquire a spawn lock, re-probe, then spawn
           ``python -m aprilcam.daemon`` as a detached background process.
        4. Poll every 50 ms for up to 5 seconds; raise on timeout.

        *log_level* overrides ``APRILCAM_LOG_LEVEL`` for the spawned process.
        """
        resolved_unix = unix_path or str(config.socket_dir / "control.sock")
        resolved_port = tcp_port  # may be None — only used if unix fails

        def _try_connect() -> "DaemonControl | None":
            dc = cls(unix_path=resolved_unix)
            dc.connect()
            try:
                dc.list_cameras()
                return dc
            except grpc.RpcError:
                dc.close()
                return None
            except Exception:
                dc.close()
                return None

        # Fast path: daemon already running
        result = _try_connect()
        if result is not None:
            return result

        # Spawn lock: prevent two callers from starting the daemon simultaneously
        lock_path = config.socket_dir / "aprilcamd.spawn.lock"
        config.socket_dir.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w")  # noqa: WPS515  (kept open for flock)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            # Re-probe after acquiring the lock (another caller may have spawned)
            result = _try_connect()
            if result is not None:
                return result

            # Spawn the daemon
            config.data_dir.mkdir(parents=True, exist_ok=True)
            log_file = open(config.data_dir / "aprilcamd.log", "a")  # noqa: WPS515
            env = os.environ.copy()
            if log_level:
                env["APRILCAM_LOG_LEVEL"] = log_level
            subprocess.Popen(
                [sys.executable, "-m", "aprilcam.daemon"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                env=env,
            )

            # Poll until the daemon is ready.  OpenCV + gRPC module loading
            # can take 10+ seconds on a cold start, so allow 20 seconds.
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                time.sleep(0.1)
                result = _try_connect()
                if result is not None:
                    return result

            raise RuntimeError("aprilcamd did not start within 20 seconds")

        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def connect(self) -> "DaemonControl":
        """Open the gRPC channel and create the stub.

        Idempotent — calling ``connect()`` on an already-connected instance
        is a no-op.
        """
        if self._channel is not None:
            return self
        if self._unix_path:
            target = f"unix:{self._unix_path}"
        else:
            target = f"{self._host}:{self._port}"
        self._channel = grpc.insecure_channel(target)
        self._stub = aprilcam_pb2_grpc.AprilCamStub(self._channel)
        return self

    def close(self) -> None:
        """Close the gRPC channel."""
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

    def __enter__(self) -> "DaemonControl":
        return self.connect()

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _stub_or_raise(self) -> aprilcam_pb2_grpc.AprilCamStub:
        if self._stub is None:
            raise RuntimeError(
                "DaemonControl is not connected — call connect() first "
                "or use it as a context manager."
            )
        return self._stub

    # ------------------------------------------------------------------
    # RPC methods
    # ------------------------------------------------------------------

    def list_cameras(self) -> list[str]:
        """Return names of all currently open cameras."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.ListCamerasResponse = stub.ListCameras(
            aprilcam_pb2.Empty()
        )
        return list(resp.cameras)

    def open_camera(self, index: int) -> tuple[str, str]:
        """Open camera by device index; return ``(cam_name, camera_dir)``."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.OpenCameraResponse = stub.OpenCamera(
            aprilcam_pb2.OpenCameraRequest(index=index)
        )
        return str(resp.cam_name), str(resp.camera_dir)

    def close_camera(self, cam_name: str) -> None:
        """Close an open camera."""
        stub = self._stub_or_raise()
        stub.CloseCamera(aprilcam_pb2.CameraRequest(cam_name=cam_name))

    def reload_calibration(self, cam_name: str) -> None:
        """Reload calibration data for a camera from disk."""
        stub = self._stub_or_raise()
        stub.ReloadCalibration(aprilcam_pb2.CameraRequest(cam_name=cam_name))

    def get_camera_info(self, cam_name: str) -> CameraInfo:
        """Return metadata for an open camera."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.CameraInfoResponse = stub.GetCameraInfo(
            aprilcam_pb2.CameraRequest(cam_name=cam_name)
        )
        return CameraInfo.from_proto(resp)

    def capture_frame(self, cam_name: str) -> np.ndarray:
        """Capture a single frame; return a BGR ``np.ndarray``."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.CaptureFrameResponse = stub.CaptureFrame(
            aprilcam_pb2.CameraRequest(cam_name=cam_name)
        )
        buf = np.frombuffer(resp.jpeg, dtype=np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError(
                f"Failed to decode JPEG frame from camera '{cam_name}'"
            )
        return frame

    def get_tags(self, cam_name: str) -> TagFrame:
        """Return the most recent tag detections for an open camera."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.TagFrameResponse = stub.GetTags(
            aprilcam_pb2.CameraRequest(cam_name=cam_name)
        )
        return _tag_frame_response_to_pydantic(resp)

    def get_tag(self, cam_name: str, tag_id: int) -> "TagRecord | None":
        """Return a single tag by marker id, or ``None`` if not currently seen.

        Thin convenience wrapper over :meth:`get_tags`: the daemon has no
        per-tag RPC, so this still fetches the latest full frame and selects
        the matching tag. For repeated lookups against the same frame, call
        :meth:`get_tags` once and use :meth:`TagFrame.by_id` instead.
        """
        return self.get_tags(cam_name).by_id(tag_id)

    def where_is(self, query: str, cam_name: str = "") -> dict:
        """Resolve a natural-language "where is X" question via the daemon.

        Runs a keyword search over the static playfield map (playfield.json).
        When *cam_name* is given, live detections for that camera are merged
        into matched tag features.

        Args:
            query: Natural-language question, e.g. ``"where is the blue dot"``.
            cam_name: Optional open camera to merge live tag positions from.

        Returns:
            A dict with ``status`` (``"ok"`` | ``"ambiguous"`` | ``"not_found"``),
            ``tokens`` (the normalised search tokens) and ``matches`` (a list of
            resolved features, each with ``slug``, ``type``, ``location`` and the
            full ``record``).  On ``"not_found"`` a ``playfield`` key holds the
            parsed playfield.json so the caller can resolve the reference itself.
        """
        import json as _json

        stub = self._stub_or_raise()
        resp: aprilcam_pb2.WhereResponse = stub.WhereIs(
            aprilcam_pb2.WhereRequest(query=query, cam_name=cam_name)
        )

        matches = []
        for m in resp.matches:
            entry: dict = {
                "slug": m.slug,
                "type": m.type,
                "category": m.category,
                "location": (
                    {"x": m.x, "y": m.y, "units": "cm", "frame": "a1-centred"}
                    if m.has_location
                    else None
                ),
                "record": _json.loads(m.record_json) if m.record_json else {},
            }
            if m.has_live:
                entry["live_detection"] = {
                    "world_xy": [m.live_x, m.live_y],
                    "in_playfield": m.in_playfield,
                }
            matches.append(entry)

        result: dict = {
            "status": resp.status,
            "query": query,
            "tokens": list(resp.tokens),
            "matches": matches,
        }
        if resp.status == "not_found" and resp.playfield_json:
            try:
                result["playfield"] = _json.loads(resp.playfield_json)
            except _json.JSONDecodeError:
                pass
        return result

    def get_image_stream(
        self, cam_name: str, max_hz: int = 20
    ) -> "ImageStreamConsumer":
        """Request an image stream and return a connected ``ImageStreamConsumer``."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.StreamEndpoint = stub.GetImageStream(
            aprilcam_pb2.StreamRequest(cam_name=cam_name, max_hz=max_hz)
        )
        endpoint = StreamEndpoint.from_proto(resp)
        consumer = ImageStreamConsumer(endpoint, cam_name=cam_name)
        consumer.connect()
        return consumer

    def get_tag_stream(
        self, cam_name: str, max_hz: int = 20
    ) -> "TagStreamConsumer":
        """Request a tag stream and return a connected ``TagStreamConsumer``."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.StreamEndpoint = stub.GetTagStream(
            aprilcam_pb2.StreamRequest(cam_name=cam_name, max_hz=max_hz)
        )
        endpoint = StreamEndpoint.from_proto(resp)
        consumer = TagStreamConsumer(endpoint)
        consumer.connect()
        return consumer

    def publish_overlay(
        self, cam_name: str, elements: list, ttl: float = 1.0
    ) -> bool:
        """Push overlay elements to all tag stream subscribers for this camera.

        Any process with DaemonControl access can call this directly (not only
        via MCP). Useful for robots updating at 5-10 Hz.

        Args:
            cam_name: Camera name returned by open_camera().
            elements: List of dicts with keys: type (str), params (list[float]),
                      color (list[int] RGB), thickness (int, -1=filled).
            ttl: Seconds before the view drops the overlay (default 1.0).

        Returns:
            True if the daemon accepted the overlay, False otherwise.
        """
        stub = self._stub_or_raise()
        overlay_elements = [
            aprilcam_pb2.OverlayElement(
                type=e["type"],
                params=list(e.get("params", [])),
                color=list(e.get("color", [255, 255, 255])),
                thickness=int(e.get("thickness", 2)),
                text=str(e.get("text", "")),
            )
            for e in elements
        ]
        overlay = aprilcam_pb2.OverlayFrame(
            timestamp=time.time(),
            ttl=float(ttl),
            elements=overlay_elements,
            camera_id=cam_name,
        )
        reply = stub.PublishOverlay(
            aprilcam_pb2.PublishOverlayRequest(cam_name=cam_name, overlay=overlay)
        )
        return reply.ok

    def shutdown(self) -> None:
        """Send the Shutdown RPC; the daemon process will exit."""
        stub = self._stub_or_raise()
        stub.Shutdown(aprilcam_pb2.Empty())


# ---------------------------------------------------------------------------
# Private converters
# ---------------------------------------------------------------------------


def _tag_frame_response_to_pydantic(resp: "aprilcam_pb2.TagFrameResponse") -> TagFrame:
    """Convert a ``TagFrameResponse`` proto message to a ``TagFrame`` Pydantic model.

    ``TagFrameResponse`` is the one-shot GetTags variant; it lacks timestamp
    and fps fields so we default those to zero.
    """
    from aprilcam.client.models import TagRecord

    homo_flat: list[float] = list(resp.homography)
    homography: list[list[float]] | None = None
    if len(homo_flat) == 9:
        homography = [
            homo_flat[0:3],
            homo_flat[3:6],
            homo_flat[6:9],
        ]

    corners_flat: list[float] = list(resp.playfield_corners)
    playfield_corners: list[tuple[float, float]] = [
        (corners_flat[i], corners_flat[i + 1])
        for i in range(0, len(corners_flat), 2)
    ]

    return TagFrame(
        frame_id=int(resp.frame_id),
        ts_mono_ns=0,
        ts_wall_ms=0,
        tags=[TagRecord.from_proto(t) for t in resp.tags],
        homography=homography,
        playfield_corners=playfield_corners,
        fps=0.0,
    )
