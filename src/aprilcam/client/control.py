"""aprilcam.client.control — DaemonControl: typed gRPC stub wrapper.

All RPC methods return Pydantic models from ``aprilcam.client.models``.
Proto-generated types are confined to this module.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
import grpc

from aprilcam.client._imaging import require_cv2
from aprilcam.errors import DaemonNotFoundError

from aprilcam.proto import aprilcam_pb2, aprilcam_pb2_grpc
from aprilcam.client.models import (
    CameraDevice,
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
        cli_args=None,
    ) -> "DaemonControl":
        """Return a connected DaemonControl using auto-discovery.

        This method **never spawns a daemon process**.  The daemon must already
        be running.  Use ``aprilcam daemon start`` or
        ``systemctl start aprilcamd`` to start it first.

        Resolution precedence (delegated to
        :func:`~aprilcam.client.discovery.resolve_daemon_target`):

        1. *unix_path* / *tcp_port* keyword arguments (explicit override,
           mirrors old call-site interface — still supported for backward
           compatibility with existing callers that pass these directly).
        2. *cli_args* ``daemon_host`` / ``daemon_port`` attributes.
        3. ``config.daemon_host`` / ``APRILCAM_DAEMON_HOST`` env var.
        4. Local Unix socket probe (default: ``<socket_dir>/control.sock``).
        5. mDNS browse on ``_aprilcam._tcp.local.``.

        Args:
            config: Loaded :class:`~aprilcam.config.Config` instance.
            log_level: Unused (kept for backward-compatible call sites).
            unix_path: Explicit Unix socket path — skips all discovery.
            tcp_port: Explicit TCP port — used with ``config.daemon_host``
                or ``localhost`` when *unix_path* is also provided but fails.
            cli_args: Argparse namespace with optional ``daemon_host`` and
                ``daemon_port`` attributes (from
                :func:`~aprilcam.cli._daemon.add_daemon_args`).

        Returns:
            A connected :class:`DaemonControl` instance.

        Raises:
            DaemonNotFoundError: When no reachable daemon is found via any
                resolution method.
        """
        # If an explicit unix_path or tcp_port was passed (old call-site
        # compatibility), build a synthetic cli_args-like object so the
        # resolver sees an explicit override without going through mDNS.
        if unix_path is not None:
            # Explicit Unix socket — connect directly, no discovery.
            dc = cls(unix_path=unix_path)
            dc.connect()
            try:
                dc.list_cameras()
                return dc
            except grpc.RpcError as exc:
                dc.close()
                raise DaemonNotFoundError(
                    f"Daemon at unix:{unix_path} is unreachable: {exc}. "
                    "Start the daemon with `aprilcam daemon start` or "
                    "`systemctl start aprilcamd`, or set APRILCAM_DAEMON_HOST."
                ) from exc
            except Exception as exc:
                dc.close()
                raise DaemonNotFoundError(
                    f"Daemon at unix:{unix_path} is unreachable: {exc}. "
                    "Start the daemon with `aprilcam daemon start` or "
                    "`systemctl start aprilcamd`, or set APRILCAM_DAEMON_HOST."
                ) from exc

        # Build a minimal args proxy that resolve_daemon_target understands
        class _Args:
            daemon_host = None
            daemon_port = tcp_port or config.daemon_port

        _proxy_args = cli_args if cli_args is not None else _Args()

        from aprilcam.client.discovery import resolve_daemon_target

        host, port, resolved_unix = resolve_daemon_target(config, _proxy_args)

        dc = cls(unix_path=resolved_unix, host=host, port=port)
        dc.connect()
        try:
            dc.list_cameras()
            return dc
        except grpc.RpcError as exc:
            dc.close()
            target = f"unix:{resolved_unix}" if resolved_unix else f"{host}:{port}"
            raise DaemonNotFoundError(
                f"Daemon at {target} is unreachable: {exc}. "
                "Start the daemon with `aprilcam daemon start` or "
                "`systemctl start aprilcamd`, or set APRILCAM_DAEMON_HOST."
            ) from exc
        except Exception as exc:
            dc.close()
            target = f"unix:{resolved_unix}" if resolved_unix else f"{host}:{port}"
            raise DaemonNotFoundError(
                f"Daemon at {target} is unreachable: {exc}. "
                "Start the daemon with `aprilcam daemon start` or "
                "`systemctl start aprilcamd`, or set APRILCAM_DAEMON_HOST."
            ) from exc

    @staticmethod
    def _resolve_host_to_ip(host: str, port: int) -> str:
        """Resolve *host* to a numeric IPv4 address string.

        gRPC's c-ares resolver does not perform multicast DNS (mDNS), so
        ``.local`` hostnames that resolve fine via the OS resolver (Bonjour /
        Avahi) fail when passed directly to gRPC.  This method resolves via
        :func:`socket.getaddrinfo` — which delegates to the OS resolver and
        therefore handles mDNS — and returns the numeric IP so that gRPC
        receives an address it can always reach.

        If *host* is already a numeric IP address it is returned unchanged.
        On resolution failure a clear :exc:`OSError` is raised.
        """
        import ipaddress as _ip
        import socket as _socket

        # Pass through numeric IPs without a DNS round-trip.
        try:
            _ip.ip_address(host)
            return host
        except ValueError:
            pass

        try:
            results = _socket.getaddrinfo(
                host, port, _socket.AF_INET, _socket.SOCK_STREAM
            )
        except OSError as exc:
            raise OSError(
                f"Cannot resolve host '{host}': {exc}. "
                "Check the hostname or use a numeric IP address."
            ) from exc

        if not results:
            raise OSError(
                f"Cannot resolve host '{host}': getaddrinfo returned no results."
            )

        # results[i] = (family, type, proto, canonname, (addr, port))
        return results[0][4][0]

    def connect(self) -> "DaemonControl":
        """Open the gRPC channel and create the stub.

        Idempotent — calling ``connect()`` on an already-connected instance
        is a no-op.

        When connecting via TCP, the host is resolved to a numeric IP via the
        OS resolver before being passed to gRPC.  This allows ``.local``
        (mDNS/Bonjour) hostnames to work even though gRPC's c-ares resolver
        does not support multicast DNS.
        """
        if self._channel is not None:
            return self
        if self._unix_path:
            target = f"unix:{self._unix_path}"
        else:
            resolved_ip = self._resolve_host_to_ip(self._host, self._port)
            target = f"{resolved_ip}:{self._port}"
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

    def enumerate_cameras(self) -> list[CameraDevice]:
        """Return all available hardware camera devices (not necessarily open).

        Calls the ``EnumerateCameras`` RPC; the daemon probes the hardware and
        returns one :class:`CameraDevice` per detected device.
        """
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.EnumerateCamerasResponse = stub.EnumerateCameras(
            aprilcam_pb2.Empty()
        )
        return [CameraDevice.from_proto(d) for d in resp.cameras]

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
        cv2 = require_cv2()
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

    def _stream_host(self) -> str:
        """Return the host to use for TCP stream socket connections.

        When the ``DaemonControl`` is connected via a Unix socket the stream
        socket is also local, so ``"localhost"`` is correct.  When connected
        via TCP (``_unix_path is None``), remote stream sockets are on the same
        host as the gRPC endpoint, so ``self._host`` must be forwarded.
        """
        return "localhost" if self._unix_path is not None else self._host

    def get_image_stream(
        self, cam_name: str, max_hz: int = 20
    ) -> "ImageStreamConsumer":
        """Request an image stream and return a connected ``ImageStreamConsumer``."""
        stub = self._stub_or_raise()
        resp: aprilcam_pb2.StreamEndpoint = stub.GetImageStream(
            aprilcam_pb2.StreamRequest(cam_name=cam_name, max_hz=max_hz)
        )
        endpoint = StreamEndpoint.from_proto(resp)
        consumer = ImageStreamConsumer(
            endpoint, cam_name=cam_name, host=self._stream_host()
        )
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
        consumer = TagStreamConsumer(endpoint, host=self._stream_host())
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

    # ------------------------------------------------------------------
    # File-proxy RPCs
    # ------------------------------------------------------------------

    def get_camera_config(self, cam_name: str) -> "aprilcam_pb2.JsonBlobReply":
        """Return the raw ``JsonBlobReply`` from ``GetCameraConfig``.

        MCP-server parsing (``json.loads`` + ``from_dict`` helpers) happens
        in ticket 014-005.  This stub returns the proto message as-is.

        Args:
            cam_name: Camera name as returned by :meth:`open_camera`.

        Returns:
            ``JsonBlobReply`` with ``json_blob`` (UTF-8 JSON string) and
            ``present`` (False when config.json is absent on the daemon host).
        """
        stub = self._stub_or_raise()
        return stub.GetCameraConfig(aprilcam_pb2.CameraRequest(cam_name=cam_name))

    def set_camera_config(self, cam_name: str, json_blob: str) -> "aprilcam_pb2.StatusReply":
        """Write *json_blob* to ``config.json`` on the daemon host.

        Args:
            cam_name: Camera name as returned by :meth:`open_camera`.
            json_blob: UTF-8 JSON string to write (must be valid JSON).

        Returns:
            ``StatusReply`` with ``ok=True`` on success.
        """
        stub = self._stub_or_raise()
        return stub.SetCameraConfig(
            aprilcam_pb2.CameraJsonRequest(cam_name=cam_name, json_blob=json_blob)
        )

    def get_calibration(self, cam_name: str) -> "aprilcam_pb2.JsonBlobReply":
        """Return the raw ``JsonBlobReply`` from ``GetCalibration``.

        MCP-server parsing happens in ticket 014-005.

        Args:
            cam_name: Camera name as returned by :meth:`open_camera`.

        Returns:
            ``JsonBlobReply`` with ``json_blob`` (calibration.json content)
            and ``present`` (False when calibration.json is absent).
        """
        stub = self._stub_or_raise()
        return stub.GetCalibration(aprilcam_pb2.CameraRequest(cam_name=cam_name))

    def set_calibration(self, cam_name: str, json_blob: str) -> "aprilcam_pb2.StatusReply":
        """Write *json_blob* to ``calibration.json`` and trigger a live reload.

        The daemon writes the file atomically and calls
        ``pipeline.reload_calibration()`` if the camera is currently open.

        Args:
            cam_name: Camera name as returned by :meth:`open_camera`.
            json_blob: UTF-8 JSON string to write (must be valid JSON).

        Returns:
            ``StatusReply`` with ``ok=True`` on success.
        """
        stub = self._stub_or_raise()
        return stub.SetCalibration(
            aprilcam_pb2.CameraJsonRequest(cam_name=cam_name, json_blob=json_blob)
        )

    def get_paths(self, cam_name: str) -> "aprilcam_pb2.JsonBlobReply":
        """Return the raw ``JsonBlobReply`` from ``GetPaths``.

        Args:
            cam_name: Camera name as returned by :meth:`open_camera`.

        Returns:
            ``JsonBlobReply`` with ``json_blob`` (paths.json content) and
            ``present`` (False when paths.json is absent).
        """
        stub = self._stub_or_raise()
        return stub.GetPaths(aprilcam_pb2.CameraRequest(cam_name=cam_name))

    def set_paths(self, cam_name: str, json_blob: str) -> "aprilcam_pb2.StatusReply":
        """Write *json_blob* to ``paths.json`` on the daemon host atomically.

        Args:
            cam_name: Camera name as returned by :meth:`open_camera`.
            json_blob: UTF-8 JSON string to write (must be valid JSON).

        Returns:
            ``StatusReply`` with ``ok=True`` on success.
        """
        stub = self._stub_or_raise()
        return stub.SetPaths(
            aprilcam_pb2.CameraJsonRequest(cam_name=cam_name, json_blob=json_blob)
        )

    def list_playfields(self) -> "aprilcam_pb2.ListPlayfieldsResponse":
        """Return all playfield definitions from the daemon's playfields dir.

        MCP-server parsing happens in ticket 014-005.

        Returns:
            ``ListPlayfieldsResponse`` with a repeated ``PlayfieldEntry`` list,
            each carrying ``name`` (slug) and ``json_blob`` (raw file content).
        """
        stub = self._stub_or_raise()
        return stub.ListPlayfields(aprilcam_pb2.Empty())


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
        field_width_cm=float(resp.field_width_cm),
        field_height_cm=float(resp.field_height_cm),
        origin_x=float(resp.origin_x),
        origin_y=float(resp.origin_y),
    )
