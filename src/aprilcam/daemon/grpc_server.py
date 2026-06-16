"""
aprilcam.daemon.grpc_server — AprilCamServicer and make_grpc_server().

``AprilCamServicer`` implements all 10 RPC methods of the ``AprilCam`` gRPC
service (defined in aprilcam.proto).  It is a thin delegation layer:
all camera operations are forwarded to the ``DaemonServer`` helper methods
through the shared camera registry dict and config reference.

``make_grpc_server(transports, servicer)`` builds a ``grpc.Server`` with
the servicer registered and gRPC Server Reflection enabled.  The returned
server is *not* yet started; the caller (``DaemonServer``) calls
``server.start()`` / ``server.wait_for_termination()``.

Usage inside DaemonServer::

    servicer = AprilCamServicer(cameras=self._cameras,
                                cam_lock=self._cam_lock,
                                config=self._config,
                                shutdown_event=self._shutdown_event)
    grpc_srv = make_grpc_server(transports=[...], servicer=servicer)
    grpc_srv.start()
"""

from __future__ import annotations

import logging
import threading
from concurrent import futures
from typing import TYPE_CHECKING, Dict, List, Optional

import grpc
from grpc_reflection.v1alpha import reflection

from ..proto import aprilcam_pb2, aprilcam_pb2_grpc

if TYPE_CHECKING:
    from ..config import Config
    from .camera_pipeline import CameraPipeline
    from .stream import ImageStreamProducer, TagStreamProducer

log = logging.getLogger(__name__)


class AprilCamServicer(aprilcam_pb2_grpc.AprilCamServicer):
    """Thin gRPC servicer that delegates all camera work to DaemonServer helpers.

    Args:
        cameras:        Shared ``cam_name → CameraPipeline`` dict (owned by
                        ``DaemonServer``).
        cam_lock:       ``threading.Lock`` guarding *cameras*.
        config:         Daemon ``Config`` instance.
        shutdown_event: ``threading.Event`` set by ``Shutdown`` RPC or signal.
    """

    def __init__(
        self,
        cameras: Dict[str, "CameraPipeline"],
        cam_lock: threading.Lock,
        config: "Config",
        shutdown_event: threading.Event,
    ) -> None:
        self._cameras = cameras
        self._cam_lock = cam_lock
        self._config = config
        self._shutdown_event = shutdown_event

        # Per-camera stream producers — created on demand
        self._image_producers: Dict[str, "ImageStreamProducer"] = {}
        self._tag_producers: Dict[str, "TagStreamProducer"] = {}
        self._producer_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Camera lifecycle
    # ------------------------------------------------------------------

    def ListCameras(
        self,
        request: aprilcam_pb2.Empty,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.ListCamerasResponse:
        """Return names of all currently-open cameras."""
        with self._cam_lock:
            cam_names = list(self._cameras.keys())
        return aprilcam_pb2.ListCamerasResponse(cameras=cam_names)

    def _resolve_cam_name(self, index: int) -> str:
        """Resolve an OpenCV ``index`` to its registry per-camera dir key.

        Resolves the device's stable hardware identity and looks it up in the
        persistent :class:`~aprilcam.camera.registry.CameraRegistry` (creating a
        record on first sight, reusing it on reconnect). The registry-assigned
        per-camera ``dir`` is returned as the ``cam_name`` so the daemon's
        per-camera data dir (``cameras_dir / cam_name``) is owned by the
        registry and is stable across unplug/replug and re-enumeration.

        Falls back to the legacy ``device_name_slug`` / ``cam-<index>`` naming
        only if identity resolution or registry persistence fails, so a camera
        can always be opened even when the registry is unavailable.
        """
        from ..camera.camutil import get_device_name
        from ..calibration.calibration import device_name_slug
        from ..camera.identity import resolve_identity
        from ..camera.registry import CameraRegistry

        device_name = get_device_name(index)
        try:
            identity = resolve_identity(index, name=device_name)
            registry = CameraRegistry(self._config.cameras_dir)
            record = registry.resolve(identity)
            if record.dir:
                return record.dir
        except Exception:
            log.exception("OpenCamera: registry resolve failed for index %s", index)

        return device_name_slug(device_name) if device_name else f"cam-{index}"

    def OpenCamera(
        self,
        request: aprilcam_pb2.OpenCameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.OpenCameraResponse:
        """Open a camera by index and return its cam_name."""
        from .camera_pipeline import CameraPipeline

        index = request.index
        cam_name = self._resolve_cam_name(index)

        with self._cam_lock:
            if cam_name in self._cameras:
                camera_dir = str(self._config.cameras_dir / cam_name)
                return aprilcam_pb2.OpenCameraResponse(cam_name=cam_name, camera_dir=camera_dir)

            # Determine detection_fps: calibration.json > config default
            import json as _json
            detection_fps = self._config.detection_fps
            cal_file = self._config.cameras_dir / cam_name / "calibration.json"
            if cal_file.exists():
                try:
                    _cal_data = _json.loads(cal_file.read_text())
                    if "detection_fps" in _cal_data:
                        detection_fps = int(_cal_data["detection_fps"])
                except Exception:
                    pass

            pipeline = CameraPipeline(cam_name, index, self._config, detection_fps=detection_fps)
            try:
                pipeline.start()
            except RuntimeError as exc:
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(str(exc))
                return aprilcam_pb2.OpenCameraResponse()

            self._cameras[cam_name] = pipeline

        camera_dir = str(self._config.cameras_dir / cam_name)
        return aprilcam_pb2.OpenCameraResponse(cam_name=cam_name, camera_dir=camera_dir)

    def CloseCamera(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.Empty:
        """Stop and remove a camera pipeline; stop any associated producers."""
        cam_name = request.cam_name

        # Stop stream producers first so their threads exit cleanly.
        with self._producer_lock:
            ip = self._image_producers.pop(cam_name, None)
            tp = self._tag_producers.pop(cam_name, None)

        if ip is not None:
            try:
                ip.stop()
            except Exception:
                log.exception("Error stopping image producer for %s", cam_name)

        if tp is not None:
            try:
                tp.stop()
            except Exception:
                log.exception("Error stopping tag producer for %s", cam_name)

        with self._cam_lock:
            pipeline = self._cameras.pop(cam_name, None)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.Empty()

        try:
            pipeline.stop()
        except Exception as exc:
            log.exception("Error stopping pipeline %s", cam_name)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))

        return aprilcam_pb2.Empty()

    def ReloadCalibration(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.Empty:
        """Reload homography / calibration from disk for an open camera."""
        cam_name = request.cam_name

        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.Empty()

        from ..calibration.calibration import load_calibration_from_camera_dir
        from .camera_pipeline import _apply_camera_settings

        camera_dir = self._config.cameras_dir / cam_name
        try:
            calibration = load_calibration_from_camera_dir(camera_dir)
        except Exception as exc:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"calibration load failed: {exc}")
            return aprilcam_pb2.Empty()

        if pipeline._april_cam is not None:
            if calibration is not None:
                pipeline._april_cam.homography = calibration.homography
                pipeline._calibration = calibration
                if calibration.settings:
                    _apply_camera_settings(
                        calibration.settings,
                        pipeline.device_name,
                        self._config,
                    )
            else:
                pipeline._april_cam.homography = None
                pipeline._calibration = None

        return aprilcam_pb2.Empty()

    def Shutdown(
        self,
        request: aprilcam_pb2.Empty,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.Empty:
        """Signal the daemon to shut down gracefully."""
        log.info("AprilCamServicer: Shutdown RPC received")
        self._shutdown_event.set()
        return aprilcam_pb2.Empty()

    # ------------------------------------------------------------------
    # One-shot queries
    # ------------------------------------------------------------------

    def GetCameraInfo(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.CameraInfoResponse:
        """Return live camera metadata from the running pipeline."""
        cam_name = request.cam_name

        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.CameraInfoResponse()

        cap = pipeline._cap
        frame_w = int(cap.get(2)) if cap is not None else 0   # cv.CAP_PROP_FRAME_WIDTH
        frame_h = int(cap.get(4)) if cap is not None else 0   # cv.CAP_PROP_FRAME_HEIGHT
        calibrated = pipeline._calibration is not None

        # Rolling FPS from the deque maintained by the pipeline
        fps = 0.0
        ts_deque = getattr(pipeline, "_ts_deque", None)
        if ts_deque is not None and len(ts_deque) >= 2:
            elapsed = ts_deque[-1] - ts_deque[0]
            if elapsed > 0.0:
                fps = (len(ts_deque) - 1) / elapsed

        return aprilcam_pb2.CameraInfoResponse(
            cam_name=cam_name,
            calibrated=calibrated,
            frame_w=frame_w,
            frame_h=frame_h,
            fps=float(fps),
        )

    def CaptureFrame(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.CaptureFrameResponse:
        """Return the most recent JPEG frame from an open camera."""
        cam_name = request.cam_name

        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.CaptureFrameResponse()

        import time as _time
        jpeg = None
        deadline = _time.monotonic() + 3.0
        while jpeg is None and _time.monotonic() < deadline:
            jpeg = pipeline.capture_frame()
            if jpeg is None:
                _time.sleep(0.05)
        if jpeg is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("no frame captured yet")
            return aprilcam_pb2.CaptureFrameResponse()

        return aprilcam_pb2.CaptureFrameResponse(jpeg=jpeg)

    def GetTags(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.TagFrameResponse:
        """Return the latest tag detections from the ring buffer."""
        cam_name = request.cam_name

        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.TagFrameResponse()

        return pipeline.get_current_tags()

    def WhereIs(
        self,
        request: aprilcam_pb2.WhereRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.WhereResponse:
        """Resolve a natural-language "where is X" query against the playfield map.

        Runs a full-text keyword search over the static playfield map (loaded
        from the named-playfields registry, falling back to the legacy
        single-file layout).  When *cam_name* is given and that camera is open,
        live detections are merged into matched tag features.  On a keyword
        miss the full playfield map is returned so the caller can fall back to
        an LLM.
        """
        import json as _json
        from ..core import playfield_query as pq

        try:
            playfield = pq.load_playfield_map(self._config)
        except (FileNotFoundError, ValueError):
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("no playfield is configured")
            return aprilcam_pb2.WhereResponse(status="not_found")

        # Gather live tag positions for the named camera, if any.
        live_tags: dict[int, dict] = {}
        if request.cam_name:
            with self._cam_lock:
                pipeline = self._cameras.get(request.cam_name)
            if pipeline is not None:
                try:
                    tag_frame = pipeline.get_current_tags()
                    for t in tag_frame.tags:
                        live_tags[int(t.id)] = {
                            "world_xy": [float(t.wx), float(t.wy)],
                            "in_playfield": bool(t.in_playfield),
                        }
                except Exception:
                    log.exception("WhereIs: failed to read live tags for %s", request.cam_name)

        result = pq.where(
            request.query, pq.iter_features(playfield), live_tags=live_tags or None
        )

        resp = aprilcam_pb2.WhereResponse(
            status=result["status"], tokens=list(result.get("tokens", []))
        )
        for m in result["matches"]:
            loc = m.get("location")
            live = m.get("live_detection")
            live_xy = (live or {}).get("world_xy") if live else None
            resp.matches.append(
                aprilcam_pb2.WhereMatch(
                    slug=str(m.get("slug") or ""),
                    type=str(m.get("type") or ""),
                    category=str(m.get("category") or ""),
                    has_location=loc is not None,
                    x=float(loc["x"]) if loc else 0.0,
                    y=float(loc["y"]) if loc else 0.0,
                    record_json=_json.dumps(m.get("record", {})),
                    has_live=live is not None,
                    live_x=float(live_xy[0]) if live_xy else 0.0,
                    live_y=float(live_xy[1]) if live_xy else 0.0,
                    in_playfield=bool((live or {}).get("in_playfield", False)),
                )
            )

        if result["status"] == "not_found":
            try:
                resp.playfield_json = _json.dumps(playfield)
            except (TypeError, ValueError):
                pass

        return resp

    # ------------------------------------------------------------------
    # Stream discovery
    # ------------------------------------------------------------------

    def GetImageStream(
        self,
        request: aprilcam_pb2.StreamRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.StreamEndpoint:
        """Ensure an ImageStreamProducer exists for the camera and return its endpoint."""
        cam_name = request.cam_name

        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.StreamEndpoint()

        with self._producer_lock:
            if cam_name not in self._image_producers:
                from .stream import ImageStreamProducer

                producer = ImageStreamProducer(cam_name, self._config)
                endpoint = producer.start()
                self._image_producers[cam_name] = producer

                # Wire producer into the pipeline so it receives frames.
                tp = self._tag_producers.get(cam_name)
                pipeline.set_producers(producer, tp)

                log.info(
                    "AprilCamServicer: image stream started for %s (unix=%s, tcp=%s)",
                    cam_name,
                    endpoint.socket_path,
                    endpoint.tcp_port,
                )
            else:
                producer = self._image_producers[cam_name]
                socket_path = producer.socket_path or ""
                tcp_port = producer.tcp_port or 0
                endpoint_obj = aprilcam_pb2.StreamEndpoint(
                    socket_path=socket_path,
                    tcp_port=tcp_port,
                )
                return endpoint_obj

        # Convert StreamEndpoint (Pydantic) → proto StreamEndpoint
        return aprilcam_pb2.StreamEndpoint(
            socket_path=endpoint.socket_path or "",
            tcp_port=endpoint.tcp_port or 0,
        )

    def GetTagStream(
        self,
        request: aprilcam_pb2.StreamRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.StreamEndpoint:
        """Ensure a TagStreamProducer exists for the camera and return its endpoint."""
        cam_name = request.cam_name
        max_hz = request.max_hz if request.max_hz > 0 else 20

        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.StreamEndpoint()

        with self._producer_lock:
            if cam_name not in self._tag_producers:
                from .stream import TagStreamProducer

                producer = TagStreamProducer(cam_name, self._config, max_hz=float(max_hz))
                endpoint = producer.start()
                self._tag_producers[cam_name] = producer

                # Wire producer into the pipeline so it receives tag frames.
                ip = self._image_producers.get(cam_name)
                pipeline.set_producers(ip, producer)

                log.info(
                    "AprilCamServicer: tag stream started for %s (unix=%s, tcp=%s)",
                    cam_name,
                    endpoint.socket_path,
                    endpoint.tcp_port,
                )
            else:
                producer = self._tag_producers[cam_name]
                socket_path = producer.socket_path or ""
                tcp_port = producer.tcp_port or 0
                endpoint_obj = aprilcam_pb2.StreamEndpoint(
                    socket_path=socket_path,
                    tcp_port=tcp_port,
                )
                return endpoint_obj

        # Convert StreamEndpoint (Pydantic) → proto StreamEndpoint
        return aprilcam_pb2.StreamEndpoint(
            socket_path=endpoint.socket_path or "",
            tcp_port=endpoint.tcp_port or 0,
        )

    def PublishOverlay(
        self,
        request: aprilcam_pb2.PublishOverlayRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.StatusReply:
        """Forward an overlay frame to all tag stream subscribers for a camera."""
        producer = self._tag_producers.get(request.cam_name)
        if producer is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(
                f"Camera '{request.cam_name}' not found or not streaming"
            )
            return aprilcam_pb2.StatusReply(
                ok=False, error=f"Camera '{request.cam_name}' not found"
            )
        producer.publish_overlay(request.overlay)
        return aprilcam_pb2.StatusReply(ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def stop_all_producers(self) -> None:
        """Stop all stream producers — called during daemon shutdown."""
        with self._producer_lock:
            producers = list(self._image_producers.values()) + list(
                self._tag_producers.values()
            )
            self._image_producers.clear()
            self._tag_producers.clear()

        for p in producers:
            try:
                p.stop()
            except Exception:
                log.exception("Error stopping producer during shutdown")


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def make_grpc_server(
    transports: List[str],
    servicer: AprilCamServicer,
    *,
    max_workers: int = 10,
) -> grpc.Server:
    """Build a ``grpc.Server`` with the AprilCam servicer and reflection registered.

    The server is *not* started here.  The caller must call
    ``server.add_insecure_port(addr)`` and then ``server.start()``.

    Args:
        transports:   List of address strings to bind (e.g.
                      ``["unix:///tmp/aprilcam.sock", "[::]:50051"]``).
                      Addresses are added inside this function.
        servicer:     Configured ``AprilCamServicer`` instance.
        max_workers:  Thread-pool size for the gRPC server.

    Returns:
        Configured but not-yet-started ``grpc.Server``.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))

    aprilcam_pb2_grpc.add_AprilCamServicer_to_server(servicer, server)

    # Enable server reflection so grpcurl and similar tools can introspect.
    service_names = (
        aprilcam_pb2.DESCRIPTOR.services_by_name["AprilCam"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)

    for addr in transports:
        server.add_insecure_port(addr)

    return server
