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

    def EnumerateCameras(
        self,
        request: aprilcam_pb2.Empty,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.EnumerateCamerasResponse:
        """Probe host hardware and return all available camera devices.

        Unlike ``ListCameras`` (which returns currently-open cameras), this
        RPC calls ``camutil.list_cameras()`` to enumerate every device
        detectable on the host.  Clients must call this RPC instead of
        probing hardware locally; only the daemon owns camera hardware.
        """
        from ..camera.camutil import list_cameras
        from ..calibration.calibration import device_name_slug
        from ..camera.identity import resolve_all
        from ..camera.registry import CameraRegistry

        with self._cam_lock:
            # Hold the lock while probing so we don't race with OpenCamera
            # (brief contention; probe is local and fast in quiet mode).
            try:
                cams = list_cameras(
                    max_index=10,
                    quiet=True,
                    detailed_names=True,
                )
            except Exception as exc:
                log.exception("EnumerateCameras: hardware probe failed")
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(f"camera enumeration failed: {exc}")
                return aprilcam_pb2.EnumerateCamerasResponse()

            # Resolve each device's persistent enumeration number from the
            # registry so clients display/accept a STABLE handle. The OS
            # ``index`` changes on plug/unplug; ``enum`` does not. Done under
            # the same lock as the probe so we don't race OpenCamera.
            try:
                identities = resolve_all()
            except Exception:
                log.exception("EnumerateCameras: identity resolution failed")
                identities = {}
            registry = CameraRegistry(self._config.cameras_dir)

        devices = []
        for cam in cams:
            name = cam.device_name or cam.name or f"Camera {cam.index}"
            slug = device_name_slug(name)
            enum_no = 0
            identity = identities.get(cam.index)
            if identity is not None:
                try:
                    record = registry.resolve(identity)
                    enum_no = int(record.enum or 0)
                except Exception:
                    log.exception(
                        "EnumerateCameras: enum resolve failed for index %s", cam.index
                    )
            devices.append(
                aprilcam_pb2.CameraDevice(
                    index=cam.index,
                    name=name,
                    slug=slug,
                    enum=enum_no,
                )
            )
        return aprilcam_pb2.EnumerateCamerasResponse(cameras=devices)

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

    def GetObjects(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.GetObjectsResponse:
        """Run one-shot HSV object detection on the latest frame for *cam_name*.

        Returns a ``GetObjectsResponse`` with detected non-tag colored objects
        filtered by playfield polygon containment (60 px inset), aspect ratio
        (<= 2.0), and minimum dimension (>= 15 px).  World coordinates are
        A1-centred when the camera is calibrated; ``wx=0, wy=0`` otherwise.
        """
        import cv2 as cv
        import numpy as np
        from ..vision.color_classifier import ColorClassifier

        cam_name = request.cam_name

        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)

        if pipeline is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"camera '{cam_name}' not open")
            return aprilcam_pb2.GetObjectsResponse(cam_name=cam_name)

        # Grab the latest BGR frame from the pipeline's raw frame cache.
        with pipeline._raw_lock:
            frame = pipeline._latest_raw_frame

        if frame is None:
            return aprilcam_pb2.GetObjectsResponse(cam_name=cam_name, objects=[])

        # Retrieve homography and playfield polygon from calibration.
        homography = None
        pf_poly = None
        origin_x = 0.0
        origin_y = 0.0

        calibration = pipeline._calibration
        if calibration is not None and calibration.homography is not None:
            homography = calibration.homography

            # Use the SAME A1 origin as the tag pipeline (``_a1_origin`` prefers
            # the calibrated AprilTag-1 world position from static_markers and
            # falls back to the field centre). The stored homography is already
            # A1-centred, so subtracting fw/2,fh/2 here double-shifted every
            # object by (-fw/2, -fh/2) — making object world_xy disagree with
            # get_tags / pixel_to_world. Depends on calibration, not april_cam.
            try:
                origin_x, origin_y = pipeline._a1_origin()
            except Exception:
                pass

        april_cam = pipeline._april_cam
        if april_cam is not None:
            try:
                pf_poly = april_cam.playfield.get_polygon()
            except Exception:
                pf_poly = None

        # Detect colored objects via HSV classification.
        classifier = ColorClassifier(min_area=600, max_area=30000)
        raw = classifier.classify(frame, homography=homography)

        # Build shrunk polygon (60 px inset toward centroid).
        shrunk_poly = None
        if pf_poly is not None:
            pts = np.array(pf_poly, dtype=np.float32).reshape(-1, 2)
            center = pts.mean(axis=0)
            dirs = pts - center
            lens = np.linalg.norm(dirs, axis=1, keepdims=True)
            lens = np.maximum(lens, 1e-6)
            shrunk = pts - dirs / lens * 60
            shrunk_poly = shrunk.reshape(-1, 1, 2).astype(np.float32)

        # Filter and build ObjectRecord protos.
        object_msgs = []
        for obj in raw:
            cx_f, cy_f = obj.center_px

            # Polygon containment filter.
            if shrunk_poly is not None:
                if cv.pointPolygonTest(shrunk_poly, (float(cx_f), float(cy_f)), False) < 0:
                    continue

            x, y, bw, bh = obj.bbox
            aspect = max(bw, bh) / max(min(bw, bh), 1)
            if aspect > 2.0 or min(bw, bh) < 15:
                continue

            # A1-centred world coordinates.
            wx_cm = 0.0
            wy_cm = 0.0
            if obj.world_xy is not None:
                wx_cm = float(obj.world_xy[0]) - origin_x
                wy_cm = float(obj.world_xy[1]) - origin_y

            object_msgs.append(
                aprilcam_pb2.ObjectRecord(
                    cx_px=float(cx_f),
                    cy_px=float(cy_f),
                    wx=wx_cm,
                    wy=wy_cm,
                    color=obj.color,
                    x_bbox=int(x),
                    y_bbox=int(y),
                    w_bbox=int(bw),
                    h_bbox=int(bh),
                    area_px=float(obj.area_px),
                    object_type=obj.object_type,
                    confidence=float(obj.confidence),
                )
            )

        return aprilcam_pb2.GetObjectsResponse(cam_name=cam_name, objects=object_msgs)

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
    # File-proxy RPCs
    # ------------------------------------------------------------------

    def GetCameraConfig(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.JsonBlobReply:
        """Return config.json for *cam_name* as an opaque JSON blob.

        Reads ``<cameras_dir>/<cam_name>/config.json`` using
        ``camera_config.load_camera_config``.  Returns ``present=False`` when
        the file is absent.
        """
        import json as _json
        from ..camera.camera_config import load_camera_config

        cam_name = request.cam_name
        camera_dir = self._config.cameras_dir / cam_name
        cfg = load_camera_config(camera_dir)
        if cfg is None:
            return aprilcam_pb2.JsonBlobReply(json_blob="", present=False)
        return aprilcam_pb2.JsonBlobReply(
            json_blob=_json.dumps(cfg, indent=2, sort_keys=True),
            present=True,
        )

    def SetCameraConfig(
        self,
        request: aprilcam_pb2.CameraJsonRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.StatusReply:
        """Write *json_blob* to ``<cameras_dir>/<cam_name>/config.json`` atomically.

        Uses ``camera_config.save_camera_config`` (write to ``.tmp`` then
        ``os.replace``).
        """
        import json as _json
        from ..camera.camera_config import save_camera_config

        cam_name = request.cam_name
        camera_dir = self._config.cameras_dir / cam_name
        try:
            cfg_dict = _json.loads(request.json_blob)
        except _json.JSONDecodeError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"SetCameraConfig: invalid JSON: {exc}")
            return aprilcam_pb2.StatusReply(ok=False, error=str(exc))
        try:
            save_camera_config(camera_dir, cfg_dict)
        except Exception as exc:
            log.exception("SetCameraConfig: write failed for %s", cam_name)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return aprilcam_pb2.StatusReply(ok=False, error=str(exc))
        return aprilcam_pb2.StatusReply(ok=True)

    def GetCalibration(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.JsonBlobReply:
        """Return calibration.json for *cam_name* as an opaque JSON blob.

        Reads ``<cameras_dir>/<cam_name>/calibration.json`` directly (raw JSON,
        not parsed through ``CameraCalibration.from_dict``) so that no numpy
        round-trip occurs at the gRPC boundary.  Returns ``present=False``
        when the file is absent.
        """
        cam_name = request.cam_name
        cal_file = self._config.cameras_dir / cam_name / "calibration.json"
        if not cal_file.exists():
            return aprilcam_pb2.JsonBlobReply(json_blob="", present=False)
        try:
            blob = cal_file.read_text(encoding="utf-8")
        except Exception as exc:
            log.exception("GetCalibration: read failed for %s", cam_name)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return aprilcam_pb2.JsonBlobReply(json_blob="", present=False)
        return aprilcam_pb2.JsonBlobReply(json_blob=blob, present=True)

    def SetCalibration(
        self,
        request: aprilcam_pb2.CameraJsonRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.StatusReply:
        """Write *json_blob* to calibration.json and trigger a live pipeline reload.

        Writes atomically (tmp + os.replace), then calls
        ``pipeline.reload_calibration()`` if the camera is currently open so
        the live detection loop picks up the new homography without a restart.
        """
        import json as _json
        import os as _os

        cam_name = request.cam_name
        camera_dir = self._config.cameras_dir / cam_name

        # Validate JSON before writing.
        try:
            _json.loads(request.json_blob)
        except _json.JSONDecodeError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"SetCalibration: invalid JSON: {exc}")
            return aprilcam_pb2.StatusReply(ok=False, error=str(exc))

        # Atomic write.
        try:
            camera_dir.mkdir(parents=True, exist_ok=True)
            cal_file = camera_dir / "calibration.json"
            tmp = cal_file.with_suffix(".json.tmp")
            try:
                tmp.write_text(request.json_blob, encoding="utf-8")
                _os.replace(tmp, cal_file)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
        except Exception as exc:
            log.exception("SetCalibration: write failed for %s", cam_name)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return aprilcam_pb2.StatusReply(ok=False, error=str(exc))

        # Trigger live pipeline reload if camera is open.
        with self._cam_lock:
            pipeline = self._cameras.get(cam_name)
        if pipeline is not None:
            try:
                from ..calibration.calibration import load_calibration_from_camera_dir
                from .camera_pipeline import _apply_camera_settings

                calibration = load_calibration_from_camera_dir(camera_dir)
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
            except Exception:
                log.exception(
                    "SetCalibration: pipeline reload failed for %s (file written OK)",
                    cam_name,
                )

        return aprilcam_pb2.StatusReply(ok=True)

    def GetPaths(
        self,
        request: aprilcam_pb2.CameraRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.JsonBlobReply:
        """Return paths.json for *cam_name* as an opaque JSON blob.

        Reads ``<cameras_dir>/<cam_name>/paths.json``.  Returns
        ``present=False`` when the file is absent.
        """
        cam_name = request.cam_name
        paths_file = self._config.cameras_dir / cam_name / "paths.json"
        if not paths_file.exists():
            return aprilcam_pb2.JsonBlobReply(json_blob="", present=False)
        try:
            blob = paths_file.read_text(encoding="utf-8")
        except Exception as exc:
            log.exception("GetPaths: read failed for %s", cam_name)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return aprilcam_pb2.JsonBlobReply(json_blob="", present=False)
        return aprilcam_pb2.JsonBlobReply(json_blob=blob, present=True)

    def SetPaths(
        self,
        request: aprilcam_pb2.CameraJsonRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.StatusReply:
        """Write *json_blob* to paths.json atomically.

        Writes to ``<cameras_dir>/<cam_name>/paths.json`` using
        tmp + os.replace.
        """
        import json as _json
        import os as _os

        cam_name = request.cam_name
        camera_dir = self._config.cameras_dir / cam_name

        # Validate JSON before writing.
        try:
            _json.loads(request.json_blob)
        except _json.JSONDecodeError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"SetPaths: invalid JSON: {exc}")
            return aprilcam_pb2.StatusReply(ok=False, error=str(exc))

        # Atomic write.
        try:
            camera_dir.mkdir(parents=True, exist_ok=True)
            paths_file = camera_dir / "paths.json"
            tmp = paths_file.with_suffix(".tmp")
            try:
                tmp.write_text(request.json_blob, encoding="utf-8")
                _os.replace(tmp, paths_file)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
        except Exception as exc:
            log.exception("SetPaths: write failed for %s", cam_name)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return aprilcam_pb2.StatusReply(ok=False, error=str(exc))

        return aprilcam_pb2.StatusReply(ok=True)

    def ListPlayfields(
        self,
        request: aprilcam_pb2.Empty,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.ListPlayfieldsResponse:
        """Return all playfield definitions from ``config.playfields_dir``.

        Scans ``<playfields_dir>/*.json`` and returns each file's raw content
        as a ``PlayfieldEntry``.  Files that cannot be read are skipped with a
        log warning.
        """
        playfields_dir = self._config.playfields_dir
        entries: list[aprilcam_pb2.PlayfieldEntry] = []
        if not playfields_dir.exists():
            return aprilcam_pb2.ListPlayfieldsResponse(playfields=entries)
        for p in sorted(playfields_dir.glob("*.json")):
            try:
                blob = p.read_text(encoding="utf-8")
                entries.append(
                    aprilcam_pb2.PlayfieldEntry(name=p.stem, json_blob=blob)
                )
            except Exception:
                log.warning("ListPlayfields: skipping unreadable file %s", p)
        return aprilcam_pb2.ListPlayfieldsResponse(playfields=entries)

    # ------------------------------------------------------------------
    # Mobile-tag registry
    # ------------------------------------------------------------------

    def RegisterMobileTag(
        self,
        request: aprilcam_pb2.RegisterMobileTagRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.MobileTagsResponse:
        """Register/replace a mobile tag's mount pose; persist and apply live."""
        from .mobile_tags import MobileTag, load, save

        spec = request.tag
        if spec.tag_id <= 0:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("tag_id must be a positive integer")
            return aprilcam_pb2.MobileTagsResponse()
        mt = MobileTag(
            tag_id=int(spec.tag_id),
            x_mm=float(spec.x_mm),
            y_mm=float(spec.y_mm),
            z_cm=float(spec.z_cm),
            yaw_deg=float(spec.yaw_deg),
            owner=str(spec.owner),
        )
        reg = load(self._config.data_dir)
        reg[mt.tag_id] = mt
        save(self._config.data_dir, reg)
        self._apply_mobile_to_pipelines(reg)
        log.info(
            "RegisterMobileTag: tag %d owner=%r offset=(%.1f,%.1f mm, %.1f cm, %.1f deg)",
            mt.tag_id, mt.owner, mt.x_mm, mt.y_mm, mt.z_cm, mt.yaw_deg,
        )
        return self._mobile_tags_response(reg)

    def ClearMobileTags(
        self,
        request: aprilcam_pb2.ClearMobileTagsRequest,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.MobileTagsResponse:
        """Clear one mobile tag (``tag_id``) or the whole registry (``all=true``)."""
        from .mobile_tags import load, save

        reg = load(self._config.data_dir)
        if request.all:
            reg = {}
        elif request.tag_id:
            reg.pop(int(request.tag_id), None)
        save(self._config.data_dir, reg)
        self._apply_mobile_to_pipelines(reg)
        log.info(
            "ClearMobileTags: all=%s tag_id=%s -> %d remain",
            request.all, request.tag_id, len(reg),
        )
        return self._mobile_tags_response(reg)

    def ListMobileTags(
        self,
        request: aprilcam_pb2.Empty,
        context: grpc.ServicerContext,
    ) -> aprilcam_pb2.MobileTagsResponse:
        """Return the persisted mobile-tag registry."""
        from .mobile_tags import load

        return self._mobile_tags_response(load(self._config.data_dir))

    def _apply_mobile_to_pipelines(self, registry) -> None:
        """Push *registry* to every running pipeline so it applies without restart."""
        with self._cam_lock:
            pipelines = list(self._cameras.values())
        for p in pipelines:
            try:
                p.apply_mobile_registry(registry)
            except Exception:
                log.exception("apply_mobile_registry failed for a pipeline")

    @staticmethod
    def _mobile_tags_response(registry) -> aprilcam_pb2.MobileTagsResponse:
        resp = aprilcam_pb2.MobileTagsResponse()
        for mt in registry.values():
            resp.tags.add(
                tag_id=mt.tag_id,
                x_mm=mt.x_mm,
                y_mm=mt.y_mm,
                z_cm=mt.z_cm,
                yaw_deg=mt.yaw_deg,
                owner=mt.owner,
            )
        return resp

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
