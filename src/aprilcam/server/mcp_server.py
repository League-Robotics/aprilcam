"""MCP server exposing AprilCam camera, playfield, and image-processing tools.

This module implements the FastMCP server that provides AI agents with
programmatic access to camera management, playfield homography, tag
detection loops, multi-camera compositing, and image processing
operations. It is the primary entry point for the ``aprilcam mcp``
subcommand and the ``aprilcam-mcp`` standalone script.

Camera management is delegated to the AprilCam daemon via
:class:`aprilcam.client.control.DaemonControl`.  Path mutations
(create_path, delete_path, clear_paths) write the current path list
atomically to ``paths.json`` inside the camera's data directory so the
``aprilcam view`` subscriber process can reload them without IPC.

All ``@server.tool()`` functions follow a consistent error-handling
contract: on success they return structured JSON (or image data), and
on error they return ``{"error": "<message>"}``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path
from typing import Any, Optional

import numpy as np
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from aprilcam.config import Config
from aprilcam.client.control import DaemonControl
from aprilcam.core.aprilcam import AprilCam
from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry
from aprilcam.camera.composite import (
    CompositeManager,
    compute_cross_camera_homography,
    map_tags_to_primary,
)
from aprilcam.core.detection import DetectionLoop, RingBuffer
from aprilcam.server.frame import FrameEntry, FrameRegistry
from aprilcam.calibration.homography import (
    CORNER_ID_MAP,
    detect_aruco_4x4,
)
from aprilcam.calibration.calibration import (
    CameraPosition,
    FieldSpec,
    calibrate_from_corners,
)
from aprilcam.vision.image_processing import (
    process_detect_circles,
    process_detect_contours,
    process_detect_lines,
    process_detect_qr_codes,
)
from aprilcam.errors import CameraError, CameraNotFoundError, CameraInUseError
from aprilcam.core.models import AprilTag
from aprilcam.core.playfield import PlayfieldBoundary as Playfield
from aprilcam.server import paths as paths_module
from aprilcam.server.paths import PathRegistry, Waypoint

# ---------------------------------------------------------------------------
# Camera registry
# ---------------------------------------------------------------------------


class CameraRegistry:
    """Manages open camera/capture handles keyed by deterministic strings."""

    def __init__(self) -> None:
        self._cameras: dict[str, Any] = {}

    def open(self, capture: Any, handle: str | None = None) -> str:
        """Register *capture* and return a handle string.

        If *handle* is provided it is used as-is (deterministic).
        Otherwise a UUID4 is generated.
        """
        if handle is None:
            handle = str(uuid.uuid4())
        self._cameras[handle] = capture
        return handle

    def get(self, camera_id: str) -> Any:
        """Return the capture for *camera_id* or raise ``KeyError``."""
        return self._cameras[camera_id]

    def close(self, camera_id: str) -> None:
        """Release and remove the capture identified by *camera_id*.

        Raises ``KeyError`` if *camera_id* is not registered.
        If the stored capture is ``None`` (daemon-managed cameras),
        the release call is skipped.
        """
        cap = self._cameras.pop(camera_id)  # KeyError if missing
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def close_all(self) -> None:
        """Release every open capture and clear the registry.

        Individual release errors are swallowed so the rest still get closed.
        ``None`` entries (daemon-managed cameras) are skipped silently.
        """
        for cap in self._cameras.values():
            if cap is None:
                continue
            try:
                cap.release()
            except Exception:
                pass
        self._cameras.clear()

    def list_open(self) -> list[str]:
        """Return a list of currently-active handle strings."""
        return list(self._cameras.keys())

    def __del__(self) -> None:
        self.close_all()


# ---------------------------------------------------------------------------
# Playfield registry
# ---------------------------------------------------------------------------


@dataclass
class PlayfieldEntry:
    """A registered playfield backed by a camera."""

    playfield_id: str
    camera_id: str
    playfield: Playfield
    field_spec: Optional[FieldSpec] = None
    homography: Optional[np.ndarray] = None
    tag1_origin_cm: Optional[tuple] = None  # (x, y) raw world position of tag 1


def _get_playfield_origin(entry: "PlayfieldEntry") -> tuple:
    """Return (origin_x, origin_y) for A1-centred coordinate conversion.

    Uses tag 1's calibrated world position when available (from stored
    static_markers), otherwise falls back to the field-centre.
    """
    if entry.tag1_origin_cm is not None:
        return entry.tag1_origin_cm
    if entry.field_spec is not None:
        return (entry.field_spec.width_cm / 2.0, entry.field_spec.height_cm / 2.0)
    return (0.0, 0.0)


def _a1_coord_transform(origin_x: float, origin_y: float):
    """Return a tag-record transform that applies A1-centred coordinate conversion."""
    def _transform(tag_records):
        result = []
        for tr in tag_records:
            if tr.world_xy is None:
                result.append(tr)
            else:
                wx = tr.world_xy[0] - origin_x
                wy = origin_y - tr.world_xy[1]
                result.append(_dc_replace(tr, world_xy=(wx, wy)))
        return result
    return _transform


def _resolve_source_playfield(source_id: str, camera_id: "str | None" = None):
    """Return the PlayfieldEntry for *source_id*, accepting either handle.

    Resolves a playfield from a playfield_id directly, or — when *source_id*
    (or *camera_id*) is a camera handle — from the playfield associated with
    that camera (e.g. one rehydrated by ``open_camera``).  This lets world-
    coordinate conversion work whether the caller passes the playfield_id or
    the camera_id.  Returns ``None`` when no playfield can be resolved.
    """
    try:
        return playfield_registry.get(source_id)
    except KeyError:
        pass
    for cid in (source_id, camera_id):
        if not cid:
            continue
        pid = playfield_registry.find_by_camera(cid)
        if pid is not None:
            try:
                return playfield_registry.get(pid)
            except KeyError:
                pass
    return None


class DaemonCapture:
    """Wraps a DaemonControl client as a cv2.VideoCapture-compatible source.

    The DetectionLoop calls ``source.read()`` expecting ``(bool, ndarray)``.
    Daemon-owned cameras are not backed by a real VideoCapture object in the
    registry, so this adapter bridges the gap.
    """

    def __init__(self, client: "DaemonControl", cam_name: str) -> None:
        self._client = client
        self._cam_name = cam_name

    def read(self):
        try:
            frame = self._client.capture_frame(self._cam_name)
            if frame is None:
                return False, None
            return True, frame
        except Exception:
            return False, None

    def isOpened(self) -> bool:  # noqa: N802
        return True

    def release(self) -> None:
        pass


class PlayfieldRegistry:
    """Manages playfield entries keyed by playfield_id."""

    def __init__(self) -> None:
        self._playfields: dict[str, PlayfieldEntry] = {}

    def register(self, entry: PlayfieldEntry) -> None:
        self._playfields[entry.playfield_id] = entry

    def get(self, playfield_id: str) -> PlayfieldEntry:
        return self._playfields[playfield_id]  # raises KeyError

    def list(self) -> list[str]:
        return list(self._playfields.keys())

    def remove(self, playfield_id: str) -> None:
        del self._playfields[playfield_id]

    def find_by_camera(self, camera_id: str) -> Optional[str]:
        for pid, entry in self._playfields.items():
            if entry.camera_id == camera_id:
                return pid
        return None


# ---------------------------------------------------------------------------
# Module-level instances
# ---------------------------------------------------------------------------

_SERVER_INSTRUCTIONS = """\
AprilCam MCP server — perception for an AprilTag/ArUco robotics playfield.

Golden path (do this in order, in YOUR session):
  1. open_camera(pattern="<name>") or open_camera(index=N). It returns
     camera_id and — when the camera is configured + calibrated — playfield_id
     and playfield_name. open_camera REHYDRATES the playfield from disk; the
     server keeps NO state across restarts and auto-opens nothing, so you must
     call open_camera before any query.
  2. Use the playfield_id (e.g. "pf_<camera>") as the source_id for
     stream_tags / get_tags / get_objects / where / create_path. The playfield
     carries the calibration homography, so tag and object world_xy populate
     (centimetres, A1-centred: origin at AprilTag 1, +x east, +y north).
     Passing the camera_id also works — the server auto-resolves the camera's
     playfield — but playfield_id is canonical. Without a calibrated playfield,
     world_xy is null and only pixel coordinates are available.

Discover the field with get_playfield()/list_playfields(); search it with
where(). Call get_robot_api_guide() for the full reference (incl. the
DaemonControl Python API for high-rate robot control).
"""

server = FastMCP("aprilcam", instructions=_SERVER_INSTRUCTIONS)
registry = CameraRegistry()
playfield_registry = PlayfieldRegistry()
path_registry = PathRegistry()
composite_manager = CompositeManager()
frame_registry = FrameRegistry()
playfield_def_registry = PlayfieldDefinitionRegistry()

# ---------------------------------------------------------------------------
# Daemon client (initialised lazily on first use via DaemonControl.connect_default;
# None until first call — e.g. in tests that do not need the daemon).
# ---------------------------------------------------------------------------

_daemon_client: Optional[DaemonControl] = None

# Per-camera info read from info.json after open_camera RPC.
# Keys are camera_id strings (e.g. "cam_0"); values are the parsed
# info.json dicts including a "paths_file" key.
_cam_info: dict[str, dict] = {}


def _ensure_daemon_client() -> DaemonControl:
    """Return the module-level daemon client, starting the daemon if needed."""
    global _daemon_client
    if _daemon_client is None:
        config = Config.load()
        _daemon_client = DaemonControl.connect_default(config)
    return _daemon_client


# ---------------------------------------------------------------------------
# paths.json write helper
# ---------------------------------------------------------------------------


def _get_paths_file(camera_id: str) -> Optional[Path]:
    """Return the paths.json :class:`~pathlib.Path` for *camera_id*, or ``None``.

    Checks ``_cam_info`` first (populated by open_camera RPC); falls back to
    reading info.json from disk so that path tools work even when the MCP
    server was restarted after the camera was opened.
    """
    info = _cam_info.get(camera_id)
    if info is None:
        return None
    # Prefer paths_file if already stored; otherwise derive from camera_dir.
    pf_str = info.get("paths_file")
    if pf_str:
        return Path(pf_str)
    camera_dir = info.get("camera_dir")
    if camera_dir:
        return Path(camera_dir) / "paths.json"
    return None


def _write_paths_json(playfield_id: str) -> None:
    """Atomically rewrite paths.json for the camera backing *playfield_id*.

    Looks up the camera_id from the playfield registry, then resolves the
    paths_file path from ``_cam_info``.  If either lookup fails (e.g. in
    tests where the daemon is not running), the write is silently skipped.

    The write is atomic: a ``.tmp`` file is written first, then renamed.
    """
    try:
        pf_entry = playfield_registry.get(playfield_id)
        camera_id = pf_entry.camera_id
    except KeyError:
        return

    paths_file = _get_paths_file(camera_id)
    if paths_file is None:
        return

    data = [p.to_dict() for p in path_registry.list_for(playfield_id)]
    tmp = paths_file.with_suffix(".tmp")
    try:
        paths_file.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, paths_file)
    except OSError:
        pass  # Best-effort; do not crash the MCP handler


@dataclass
class DetectionEntry:
    """A running detection loop bound to a source (camera or playfield)."""

    source_id: str
    loop: DetectionLoop
    ring_buffer: RingBuffer
    aprilcam: AprilCam
    operations: list[str] = field(default_factory=lambda: ["detect_tags"])
    robot_tag_id: Optional[int] = None
    gripper_offset_cm: float = 14.0


detection_registry: dict[str, DetectionEntry] = {}


@dataclass
class LiveViewEntry:
    """A registered live-view session (viewer spawned as detached subprocess)."""

    source_id: str
    process: Any  # Popen handle or None for detached subprocesses
    ring_buffer: RingBuffer


live_view_registry: dict[str, LiveViewEntry] = {}


# ---------------------------------------------------------------------------
# Source resolution & image output helpers
# ---------------------------------------------------------------------------


def resolve_source(source_id: str) -> np.ndarray:
    """Resolve a source_id (playfield or camera) to a captured frame.

    If a detection loop is running on the source, returns the latest
    frame from the loop's cache (avoids racing with the loop for
    camera reads).  Otherwise reads directly from the camera.

    If *source_id* names a playfield, the frame is deskewed automatically.

    Raises:
        KeyError: if *source_id* is not found in either registry.
        RuntimeError: if the underlying capture fails to read a frame.
    """
    # If a detection loop is running, grab its cached frame to avoid
    # racing with the loop thread for camera reads.
    det_entry = detection_registry.get(source_id)
    if det_entry is not None and det_entry.loop.last_frame is not None:
        frame = det_entry.loop.last_frame.copy()
        # Apply playfield deskew if this source is a playfield
        try:
            pf_entry = playfield_registry.get(source_id)
            frame = pf_entry.playfield.deskew(frame)
        except KeyError:
            pass
        return frame

    # Try playfield first
    try:
        pf_entry = playfield_registry.get(source_id)
        camera_id = pf_entry.camera_id
        frame = _read_one_frame(camera_id)
        return pf_entry.playfield.deskew(frame)
    except KeyError:
        pass

    # Try camera (may be None sentinel when daemon owns the capture)
    if source_id not in registry._cameras:
        raise KeyError(f"Unknown source_id '{source_id}'")

    cap = registry._cameras.get(source_id)
    if cap is None:
        # Daemon-owned camera — read from data socket
        return _read_one_frame(source_id)

    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Failed to read frame")
    return frame


def _read_one_frame(camera_id: str) -> np.ndarray:
    """Read a single decoded BGR frame from the daemon data socket for *camera_id*."""
    for frame in _frames_from_daemon(camera_id, 1):
        return frame
    raise RuntimeError(f"No frame available from daemon for camera '{camera_id}'")


def format_image_output(
    frame: np.ndarray,
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Encode *frame* as JPEG and return MCP content items.

    Args:
        frame: BGR image as a NumPy array.
        format: ``"base64"`` (default) returns an ``ImageContent`` with
            inline data; ``"file"`` writes a temp file and returns a
            ``TextContent`` with the path.
        quality: JPEG quality (0-100).
    """
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Failed to encode frame as JPEG")

    if format == "file":
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(buf.tobytes())
        tmp.close()
        return [TextContent(type="text", text=json.dumps({"path": tmp.name}))]

    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return [ImageContent(type="image", data=b64, mimeType="image/jpeg")]


# ---------------------------------------------------------------------------
# Core handler functions (business logic, plain Python return values)
# ---------------------------------------------------------------------------


def _handle_list_cameras() -> list[dict]:
    """Core logic for list_cameras — returns a list of camera info dicts."""
    from aprilcam.camera.camutil import list_cameras as _list_cameras

    try:
        cams = _list_cameras(max_index=10, quiet=True, detailed_names=True)
        return [
            {"index": c.index, "name": c.name, "backend": c.backend, "device_name": c.device_name}
            for c in cams
        ]
    except Exception:
        return []  # empty array, not an error


def _handle_open_camera(
    index: int | None = None,
    pattern: str | None = None,
    source: str | None = None,
    backend: str | None = None,
) -> dict:
    """Core logic for open_camera — returns ``{"camera_id": ...}`` or ``{"error": ...}``."""
    try:
        if source == "screen":
            from aprilcam.camera.screencap import ScreenCaptureMSS

            cap = ScreenCaptureMSS()
            handle = "screen"
            camera_id = registry.open(cap, handle=handle)
            return {"camera_id": camera_id}

        # Resolve index from pattern or default
        idx: int
        if pattern is not None:
            from aprilcam.camera.camutil import (
                list_cameras as _list_cameras,
                select_camera_by_pattern,
            )

            cams = _list_cameras(max_index=10, quiet=True)
            resolved = select_camera_by_pattern(pattern, cams)
            if resolved is None:
                return {"error": f"No camera matching pattern '{pattern}'"}
            idx = resolved
        elif index is not None:
            idx = index
        else:
            idx = 0

        # Use the daemon's cam_name (device slug) as handle after RPC
        handle = None  # resolved below after daemon open

        # Delegate to daemon via gRPC — no direct cv.VideoCapture here
        client = _ensure_daemon_client()
        cam_name, camera_dir = client.open_camera(idx)
        handle = cam_name

        # Use the daemon-returned camera_dir (absolute path) to build file paths.
        # This avoids CWD-relative Config.load() path mismatches when the MCP
        # server runs from a different directory than the daemon.
        info: dict = {
            "camera_dir": camera_dir,
            "paths_file": str(Path(camera_dir) / "paths.json"),
        }

        # Store a sentinel (None) in the registry so other code that checks
        # "is this camera_id registered?" still works.  Close any stale entry first.
        if handle in registry._cameras:
            try:
                registry.close(handle)
            except Exception:
                pass
        registry.open(None, handle=handle)

        # Cache info.json data for path tools
        _cam_info[handle] = info

        # Write an empty paths.json to clear any stale state from a prior session
        paths_file_str = info.get("paths_file")
        if paths_file_str:
            paths_file = Path(paths_file_str)
            tmp = paths_file.with_suffix(".tmp")
            try:
                paths_file.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text("[]")
                os.replace(tmp, paths_file)
            except OSError:
                pass

        # --- Auto-rehydrate playfield entry from disk ---
        result_extra: dict = {}
        try:
            camera_dir = Path(info.get("camera_dir", ""))
            if camera_dir:
                from aprilcam.camera.camera_config import load_camera_config
                from aprilcam.calibration.calibration import load_calibration_from_camera_dir

                cam_cfg = load_camera_config(camera_dir)
                if cam_cfg and "playfield" in cam_cfg:
                    pf_slug = cam_cfg["playfield"]
                    try:
                        pf_def = playfield_def_registry.get(pf_slug)
                    except KeyError:
                        pf_def = None

                    if pf_def is not None:
                        cal = load_calibration_from_camera_dir(camera_dir, cam_cfg, pf_def)
                        if cal is not None and cal.homography is not None:
                            # Guard: don't overwrite an existing PlayfieldEntry for this camera
                            existing_pid = playfield_registry.find_by_camera(handle)
                            if existing_pid is None:
                                pf = Playfield(detect_inverted=True, proc_width=0)
                                from aprilcam.calibration.geometry import corner_pixels_from_homography
                                poly = corner_pixels_from_homography(
                                    cal.homography, pf_def.width_cm, pf_def.height_cm
                                )
                                pf._poly = poly  # type: ignore[attr-defined]
                                pf_entry = PlayfieldEntry(
                                    playfield_id=f"pf_{handle}",
                                    camera_id=handle,
                                    playfield=pf,
                                    field_spec=FieldSpec(pf_def.width_cm, pf_def.height_cm, "cm"),
                                    homography=cal.homography,
                                    tag1_origin_cm=(0.0, 0.0),
                                )
                                playfield_registry.register(pf_entry)
                                result_extra = {
                                    "playfield_id": f"pf_{handle}",
                                    "playfield_name": pf_slug,
                                }
                                if getattr(cal, "calibration_stale", False):
                                    result_extra["calibration_stale"] = True
        except Exception as _rh_exc:
            import logging
            logging.getLogger("aprilcam").warning("Playfield rehydration failed: %s", _rh_exc)

        base_result: dict = {"camera_id": handle, "cam_name": cam_name}
        return {**base_result, **result_extra}
    except Exception as exc:
        return {"error": str(exc)}


def _handle_close_camera(camera_id: str) -> dict:
    """Core logic for close_camera — returns ``{"status": "closed"}`` or ``{"error": ...}``."""
    try:
        registry.close(camera_id)
    except KeyError:
        return {"error": f"Unknown camera_id '{camera_id}'"}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}
    return {"status": "closed"}


def _handle_set_camera_playfield(camera_id: str, playfield: str) -> dict:
    """Core logic for set_camera_playfield — returns result dict or ``{"error": ...}``."""
    # Validate camera_id is open
    if camera_id not in registry._cameras:
        return {"error": f"Unknown camera_id '{camera_id}'"}

    # Validate playfield name exists in the definition registry
    available = playfield_def_registry.list()
    if playfield not in available:
        names = ", ".join(available) if available else "(none loaded)"
        return {"error": f"Playfield '{playfield}' not found. Available: [{names}]"}

    # Resolve camera_dir from _cam_info
    info = _cam_info.get(camera_id)
    if info is None:
        return {"error": f"No camera info found for '{camera_id}' — was it opened via open_camera?"}
    camera_dir_str = info.get("camera_dir")
    if not camera_dir_str:
        return {"error": f"camera_dir not available for '{camera_id}'"}

    camera_dir = Path(camera_dir_str)

    # Write config.json atomically
    from aprilcam.camera.camera_config import save_camera_config

    config_path = save_camera_config(camera_dir, {"playfield": playfield})
    return {
        "camera_id": camera_id,
        "playfield": playfield,
        "config_path": str(config_path),
    }


def _handle_capture_frame(
    camera_id: str,
    format: str = "base64",
    quality: int = 85,
) -> dict:
    """Core logic for capture_frame — returns image dict or error dict.

    Returns:
        ``{"type": "image", "data": ..., "mime": "image/jpeg"}`` for base64,
        ``{"type": "file", "path": ...}`` for file format,
        ``{"type": "error", "error": ...}`` for errors.
    """
    # Check if this is a playfield ID first
    pf_entry = None
    try:
        pf_entry = playfield_registry.get(camera_id)
        # Resolve to underlying camera
        try:
            cap = registry.get(pf_entry.camera_id)
        except KeyError:
            return {"type": "error", "error": f"Underlying camera '{pf_entry.camera_id}' is no longer open"}
    except KeyError:
        # Not a playfield, try camera registry
        try:
            cap = registry.get(camera_id)
        except KeyError:
            return {"type": "error", "error": f"Unknown camera_id '{camera_id}'"}

    try:
        import cv2
        import time

        ret, frame = None, None
        if cap is None:
            # Daemon-owned camera: fetch a frame via gRPC
            cam_id = pf_entry.camera_id if pf_entry is not None else camera_id
            try:
                client = _ensure_daemon_client()
                frame = client.capture_frame(cam_id)
                ret = True
            except Exception:
                pass
        else:
            for _attempt in range(5):
                ret, frame = cap.read()
                if ret:
                    break
                time.sleep(0.1)
        if not ret:
            return {"type": "error", "error": "Failed to read frame"}

        # Apply deskew if this is a playfield capture
        if pf_entry is not None:
            frame = pf_entry.playfield.deskew(frame)

        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality]
        )
        if not ok:
            return {"type": "error", "error": "Failed to encode frame"}

        if format == "file":
            tmp = tempfile.NamedTemporaryFile(
                suffix=".jpg", delete=False
            )
            tmp.write(buf.tobytes())
            tmp.close()
            return {"type": "file", "path": tmp.name}

        # default: base64
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        return {"type": "image", "data": b64, "mime": "image/jpeg"}
    except Exception as exc:
        return {"type": "error", "error": str(exc)}


def _frames_from_daemon(camera_id: str, count: int):
    """Yield up to *count* decoded BGR frames from the daemon data socket.

    Connects to <socket_dir>/<camera_id>/data.sock, reads msgpack frames,
    and yields numpy arrays.  Closes the socket when done or on error.
    """
    import socket as _socket
    import numpy as np
    import cv2 as cv
    from aprilcam.daemon.protocol import read_frame

    info = _cam_info.get(camera_id)
    if info is None:
        return

    sock_path = info.get("data_socket")
    if not sock_path:
        return

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
        for _ in range(count):
            try:
                msg = read_frame(sock)
            except ConnectionError:
                break
            if msg.frame_jpeg:
                buf = np.frombuffer(bytes(msg.frame_jpeg), dtype=np.uint8)
                frame = cv.imdecode(buf, cv.IMREAD_COLOR)
                if frame is not None:
                    yield frame
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _handle_create_playfield(
    camera_id: str,
    max_frames: int = 30,
) -> dict:
    """Core logic for create_playfield — returns result dict or error dict."""
    try:
        # Validate camera exists (sentinel None is fine — daemon owns the capture)
        if camera_id not in registry._cameras:
            return {"error": f"Unknown camera_id '{camera_id}'"}

        # Create playfield and try to detect corners (proc_width=0 disables downscale)
        pf = Playfield(detect_inverted=True, proc_width=0)
        last_frame = None
        for frame in _frames_from_daemon(camera_id, max(1, max_frames)):
            last_frame = frame
            pf.update(frame)
            if pf.get_polygon() is not None:
                break

        poly = pf.get_polygon()

        # Fallback: if tag-based detection failed, try the stored camera calibration.
        # A calibrated camera already has a homography; project the four field corners
        # from world space back to pixel space to build the polygon.
        stored_cal = None
        stored_H = None
        stored_field_w = None
        stored_field_h = None
        if poly is None:
            try:
                from aprilcam.calibration.calibration import (
                    load_calibration_from_camera_dir,
                )
                info = _cam_info.get(camera_id, {})
                _camera_dir = info.get("camera_dir", "")
                cam_dir = Path(_camera_dir) if _camera_dir else None
                cal = load_calibration_from_camera_dir(cam_dir) if cam_dir is not None else None
                if cal is not None and cal.homography is not None:
                    # Read field dimensions from the calibration file
                    import json as _json
                    cal_file = cam_dir / "calibration.json"
                    cal_data = _json.loads(cal_file.read_text())
                    fw = cal_data.get("field_width_cm", 101.0)
                    fh = cal_data.get("field_height_cm", 89.0)
                    # Invert H to project world corners → pixel corners
                    H_inv = np.linalg.inv(cal.homography)
                    world_corners = np.array([[0,0],[fw,0],[fw,fh],[0,fh]], dtype=np.float64)
                    px_corners = []
                    for wx, wy in world_corners:
                        v = H_inv @ np.array([wx, wy, 1.0])
                        px_corners.append([v[0]/v[2], v[1]/v[2]])
                    poly = np.array(px_corners, dtype=np.float32)
                    stored_cal = cal
                    stored_H = cal.homography
                    stored_field_w = fw
                    stored_field_h = fh
            except Exception:
                pass

        if poly is None:
            return {
                "error": "Failed to detect corner markers and no stored calibration found",
            }

        # Register the playfield
        playfield_id = f"pf_{camera_id}"

        # Replace existing if same camera
        existing = playfield_registry.find_by_camera(camera_id)
        if existing:
            playfield_registry.remove(existing)

        tag1_origin_cm = None
        if stored_cal is not None and stored_cal.static_markers:
            m = stored_cal.static_markers.get("apriltag:1")
            if m and m.get("world") and len(m["world"]) >= 2:
                tag1_origin_cm = (float(m["world"][0]), float(m["world"][1]))
        entry = PlayfieldEntry(
            playfield_id=playfield_id,
            camera_id=camera_id,
            playfield=pf,
            homography=stored_H,
            field_spec=FieldSpec(stored_field_w, stored_field_h, "cm") if stored_field_w else None,
            tag1_origin_cm=tag1_origin_cm,
        )
        # Inject the polygon from calibration if tag detection failed
        if stored_cal is not None:
            pf._poly = poly  # type: ignore[attr-defined]
        playfield_registry.register(entry)

        corners = poly.tolist()  # UL, UR, LR, LL
        calibrated = stored_H is not None
        result = {
            "playfield_id": playfield_id,
            "corners": corners,
            "calibrated": calibrated,
        }
        if calibrated:
            result["field_width_cm"] = stored_field_w
            result["field_height_cm"] = stored_field_h
        return result
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_get_playfield_info(playfield_id: str) -> dict:
    """Core logic for get_playfield_info — returns info dict or error dict."""
    try:
        try:
            entry = playfield_registry.get(playfield_id)
        except KeyError:
            return {"error": f"Unknown playfield_id '{playfield_id}'"}

        poly = entry.playfield.get_polygon()
        calibrated = entry.homography is not None

        result: dict = {
            "playfield_id": entry.playfield_id,
            "camera_id": entry.camera_id,
            "corners": poly.tolist() if poly is not None else None,
            "calibrated": calibrated,
        }

        if calibrated and entry.field_spec is not None:
            result["width_cm"] = entry.field_spec.width_cm
            result["height_cm"] = entry.field_spec.height_cm
            result["homography"] = entry.homography.tolist()

        return result
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_calibrate_playfield(
    playfield_id: str,
    width: float,
    height: float,
    units: str = "cm",
) -> dict:
    """Core logic for calibrate_playfield — returns result dict or error dict."""
    try:
        try:
            entry = playfield_registry.get(playfield_id)
        except KeyError:
            return {"error": f"Unknown playfield_id '{playfield_id}'"}

        poly = entry.playfield.get_polygon()
        if poly is None:
            return {"error": "Playfield has no polygon (detection not complete)"}

        pixel_corners = {
            "upper_left": (float(poly[0][0]), float(poly[0][1])),
            "upper_right": (float(poly[1][0]), float(poly[1][1])),
            "lower_right": (float(poly[2][0]), float(poly[2][1])),
            "lower_left": (float(poly[3][0]), float(poly[3][1])),
        }

        field_spec = FieldSpec(width_in=width, height_in=height, units=units)

        H, _, _ = calibrate_from_corners(pixel_corners, field_spec)

        entry.field_spec = field_spec
        entry.homography = H

        # Persist calibration into the daemon's per-camera directory.
        # camera_dir is returned by the daemon at open_camera time, so it is
        # always an absolute path regardless of the MCP server's CWD.
        per_camera_path: str | None = None
        try:
            camera_id = entry.camera_id
            cap = registry.get(camera_id)
            import cv2
            cap_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            cap_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

            from aprilcam.camera.camutil import get_device_name
            from aprilcam.calibration.calibration import (
                CameraCalibration,
                save_calibration_to_camera_dir,
            )

            # Resolve device name from camera index
            cam_idx: int | None = None
            if camera_id.startswith("cam_"):
                try:
                    cam_idx = int(camera_id.split("_", 1)[1])
                except (ValueError, IndexError):
                    pass
            dev_name = get_device_name(cam_idx) if cam_idx is not None else None

            camera_dir_str = _cam_info.get(camera_id, {}).get("camera_dir", "")
            if dev_name and cap_w and cap_h and camera_dir_str:
                cal = CameraCalibration(
                    device_name=dev_name,
                    resolution=(cap_w, cap_h),
                    homography=H,
                )
                pc_path = save_calibration_to_camera_dir(
                    cal,
                    camera_dir_str,
                    field_width_cm=field_spec.width_cm,
                    field_height_cm=field_spec.height_cm,
                )
                per_camera_path = str(pc_path)
        except Exception as _persist_exc:
            import logging
            logging.getLogger("aprilcam").warning("Failed to persist calibration: %s", _persist_exc)

        result = {
            "playfield_id": playfield_id,
            "calibrated": True,
            "width_cm": field_spec.width_cm,
            "height_cm": field_spec.height_cm,
        }
        if per_camera_path:
            result["homography_file"] = per_camera_path
        return result
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_start_detection(
    source_id: str,
    family: str = "36h11",
    proc_width: int = 0,
    detect_interval: int = 1,
    use_clahe: bool = False,
    use_sharpen: bool = False,
    robot_tag_id: Optional[int] = None,
    gripper_offset_cm: float = 14.0,
) -> dict:
    """Core logic for start_detection — returns status dict or error dict."""
    try:
        if source_id in detection_registry:
            return {"error": f"Detection already running on '{source_id}'"}

        import cv2

        # Resolve source to a capture object and optional playfield data
        cap = None
        homography = None
        playfield_poly = None
        camera_id: str | None = None  # the registry handle to re-open on stop
        camera_index: int | None = None  # real device index (if applicable)
        exclusive_cap = None  # set when we open our own exclusive camera

        try:
            pf_entry = playfield_registry.get(source_id)
            camera_id = pf_entry.camera_id
            try:
                cap = registry.get(camera_id)
            except KeyError:
                return {"error": f"Underlying camera '{camera_id}' is no longer open"}
            homography = pf_entry.homography
            poly = pf_entry.playfield.get_polygon()
            if poly is not None:
                playfield_poly = poly
        except KeyError:
            # Not a playfield_id — treat source_id as a camera handle.
            camera_id = source_id
            try:
                cap = registry.get(source_id)
            except KeyError:
                return {"error": f"Unknown source_id '{source_id}'"}
            # Hardening: if this camera has an associated playfield (e.g. one
            # rehydrated by open_camera), use its homography/polygon so tag
            # world_xy is computed even when the caller passes the camera id.
            _assoc = _resolve_source_playfield(source_id)
            if _assoc is not None:
                homography = _assoc.homography
                _assoc_poly = _assoc.playfield.get_polygon()
                if _assoc_poly is not None:
                    playfield_poly = _assoc_poly

        # For real cameras (cam_N handles), open an exclusive capture to
        # avoid frame contention with other MCP tools reading the same handle.
        if camera_id and camera_id.startswith("cam_"):
            try:
                camera_index = int(camera_id.split("_", 1)[1])
            except (ValueError, IndexError):
                camera_index = None

            if camera_index is not None:
                # Release the shared camera so the detection loop gets exclusive access
                try:
                    registry.close(camera_id)
                except KeyError:
                    pass

                exclusive_cap = cv2.VideoCapture(camera_index)
                if exclusive_cap.isOpened():
                    cap = exclusive_cap
                else:
                    # Re-open shared camera on failure
                    exclusive_cap = None
                    try:
                        shared_cap = cv2.VideoCapture(camera_index)
                        if shared_cap.isOpened():
                            registry.open(shared_cap, handle=camera_id)
                            cap = registry.get(camera_id)
                    except Exception:
                        pass

        # Daemon-owned cameras have None in the registry; wrap with DaemonCapture
        # so DetectionLoop can call .read() via gRPC.
        if cap is None and camera_id is not None:
            try:
                daemon_client = _ensure_daemon_client()
                cap = DaemonCapture(daemon_client, camera_id)
            except Exception as exc:
                return {"error": f"Cannot reach daemon for camera '{camera_id}': {exc}"}

        cam = AprilCam(
            index=camera_index if camera_index is not None else 0,
            backend=None,
            speed_alpha=0.3,
            family=family,
            proc_width=proc_width,
            detect_interval=detect_interval,
            use_clahe=use_clahe,
            use_sharpen=use_sharpen,
            headless=True,
            cap=cv2.VideoCapture(),
            homography=homography,
            playfield_poly_init=playfield_poly,
        )

        buf = RingBuffer(maxlen=300)
        coord_transform = None
        _pf = _resolve_source_playfield(source_id, camera_id)
        if _pf is not None:
            ox, oy = _get_playfield_origin(_pf)
            if ox != 0.0 or oy != 0.0:
                coord_transform = _a1_coord_transform(ox, oy)
        loop = DetectionLoop(source=cap, aprilcam=cam, ring_buffer=buf,
                             coord_transform=coord_transform)
        loop.start()

        detection_registry[source_id] = DetectionEntry(
            source_id=source_id,
            loop=loop,
            ring_buffer=buf,
            aprilcam=cam,
            robot_tag_id=robot_tag_id,
            gripper_offset_cm=gripper_offset_cm,
        )
        # Remember state so stop_detection can re-open the shared camera
        detection_registry[source_id]._camera_id = camera_id  # type: ignore[attr-defined]
        detection_registry[source_id]._camera_index = camera_index  # type: ignore[attr-defined]
        detection_registry[source_id]._exclusive_cap = exclusive_cap  # type: ignore[attr-defined]

        return {"source_id": source_id, "status": "started"}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_stop_detection(source_id: str) -> dict:
    """Core logic for stop_detection — returns status dict or error dict."""
    try:
        entry = detection_registry.pop(source_id, None)
        if entry is None:
            return {"error": f"No detection running on '{source_id}'"}

        entry.loop.stop()

        # Release the exclusive capture and re-open the shared camera
        exclusive_cap = getattr(entry, "_exclusive_cap", None)
        if exclusive_cap is not None:
            try:
                exclusive_cap.release()
            except Exception:
                pass
        camera_id = getattr(entry, "_camera_id", None)
        camera_index = getattr(entry, "_camera_index", 0)
        if camera_id is not None:
            try:
                import cv2
                shared_cap = cv2.VideoCapture(camera_index)
                if shared_cap.isOpened():
                    registry.open(shared_cap, handle=camera_id)
            except Exception:
                pass

        return {"source_id": source_id, "status": "stopped"}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _compute_gripper_world_xy(
    tag_dict: dict,
    homography: Optional[np.ndarray],
    offset_cm: float = 14.0,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> Optional[list[float]]:
    """Compute the gripper position in A1-centred world coords for a robot tag.

    Returns [x, y] in world units (A1-centred), or None if homography is
    unavailable or the geometry is degenerate.
    """
    if homography is None or homography.size != 9:
        return None
    try:
        cx, cy = tag_dict["center_px"]
        corners = tag_dict["corners_px"]
        top_mid_px = [
            (corners[0][0] + corners[1][0]) * 0.5,
            (corners[0][1] + corners[1][1]) * 0.5,
        ]
        # Map center and top-mid to raw world coords; direction is unaffected by origin
        cvec = np.array([cx, cy, 1.0])
        cw = homography @ cvec
        cw_xy = np.array([cw[0] / cw[2], cw[1] / cw[2]])

        tvec = np.array([top_mid_px[0], top_mid_px[1], 1.0])
        tw = homography @ tvec
        tw_xy = np.array([tw[0] / tw[2], tw[1] / tw[2]])

        w_dir = tw_xy - cw_xy
        w_norm = float(np.linalg.norm(w_dir))
        if w_norm < 1e-6:
            return None
        w_unit = w_dir / w_norm
        gripper = cw_xy + w_unit * offset_cm
        return [float(gripper[0]) - origin_x, origin_y - float(gripper[1])]
    except Exception:
        return None


def _handle_get_tags(source_id: str) -> dict:
    """Core logic for get_tags — returns tag data dict or error dict."""
    try:
        entry = detection_registry.get(source_id)
        if entry is None:
            return {"error": f"No detection running on '{source_id}'"}

        latest = entry.ring_buffer.get_latest()
        if latest is None:
            return {"source_id": source_id, "frame": None, "tags": []}

        result = latest.to_dict()
        result["source_id"] = source_id

        # Add gripper position for the robot tag if configured
        if entry.robot_tag_id is not None:
            homography = None
            origin_x = 0.0
            origin_y = 0.0
            pf_entry = _resolve_source_playfield(
                source_id, getattr(entry, "_camera_id", None)
            )
            if pf_entry is not None:
                homography = pf_entry.homography
                origin_x, origin_y = _get_playfield_origin(pf_entry)
            for tag in result.get("tags", []):
                if tag["id"] == entry.robot_tag_id:
                    tag["gripper_world_xy"] = _compute_gripper_world_xy(
                        tag, homography, offset_cm=entry.gripper_offset_cm,
                        origin_x=origin_x, origin_y=origin_y,
                    )

        return result
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_pixel_to_world(
    source_id: str,
    pixels: list[list[float]],
) -> dict:
    """Convert pixel coordinates to world coordinates using the source homography."""
    try:
        homography = None
        origin_x = 0.0
        origin_y = 0.0
        try:
            pf_entry = playfield_registry.get(source_id)
            homography = pf_entry.homography
            origin_x, origin_y = _get_playfield_origin(pf_entry)
        except KeyError:
            pass

        if homography is None:
            return {"error": f"No calibrated homography for '{source_id}'"}

        H = np.array(homography, dtype=np.float64).reshape(3, 3)
        world_points = []
        for px in pixels:
            if len(px) < 2:
                world_points.append(None)
                continue
            vec = H @ np.array([float(px[0]), float(px[1]), 1.0])
            if abs(vec[2]) < 1e-9:
                world_points.append(None)
            else:
                world_points.append([
                    float(vec[0] / vec[2]) - origin_x,
                    origin_y - float(vec[1] / vec[2]),
                ])

        return {"source_id": source_id, "world_points": world_points}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_get_tag_history(
    source_id: str,
    num_frames: int = 30,
) -> dict:
    """Core logic for get_tag_history — returns history dict or error dict."""
    try:
        entry = detection_registry.get(source_id)
        if entry is None:
            return {"error": f"No detection running on '{source_id}'"}

        records = entry.ring_buffer.get_last_n(num_frames)
        return {"source_id": source_id, "frames": [r.to_dict() for r in records]}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_get_objects(source_id: str) -> dict:
    """Core logic for get_objects — returns detected non-tag objects or error."""
    try:
        import cv2 as cv
        import math as _math
        from aprilcam.vision.color_classifier import ColorClassifier

        det_entry = detection_registry.get(source_id)
        if det_entry is None:
            return {"error": f"No detection loop on '{source_id}'"}

        frame = det_entry.loop.last_frame
        if frame is None:
            return {"error": "No frames captured yet"}

        # Get homography, field dimensions, and playfield polygon if this source is a playfield.
        homography = None
        pf_poly = None
        origin_x = 0.0
        origin_y = 0.0
        pf_entry = _resolve_source_playfield(
            source_id, getattr(det_entry, "_camera_id", None)
        )
        if pf_entry is not None:
            if pf_entry.homography is not None:
                homography = pf_entry.homography
            try:
                pf_poly = pf_entry.playfield.get_polygon()
            except AttributeError:
                pf_poly = None
            origin_x, origin_y = _get_playfield_origin(pf_entry)

        # Detect colored objects via HSV classification.
        classifier = ColorClassifier(min_area=600, max_area=30000)
        raw = classifier.classify(frame, homography=homography)

        # Filter: inside playfield polygon (inset by 60 px) + roughly square shape.
        objects = []
        shrunk_poly = None
        if pf_poly is not None:
            pts = pf_poly.reshape(-1, 2).astype(np.float32)
            center = pts.mean(axis=0)
            dirs = pts - center
            lens = np.linalg.norm(dirs, axis=1, keepdims=True)
            lens = np.maximum(lens, 1e-6)
            shrunk = pts - dirs / lens * 60
            shrunk_poly = shrunk.reshape(-1, 1, 2).astype(np.float32)

        for obj in raw:
            cx, cy = obj.center_px
            if shrunk_poly is not None:
                if cv.pointPolygonTest(shrunk_poly, (float(cx), float(cy)), False) < 0:
                    continue
            x, y, bw, bh = obj.bbox
            aspect = max(bw, bh) / max(min(bw, bh), 1)
            if aspect > 2.0 or min(bw, bh) < 15:
                continue
            objects.append(obj)

        def _centre(wxy):
            if wxy is None:
                return None
            return [wxy[0] - origin_x, origin_y - wxy[1]]

        return {
            "source_id": source_id,
            "objects": [
                {
                    "center_px": list(o.center_px),
                    "world_xy": _centre(o.world_xy),
                    "color": o.color,
                    "bbox": list(o.bbox),
                    "area_px": o.area_px,
                    "object_type": o.object_type,
                    "confidence": o.confidence,
                }
                for o in objects
            ],
        }
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_where(query: str, source_id: str = "") -> dict:
    """Core logic for the ``where`` tool — natural-language feature lookup.

    Stage 1 runs a keyword search over the static playfield map.  When
    *source_id* names a running detection loop, live tag positions are merged
    into matched tag features.  Stage 2 (the LLM fallback) is signalled by
    ``status == "needs_resolution"``, in which case the full playfield map is
    returned for the agent to resolve.

    The playfield map is loaded from ``playfield_def_registry`` when it is
    populated (new ``data/aprilcam/playfields/`` layout).  When the registry
    is empty (migration not yet run), falls back to the legacy
    ``data/aprilcam/playfield.json`` path for backward compatibility.
    """
    try:
        from aprilcam.core import playfield_query as pq

        # Prefer the registry (new layout); fall back to legacy path.
        pf_def = playfield_def_registry.first()
        if pf_def is not None:
            # Build a playfield dict from the definition so the rest of the
            # handler (iter_features, where, needs_resolution) is unchanged.
            playfield = {
                "playfield": {
                    "width_cm": pf_def.width_cm,
                    "height_cm": pf_def.height_cm,
                    "origin": pf_def.origin,
                },
                "april_tags": pf_def.april_tags,
                "aruco_tags": pf_def.aruco_tags,
                "rectangles": pf_def.rectangles,
                "dots": pf_def.dots,
            }
        else:
            config = Config.load()
            pf_path = pq.default_playfield_path(config.data_dir)
            try:
                playfield = pq.load_playfield(pf_path)
            except (FileNotFoundError, ValueError) as exc:
                return {"error": str(exc)}

        # Merge live detections when a detection source is supplied.
        live_tags: Optional[dict[int, dict]] = None
        if source_id:
            tags_result = _handle_get_tags(source_id)
            if "error" not in tags_result:
                live_tags = {}
                for t in tags_result.get("tags", []) or []:
                    tid = t.get("id")
                    if tid is None:
                        continue
                    live_tags[int(tid)] = {
                        "world_xy": t.get("world_xy"),
                        "in_playfield": t.get("in_playfield"),
                    }
                live_tags = live_tags or None

        result = pq.where(query, pq.iter_features(playfield), live_tags=live_tags)

        if result["status"] == "not_found":
            # Stage 2: hand the whole map back to the agent for LLM resolution.
            result["status"] = "needs_resolution"
            result["playfield"] = playfield
            result["hint"] = (
                "Keyword search found no match. Identify which feature the query "
                "refers to from the 'playfield' map, then call where() again with a "
                "more specific phrase (exact slug, or type + color + cardinal)."
            )
        return result
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_list_playfields() -> dict:
    """Core logic for list_playfields — return registered playfield summaries."""
    try:
        playfields = []
        for n in playfield_def_registry.list():
            d = playfield_def_registry.get(n)
            playfields.append({
                "name": d.name,
                "display_name": d.display_name,
                "width_cm": d.width_cm,
                "height_cm": d.height_cm,
            })
        return {"playfields": playfields}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_get_playfield(name: str = "") -> dict:
    """Core logic for get_playfield — return one full playfield definition.

    Returns the complete structure (dimensions, origin, and every AprilTag,
    ArUco tag, rectangle, and dot) for *name*, or the first/only registered
    playfield when *name* is empty.  Falls back to the legacy single-file
    ``data/aprilcam/playfield.json`` layout when the registry is empty.
    """
    try:
        if name:
            try:
                return playfield_def_registry.get(name).to_dict()
            except KeyError:
                available = ", ".join(playfield_def_registry.list()) or "(none)"
                return {"error": f"Unknown playfield '{name}'. Available: [{available}]"}

        pf_def = playfield_def_registry.first()
        if pf_def is not None:
            return pf_def.to_dict()

        # Legacy fallback: single data/aprilcam/playfield.json (pre-migration).
        from aprilcam.core import playfield_query as pq
        config = Config.load()
        try:
            legacy = pq.load_playfield(pq.default_playfield_path(config.data_dir))
        except (FileNotFoundError, ValueError) as exc:
            return {"error": f"No playfield definitions available: {exc}"}
        return {"name": "playfield", "display_name": "playfield", **legacy}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _warp_points(points: list, homography: np.ndarray) -> list:
    """Transform a list of [x, y] points through a homography matrix."""
    import cv2
    pts = np.array(points, dtype=np.float32).reshape(-1, 1, 2)
    warped = cv2.perspectiveTransform(pts, homography)
    return warped.reshape(-1, 2).tolist()


# Color name to BGR mapping for drawing object annotations
_COLOR_BGR = {
    "red": (0, 0, 255),
    "green": (0, 200, 0),
    "blue": (255, 0, 0),
    "yellow": (0, 255, 255),
    "orange": (0, 165, 255),
    "purple": (255, 0, 255),
    "unknown": (200, 200, 200),
}


def _draw_object_overlay(
    frame: np.ndarray,
    objects: list,
) -> None:
    """Draw object detection overlays directly on *frame* (mutates in place)."""
    import cv2

    for obj in objects:
        x, y, w, h = obj.bbox
        bgr = _COLOR_BGR.get(obj.color, (200, 200, 200))
        cv2.rectangle(frame, (x, y), (x + w, y + h), bgr, 2)
        label = obj.color
        cv2.putText(frame, label, (x, y - 5),
                     cv2.FONT_HERSHEY_SIMPLEX, 0.5, bgr, 1, cv2.LINE_AA)
        if obj.world_xy:
            coord = f"({obj.world_xy[0]:.1f}, {obj.world_xy[1]:.1f})"
            cv2.putText(frame, coord, (x, y + h + 15),
                         cv2.FONT_HERSHEY_SIMPLEX, 0.4, bgr, 1, cv2.LINE_AA)


def _draw_tag_overlay(
    frame: np.ndarray,
    tags: list,
    has_homography: bool = False,
    homography: np.ndarray | None = None,
) -> None:
    """Draw tag detection overlays directly on *frame* (mutates in place).

    When *homography* is provided, tag coordinates (which are in raw
    camera space) are transformed through the homography so they align
    with the deskewed image.

    Draws corner outlines, tag IDs, and world coordinates (when
    *has_homography* is True and ``world_xy`` is present).
    """
    import cv2
    import math

    for tag in tags:
        corners = tag.corners_px
        center = list(tag.center_px)

        # Transform coordinates if we have a homography (raw -> deskewed)
        if homography is not None:
            corners = _warp_points(corners, homography)
            center = _warp_points([center], homography)[0]

        pts = [(int(c[0]), int(c[1])) for c in corners]
        # Draw corner polygon — green top edge, red other sides
        cv2.line(frame, pts[0], pts[1], (0, 255, 0), 2, cv2.LINE_AA)
        cv2.line(frame, pts[1], pts[2], (0, 0, 255), 2, cv2.LINE_AA)
        cv2.line(frame, pts[2], pts[3], (0, 0, 255), 2, cv2.LINE_AA)
        cv2.line(frame, pts[3], pts[0], (0, 0, 255), 2, cv2.LINE_AA)

        # Center dot
        cx, cy = int(center[0]), int(center[1])
        cv2.circle(frame, (cx, cy), 4, (0, 255, 255), -1)

        # ID label
        id_text = str(tag.id)
        (tw, th), _ = cv2.getTextSize(id_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        tx, ty = cx - tw // 2, cy + th // 2
        # Outline for readability
        cv2.putText(frame, id_text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(frame, id_text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)

        # World coordinates label
        if has_homography and tag.world_xy is not None:
            wx, wy = tag.world_xy
            coord_text = f"({wx:.1f}, {wy:.1f})"
            y_bottom = max(p[1] for p in pts)
            cv2.putText(frame, coord_text, (cx - 30, y_bottom + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(frame, coord_text, (cx - 30, y_bottom + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1, cv2.LINE_AA)

        # Velocity arrow
        if tag.vel_px is not None:
            vx, vy = tag.vel_px
            norm = math.hypot(vx, vy)
            if norm > 1e-6:
                length = int(max(12, min(250, norm * 0.5)))
                ux, uy = vx / norm, vy / norm
                end_pt = (int(cx + ux * length), int(cy + uy * length))
                cv2.arrowedLine(frame, (cx, cy), end_pt, (0, 255, 255), 2, tipLength=0.12)


def _handle_get_frame(
    source_id: str,
    format: str = "base64",
    quality: int = 85,
    annotate: bool = False,
) -> dict:
    """Core logic for get_frame — returns image dict or error dict.

    When *annotate* is True and a detection loop is running on the
    source, draws tag overlays (corners, IDs, world coordinates) on
    the frame before encoding.

    Returns:
        ``{"type": "image", "data": ..., "mime": "image/jpeg"}`` for base64,
        ``{"type": "file", "path": ...}`` for file format,
        ``{"type": "error", "error": ...}`` for errors.
    """
    try:
        frame = resolve_source(source_id)
    except KeyError as e:
        return {"type": "error", "error": str(e)}
    except RuntimeError as e:
        return {"type": "error", "error": str(e)}

    # Draw tag overlays if requested
    if annotate:
        det_entry = detection_registry.get(source_id)
        if det_entry is not None:
            latest = det_entry.ring_buffer.get_latest()
            if latest is not None:
                deskew_matrix = None
                has_homography = False
                try:
                    pf_entry = playfield_registry.get(source_id)
                    has_homography = True
                    deskew_matrix = pf_entry.playfield.get_deskew_matrix()
                except KeyError:
                    pass
                _draw_tag_overlay(frame, latest.tags,
                                 has_homography=has_homography,
                                 homography=deskew_matrix)

        # Draw detected objects
        try:
            import cv2 as _cv
            from aprilcam.vision.objects import SquareDetector

            detector = SquareDetector()
            gray_ann = _cv.cvtColor(frame, _cv.COLOR_BGR2GRAY)

            # Get tag corners for exclusion
            tag_corners_for_exclude = []
            if det_entry is not None:
                latest_for_obj = det_entry.ring_buffer.get_latest()
                if latest_for_obj is not None:
                    for tag in latest_for_obj.tags:
                        tag_corners_for_exclude.append(
                            np.array(tag.corners_px, dtype=np.float32)
                        )

            homography = None
            pf_poly_ann = None
            _pf_ann = _resolve_source_playfield(source_id)
            if _pf_ann is not None:
                if _pf_ann.homography is not None:
                    homography = _pf_ann.homography
                try:
                    pf_poly_ann = _pf_ann.playfield.get_polygon()
                except AttributeError:
                    pf_poly_ann = None

            objects = detector.detect(
                gray_ann, homography=homography,
                tag_corners=tag_corners_for_exclude,
                playfield_polygon=pf_poly_ann,
            )
            _draw_object_overlay(frame, objects)
        except Exception:
            pass  # Object annotation is best-effort

    try:
        import cv2

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("Failed to encode frame as JPEG")

        if format == "file":
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(buf.tobytes())
            tmp.close()
            return {"type": "file", "path": tmp.name}

        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        return {"type": "image", "data": b64, "mime": "image/jpeg"}
    except Exception as exc:
        return {"type": "error", "error": f"Unexpected error: {exc}"}


def _handle_start_live_view(
    camera_id: str,
    deskew: bool = True,
    family: str = "36h11",
    proc_width: int = 0,
    use_clahe: bool = False,
    use_sharpen: bool = False,
    robot_tag_id: Optional[int] = None,
    gripper_offset_cm: float = 14.0,
) -> dict:
    """Core logic for start_live_view — returns status dict or error dict.

    Spawns ``aprilcam view <cam_name>`` as a detached subprocess.
    The viewer subscribes to the daemon's data socket directly; no pipe
    plumbing is needed from the MCP server side.
    """
    try:
        if camera_id not in registry._cameras:
            return {"error": f"Unknown camera_id '{camera_id}'"}

        view_id = f"live_{camera_id}"
        if view_id in live_view_registry:
            return {"error": f"Live view already running for '{camera_id}'"}

        # Resolve cam_name from info cache, falling back to camera_id
        info = _cam_info.get(camera_id, {})
        cam_name: str = info.get("cam_name", camera_id)

        # Spawn the viewer as a detached process; it subscribes to the daemon.
        subprocess.Popen(
            [sys.executable, "-m", "aprilcam", "view", cam_name],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Register a lightweight sentinel so stop_live_view can track state.
        live_view_registry[view_id] = LiveViewEntry(
            source_id=view_id,
            process=None,  # type: ignore[arg-type]  # detached, no handle needed
            ring_buffer=None,  # type: ignore[arg-type]
        )

        return {"view_id": view_id, "camera_id": camera_id, "status": "started"}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _handle_stop_live_view(view_id: str) -> dict:
    """Core logic for stop_live_view — returns status dict or error dict."""
    try:
        entry = live_view_registry.pop(view_id, None)
        if entry is None:
            return {"error": f"No live view running with id '{view_id}'"}

        # The viewer process is detached; we have no handle to terminate it.
        # The daemon data socket disconnect will cause it to exit naturally.
        detection_registry.pop(view_id, None)

        return {"view_id": view_id, "status": "stopped"}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _image_result_to_mcp(result: dict) -> list[TextContent | ImageContent]:
    """Convert a plain image result dict to MCP content items.

    Handles the three result types from image-returning handlers:
    - ``{"type": "image", "data": ..., "mime": ...}`` -> ``ImageContent``
    - ``{"type": "file", "path": ...}`` -> ``TextContent`` with JSON path
    - ``{"type": "error", "error": ...}`` -> ``TextContent`` with JSON error
    """
    if result.get("type") == "image":
        return [ImageContent(
            type="image",
            data=result["data"],
            mimeType=result["mime"],
        )]
    elif result.get("type") == "file":
        return [TextContent(
            type="text",
            text=json.dumps({"path": result["path"]}),
        )]
    else:
        # error case
        return [TextContent(
            type="text",
            text=json.dumps({"error": result.get("error", "Unknown error")}),
        )]


# ---------------------------------------------------------------------------
# Tools (thin MCP wrappers)
# ---------------------------------------------------------------------------


@server.tool()
async def get_version() -> list[TextContent]:
    """Return the aprilcam package version.

    Returns:
        A JSON object with ``version`` (str).
    """
    from importlib.metadata import version as _pkg_version

    try:
        ver = _pkg_version("aprilcam")
    except Exception:
        ver = "unknown"
    return [TextContent(type="text", text=json.dumps({"version": ver}))]


@server.tool()
async def list_cameras() -> list[TextContent]:
    """List available cameras by probing indices 0 through 9.

    Returns:
        A JSON array of camera objects, each with ``index`` (int),
        ``name`` (str), and ``backend`` (str). Returns an empty
        array if no cameras are found or an error occurs.
    """
    result = _handle_list_cameras()
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def open_camera(
    index: int | None = None,
    pattern: str | None = None,
    source: str | None = None,
    backend: str | None = None,
) -> list[TextContent]:
    """Open a camera by index, name pattern, or screen capture and return a camera handle.

    Workflow: Start here. The returned camera_id is used by capture_frame,
    create_playfield, start_detection, start_live_view, and set_live_overlay.

    Playfield rehydration (world coordinates): if the camera's data directory
    contains a ``config.json`` linking it to a known playfield definition AND a
    stored ``calibration.json``, open_camera reconstructs the calibrated
    playfield from disk and additionally returns ``playfield_id`` and
    ``playfield_name``. Pass that ``playfield_id`` as the source_id to
    stream_tags / get_tags / get_objects / where so tag and object ``world_xy``
    (cm, A1-centred: origin at AprilTag 1, +x east, +y north) populate. Passing
    the camera_id to those tools also works — the server auto-resolves the
    camera's playfield. If no playfield is configured, only camera_id/cam_name
    come back and world coordinates are null; link one with ``set_camera_playfield``
    then ``calibrate_playfield`` (which needs a playfield definition in
    ``<data_dir>/playfields/``). The server keeps no state across restarts, so
    open_camera must be called in your session before any query.

    If you are writing a robot program that needs high-frequency tag access
    or live overlay drawing, call get_robot_api_guide() first — it shows the
    equivalent DaemonControl Python API for use without MCP at runtime.

    The returned camera_id is a deterministic name derived from the camera
    (e.g. ``"arducam-ov9782-usb-camera"``), not a UUID.

    Args:
        index: Camera device index (default 0 if nothing else is specified).
        pattern: Substring to match against camera names (e.g. ``"FaceTime"``).
        source: Set to ``"screen"`` to capture the desktop instead of a camera.
        backend: OpenCV backend constant name (e.g. ``"CAP_AVFOUNDATION"``).

    Returns:
        On success: ``{"camera_id": "<camera_name>", "cam_name": "<slug>"}``;
        when a configured, calibrated playfield is rehydrated, also
        ``"playfield_id"`` and ``"playfield_name"``, plus ``"calibration_stale":
        true`` when the stored calibration predates the current playfield
        definition.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_open_camera(index=index, pattern=pattern, source=source, backend=backend)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def capture_frame(
    camera_id: str,
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Capture a single frame from an open camera or playfield.

    If *camera_id* refers to a playfield, the frame is automatically deskewed.

    Args:
        camera_id: UUID handle from ``open_camera`` or a playfield_id.
        format: ``"base64"`` (default) returns inline image data;
            ``"file"`` writes a JPEG to a temp file and returns its path.
        quality: JPEG encoding quality (0-100, default 85).

    Returns:
        On success (base64): an ``ImageContent`` with inline JPEG data.
        On success (file): ``{"path": "<temp_file_path>"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_capture_frame(camera_id, format=format, quality=quality)
    return _image_result_to_mcp(result)


@server.tool()
async def close_camera(camera_id: str) -> list[TextContent]:
    """Close a previously-opened camera and release its resources.

    Args:
        camera_id: The UUID handle returned by ``open_camera``.

    Returns:
        On success: ``{"status": "closed"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_close_camera(camera_id)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def set_camera_playfield(
    camera_id: str,
    playfield: str,
) -> list[TextContent]:
    """Link a camera to a named playfield definition.

    Writes data/aprilcam/cameras/<slug>/config.json with {"playfield": "<name>"}.
    The named playfield must exist in the registry.

    This must be called before calibrate_playfield when the camera has no
    existing config.json, or to switch a camera to a different playfield.

    Does not trigger recalibration. The existing calibration (if any) becomes
    stale and will be flagged on the next open_camera.

    Args:
        camera_id: The camera_id from open_camera.
        playfield: The playfield name (slug) to link. Must be a name returned
            by list_playfields (not yet implemented) or known to the operator.

    Returns:
        ``{"camera_id": ..., "playfield": ..., "config_path": ...}`` on success.
        ``{"error": ...}`` on failure.
    """
    result = _handle_set_camera_playfield(camera_id, playfield)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def create_playfield(
    camera_id: str,
    max_frames: int = 30,
) -> list[TextContent]:
    """Create a playfield from a camera by detecting ArUco corner markers.

    Workflow: open_camera → create_playfield.

    Reads up to *max_frames* frames from the camera, looking for four
    ArUco 4x4 corner markers (IDs 0-3). Once all four are found, the
    playfield polygon is established and a playfield_id is returned.

    The returned playfield_id can be used anywhere a camera_id is accepted:
    capture_frame, get_frame, stream_tags, start_live_view, and create_path
    all accept playfield_id as their source/camera argument.

    Calibration behavior:
    - If a stored ``calibration.json`` exists for the camera, it is loaded
      automatically and the response contains ``"calibrated": true``.
      Subsequent ``get_tags`` calls will include ``world_xy`` (x, y in cm)
      for every detected tag.
    - If no stored calibration exists, ``"calibrated": false`` and
      ``world_xy`` will be null in all tag records until you call
      ``calibrate_playfield`` to provide real-world measurements.

    Args:
        camera_id: Camera handle from ``open_camera``.
        max_frames: Maximum number of frames to read while searching
            for corner markers (default 30).

    Returns:
        On success: ``{"playfield_id": "<id>", "corners": [[x,y],...],
        "calibrated": <bool>}``. When ``calibrated`` is true, also includes
        ``"field_width_cm"`` and ``"field_height_cm"``.
        On partial detection: ``{"error": "...", "missing_corner_ids": [...]}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_create_playfield(camera_id, max_frames=max_frames)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def calibrate_playfield(
    playfield_id: str,
    width: Optional[float] = None,
    height: Optional[float] = None,
    units: str = "inch",
    camera_height_cm: float = 0.0,
    camera_x_offset_cm: float = 0.0,
    camera_y_offset_cm: float = 0.0,
) -> list[TextContent]:
    """Calibrate a playfield using the linked playfield definition.

    Workflow: open_camera → calibrate_playfield.

    Delegates to ``calibrate_from_playfield_def``: reads the camera's
    ``config.json`` to find the linked playfield definition, detects the
    corner ArUco markers from the live feed, computes a homography, and
    writes ``calibration.json`` with provenance.  Field dimensions come
    from the definition; any supplied *width* / *height* are ignored
    when a ``config.json`` is present.

    The camera mounting position is stored in ``calibration.json`` under
    ``camera_position`` and is used by the daemon pipeline to apply
    automatic parallax correction for tags elevated above the playfield.

    Args:
        playfield_id: The playfield handle from ``open_camera`` (rehydrated)
            or ``create_playfield``.
        width: Ignored when a ``config.json`` links the camera to a
            playfield definition.  Retained for backward compatibility.
        height: See *width*.
        units: Ignored (dimensions now come from the definition).
        camera_height_cm: Height of the camera above the playfield surface
            in cm (default 0.0, which disables parallax correction).
        camera_x_offset_cm: Horizontal offset of the camera lens from the
            playfield center, in cm (default 0.0, positive = right).
        camera_y_offset_cm: Forward/depth offset of the camera lens from
            the playfield center, in cm (default 0.0, positive = up/forward).

    Returns:
        On success: ``{"playfield_id": "<id>", "calibrated": true,
        "width_cm": ..., "height_cm": ...}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            entry = playfield_registry.get(playfield_id)
        except KeyError:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown playfield_id '{playfield_id}'"}
            ))]

        camera_id = entry.camera_id
        camera_dir_str = _cam_info.get(camera_id, {}).get("camera_dir", "")
        if not camera_dir_str:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"No camera_dir recorded for camera '{camera_id}'"}
            ))]

        camera_dir = Path(camera_dir_str)
        camera_slug = camera_dir.name  # slug == dir name

        try:
            from aprilcam.calibration.calibration import (
                calibrate_from_playfield_def,
                PlayfieldConfigError,
            )
            cap = DaemonCapture(_ensure_daemon_client(), camera_id)
            cal = calibrate_from_playfield_def(
                cap=cap,
                camera_dir=camera_dir,
                camera_slug=camera_slug,
                playfield_def_registry=playfield_def_registry,
                camera_position=CameraPosition(
                    x_offset=camera_x_offset_cm,
                    y_offset=camera_y_offset_cm,
                    height=camera_height_cm,
                ),
            )
        except PlayfieldConfigError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Calibration failed: {exc}"}
            ))]

        # Update the in-memory PlayfieldEntry with the new calibration.
        new_field_spec = FieldSpec(cal.playfield_width_cm, cal.playfield_height_cm, "cm")
        entry.field_spec = new_field_spec
        entry.homography = cal.homography
        entry.tag1_origin_cm = (0.0, 0.0)

        # Refresh the polygon from the new homography.
        from aprilcam.calibration.geometry import corner_pixels_from_homography
        poly = corner_pixels_from_homography(
            cal.homography, cal.playfield_width_cm, cal.playfield_height_cm
        )
        entry.playfield._poly = poly  # type: ignore[attr-defined]

        result: dict = {
            "playfield_id": playfield_id,
            "calibrated": True,
            "width_cm": cal.playfield_width_cm,
            "height_cm": cal.playfield_height_cm,
            "camera_height_cm": camera_height_cm,
            "homography_file": str(camera_dir / "calibration.json"),
        }
        return [TextContent(type="text", text=json.dumps(result))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def create_playfield_from_image(
    image_path: str,
) -> list[TextContent]:
    """Create a playfield from a static image file by detecting ArUco corner markers.

    Reads the image from disk and attempts to detect four ArUco 4x4
    corner markers (IDs 0-3). Useful for testing or working with
    pre-captured images rather than live cameras.

    Args:
        image_path: Absolute path to an image file readable by OpenCV.

    Returns:
        On success: ``{"playfield_id": "<id>", "corners": [[x,y],...], "calibrated": false}``.
        On partial detection: ``{"error": "...", "missing_corner_ids": [...]}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        import cv2

        img = cv2.imread(image_path)
        if img is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to read image file '{image_path}'"}
            ))]

        pf = Playfield(detect_inverted=True, proc_width=0)
        pf.update(img)

        poly = pf.get_polygon()
        if poly is None:
            # Detect which corners are missing
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            dets = detect_aruco_4x4(gray)
            found_ids = [tid for _, tid in dets if tid in (0, 1, 2, 3)]
            missing = [i for i in (0, 1, 2, 3) if i not in found_ids]
            return [TextContent(type="text", text=json.dumps({
                "error": "Failed to detect all 4 corner markers",
                "missing_corner_ids": missing,
            }))]

        playfield_id = f"pf_{uuid.uuid4().hex[:8]}"
        camera_id = f"file:{image_path}"

        entry = PlayfieldEntry(
            playfield_id=playfield_id,
            camera_id=camera_id,
            playfield=pf,
        )
        playfield_registry.register(entry)

        corners = poly.tolist()
        return [TextContent(type="text", text=json.dumps({
            "playfield_id": playfield_id,
            "corners": corners,
            "calibrated": False,
        }))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def deskew_image(
    playfield_id: str,
    image_path: str,
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Read a static image and apply a playfield's deskew (perspective warp) transform.

    Requires playfield_id from ``create_playfield`` or ``create_playfield_from_image``.

    Warps the image to a top-down view using the homography derived
    from the playfield's detected corner markers.

    Args:
        playfield_id: The playfield handle from ``create_playfield`` or
            ``create_playfield_from_image``.
        image_path: Absolute path to an image file readable by OpenCV.
        format: ``"base64"`` (default) or ``"file"``.
        quality: JPEG encoding quality (0-100, default 85).

    Returns:
        On success (base64): an ``ImageContent`` with inline JPEG data.
        On success (file): ``{"path": "<temp_file_path>"}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            entry = playfield_registry.get(playfield_id)
        except KeyError:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown playfield_id '{playfield_id}'"}
            ))]

        import cv2

        img = cv2.imread(image_path)
        if img is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to read image file '{image_path}'"}
            ))]

        deskewed = entry.playfield.deskew(img)

        ok, buf = cv2.imencode(
            ".jpg", deskewed, [cv2.IMWRITE_JPEG_QUALITY, quality]
        )
        if not ok:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Failed to encode deskewed image"}
            ))]

        if format == "file":
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(buf.tobytes())
            tmp.close()
            return [TextContent(type="text", text=json.dumps({"path": tmp.name}))]

        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        return [ImageContent(type="image", data=b64, mimeType="image/jpeg")]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def get_playfield_info(
    playfield_id: str,
) -> list[TextContent]:
    """Return the current state of a registered playfield.

    Args:
        playfield_id: The playfield handle from ``create_playfield`` or
            ``create_playfield_from_image``.

    Returns:
        On success: ``{"playfield_id": ..., "camera_id": ..., "corners": ...,
        "calibrated": bool}``. If calibrated, also includes ``width_cm``,
        ``height_cm``, and ``homography`` (3x3 matrix as nested list).
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_get_playfield_info(playfield_id)
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# Composite tools
# ---------------------------------------------------------------------------


@server.tool()
async def create_composite(
    primary_camera_id: str,
    secondary_camera_id: str,
    playfield_id: str = "",
    correspondence_points: str = "",
) -> list[TextContent]:
    """Create a multi-camera composite by computing cross-camera homography.

    Workflow: open_camera (primary) + open_camera (secondary) → create_composite
    → get_composite_frame / get_composite_tags.

    Maps tag detections from a secondary camera into the primary camera's
    coordinate system. If *correspondence_points* is empty, auto-detects
    shared ArUco markers between both cameras.

    Args:
        primary_camera_id: Camera handle of the primary (color) camera.
        secondary_camera_id: Camera handle of the secondary (e.g. B&W) camera.
        playfield_id: Optional playfield handle for world-coordinate mapping.
        correspondence_points: JSON string of point pairs
            ``[[px1,py1,sx1,sy1], ...]`` (primary x,y then secondary x,y).
            If empty, shared ArUco markers are auto-detected.

    Returns:
        On success: ``{"composite_id": "<id>", "reprojection_error": ...,
        "num_correspondences": ...}``.
        On error: ``{"error": "<message>"}``.
    """
    import cv2

    try:
        if correspondence_points and correspondence_points.strip():
            # Manual correspondence mode
            pairs = json.loads(correspondence_points)
            if not isinstance(pairs, list) or len(pairs) < 4:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Need at least 4 correspondence point pairs"}
                ))]
            primary_pts = np.array([[p[0], p[1]] for p in pairs], dtype=np.float64)
            secondary_pts = np.array([[p[2], p[3]] for p in pairs], dtype=np.float64)
        else:
            # Auto-detect shared ArUco markers
            try:
                cap_pri = registry.get(primary_camera_id)
            except KeyError:
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"Unknown primary camera_id '{primary_camera_id}'"}
                ))]
            try:
                cap_sec = registry.get(secondary_camera_id)
            except KeyError:
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"Unknown secondary camera_id '{secondary_camera_id}'"}
                ))]

            ret1, frame1 = cap_pri.read()
            if not ret1:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Failed to read frame from primary camera"}
                ))]
            ret2, frame2 = cap_sec.read()
            if not ret2:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Failed to read frame from secondary camera"}
                ))]

            gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
            gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)

            dets1 = detect_aruco_4x4(gray1)
            dets2 = detect_aruco_4x4(gray2)

            # Build id->center maps
            map1 = {tid: pts.mean(axis=0) for pts, tid in dets1}
            map2 = {tid: pts.mean(axis=0) for pts, tid in dets2}

            shared_ids = sorted(set(map1.keys()) & set(map2.keys()))
            if len(shared_ids) < 4:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Not enough shared markers for homography",
                    "shared_ids": shared_ids,
                    "primary_ids": sorted(map1.keys()),
                    "secondary_ids": sorted(map2.keys()),
                }))]

            primary_pts = np.array([map1[sid].tolist() for sid in shared_ids], dtype=np.float64)
            secondary_pts = np.array([map2[sid].tolist() for sid in shared_ids], dtype=np.float64)

        H, rms_error = compute_cross_camera_homography(primary_pts, secondary_pts)

        comp = composite_manager.create(
            primary_camera_id=primary_camera_id,
            secondary_camera_id=secondary_camera_id,
            homography=H,
            reprojection_error=rms_error,
            playfield_id=playfield_id if playfield_id else None,
        )

        return [TextContent(type="text", text=json.dumps({
            "composite_id": comp.composite_id,
            "reprojection_error": rms_error,
            "num_correspondences": len(primary_pts),
        }))]
    except (ValueError, json.JSONDecodeError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


def _detect_apriltags_on_frame(frame: np.ndarray) -> list[tuple[np.ndarray, np.ndarray, int]]:
    """Detect AprilTag 36h11 markers on a BGR frame.

    Returns a list of (corners_4x2, raw_corners, tag_id) tuples suitable
    for passing to ``map_tags_to_primary``.
    """
    import cv2

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, params)
    corners, ids, _ = detector.detectMarkers(gray)

    results: list[tuple[np.ndarray, np.ndarray, int]] = []
    if ids is not None and len(ids) > 0:
        for c, tid in zip(corners, ids.flatten()):
            pts = np.array(c, dtype=np.float32).reshape(-1, 2)
            results.append((pts, c, int(tid)))
    return results


def render_tag_overlay(frame: np.ndarray, mapped_tags: list[dict]) -> np.ndarray:
    """Draw tag overlays (polygon + ID label) onto a frame copy.

    Args:
        frame: BGR image (will be copied, not modified in place).
        mapped_tags: list of dicts with ``corners_px`` and ``id`` keys.

    Returns:
        Annotated BGR image.
    """
    import cv2

    out = frame.copy()
    for tag in mapped_tags:
        corners = np.array(tag["corners_px"], dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(out, [corners], isClosed=True, color=(0, 255, 0), thickness=2)
        cx, cy = int(tag["center_px"][0]), int(tag["center_px"][1])
        cv2.putText(
            out, str(tag["id"]),
            (cx - 10, cy - 10),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2,
        )
    return out


@server.tool()
async def get_composite_frame(
    composite_id: str,
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Capture the primary camera frame with secondary-camera tag detections overlaid.

    Requires composite_id from ``create_composite``.

    Reads frames from both cameras, detects AprilTags on the secondary
    frame, maps their positions into the primary camera's coordinate
    system, and draws tag overlays (polygon outline + ID label) on the
    primary frame. Returns only the annotated image; use
    ``get_composite_tags`` to get structured tag records with field values.

    Args:
        composite_id: The composite handle from ``create_composite``.
        format: ``"base64"`` (default) or ``"file"``.
        quality: JPEG encoding quality (0-100, default 85).

    Returns:
        On success (base64): an ``ImageContent`` with the annotated JPEG.
        On success (file): ``{"path": "<temp_file_path>"}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            comp = composite_manager.get(composite_id)
        except KeyError:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown composite_id '{composite_id}'"}
            ))]

        try:
            cap_pri = registry.get(comp.primary_camera_id)
        except KeyError:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Primary camera '{comp.primary_camera_id}' is no longer open"}
            ))]

        try:
            cap_sec = registry.get(comp.secondary_camera_id)
        except KeyError:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Secondary camera '{comp.secondary_camera_id}' is no longer open"}
            ))]

        ret1, frame_pri = cap_pri.read()
        if not ret1:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Failed to read frame from primary camera"}
            ))]

        ret2, frame_sec = cap_sec.read()
        if not ret2:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Failed to read frame from secondary camera"}
            ))]

        # Detect tags on secondary frame
        detections = _detect_apriltags_on_frame(frame_sec)
        mapped = map_tags_to_primary(detections, comp.homography)

        # Overlay on primary frame
        annotated = render_tag_overlay(frame_pri, mapped)

        return format_image_output(annotated, format, quality)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def get_composite_tags(
    composite_id: str,
) -> list[TextContent]:
    """Detect tags on the secondary camera and return positions in primary camera coordinates.

    Requires composite_id from ``create_composite``.

    If the composite has an associated calibrated playfield, each tag
    also includes ``world_xy`` coordinates.

    Args:
        composite_id: The composite handle from ``create_composite``.

    Returns:
        On success: ``{"composite_id": "<id>", "tags": [...]}``. Each tag
        dict contains:
          - ``id``: marker ID (int)
          - ``center_px``: [x, y] position in primary camera coordinates
          - ``corners_px``: list of 4 [x, y] corner points in primary camera coordinates
          - ``world_xy``: [x_cm, y_cm] world position (present only when the
            composite has an associated calibrated playfield; null otherwise)
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            comp = composite_manager.get(composite_id)
        except KeyError:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown composite_id '{composite_id}'"}
            ))]

        try:
            cap_sec = registry.get(comp.secondary_camera_id)
        except KeyError:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Secondary camera '{comp.secondary_camera_id}' is no longer open"}
            ))]

        ret, frame_sec = cap_sec.read()
        if not ret:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Failed to read frame from secondary camera"}
            ))]

        detections = _detect_apriltags_on_frame(frame_sec)
        mapped = map_tags_to_primary(detections, comp.homography)

        # Add world_xy if composite has a calibrated playfield (A1-centred)
        if comp.playfield_id:
            try:
                pf_entry = playfield_registry.get(comp.playfield_id)
                if pf_entry.homography is not None:
                    ox, oy = _get_playfield_origin(pf_entry)
                    for tag in mapped:
                        cx, cy = tag["center_px"]
                        vec = np.array([cx, cy, 1.0], dtype=np.float64)
                        Xw = pf_entry.homography @ vec
                        if abs(Xw[2]) > 1e-9:
                            tag["world_xy"] = [
                                float(Xw[0] / Xw[2]) - ox,
                                oy - float(Xw[1] / Xw[2]),
                            ]
            except KeyError:
                pass  # playfield not found, skip world coords

        return [TextContent(type="text", text=json.dumps({
            "composite_id": composite_id,
            "tags": mapped,
        }))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


# ---------------------------------------------------------------------------
# Detection tools
# ---------------------------------------------------------------------------


@server.tool()
async def start_detection(
    source_id: str,
    family: str = "36h11",
    proc_width: int = 0,
    detect_interval: int = 1,
    use_clahe: bool = False,
    use_sharpen: bool = False,
    robot_tag_id: Optional[int] = None,
    gripper_offset_cm: float = 14.0,
) -> list[TextContent]:
    """Start a persistent tag detection loop on a camera or playfield.

    Prefer ``stream_tags`` over ``start_detection`` for new code.
    ``stream_tags`` has the same effect and records an explicit operations
    pipeline in its metadata. Use ``start_detection`` only for legacy code.

    After starting, poll with ``get_tags``, ``get_tag_history``, or
    ``get_objects``. Stop with ``stop_detection``.

    The loop captures frames continuously, detects AprilTag/ArUco markers
    on each frame, and stores results in a 300-frame ring buffer
    (~10 seconds at 30 fps).

    Args:
        source_id: A camera handle or playfield_id to detect on.
        family: AprilTag family (default ``"36h11"``).
        proc_width: Processing width in pixels; 0 means no downscale.
        detect_interval: Run detection every N frames (default 1).
        use_clahe: Apply CLAHE contrast enhancement before detection.
        use_sharpen: Apply sharpening before detection.
        robot_tag_id: Tag ID of the robot. When set, ``get_tags`` includes
            a ``gripper_world_xy`` field on this tag's record.
        gripper_offset_cm: Distance from the robot tag center to the gripper
            center, in cm along the tag's forward direction (default 14.0).

    Returns:
        On success: ``{"source_id": "<id>", "status": "started"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_start_detection(
        source_id, family=family, proc_width=proc_width,
        detect_interval=detect_interval, use_clahe=use_clahe,
        use_sharpen=use_sharpen, robot_tag_id=robot_tag_id,
        gripper_offset_cm=gripper_offset_cm,
    )
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def stop_detection(source_id: str) -> list[TextContent]:
    """Stop a running tag detection loop and discard its ring buffer.

    This is the counterpart to ``start_detection``. If you used ``stream_tags``,
    call ``stop_stream`` instead.

    Args:
        source_id: The camera handle or playfield_id passed to ``start_detection``.

    Returns:
        On success: ``{"source_id": "<id>", "status": "stopped"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_stop_detection(source_id)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def stream_tags(
    source_id: str,
    operations: list[str] | None = None,
    family: str = "36h11",
    proc_width: int = 0,
    robot_tag_id: Optional[int] = None,
    gripper_offset_cm: float = 14.0,
) -> list[TextContent]:
    """Start continuous tag detection on a camera or playfield with a fixed operation pipeline.

    This is the preferred entry point for starting a detection stream.  It
    wraps the same infrastructure as ``start_detection`` (AprilCam +
    DetectionLoop + RingBuffer) while recording an explicit *operations*
    pipeline for metadata.

    Workflow: open_camera [→ create_playfield] → stream_tags → get_tags /
    get_tag_history / get_objects → stop_stream.

    Use ``stop_stream`` (not ``stop_detection``) to shut down a stream
    started with this tool.

    Args:
        source_id: A camera handle (``cam_N``) or playfield_id to stream from.
        operations: Operation pipeline names to apply each frame.  Stored as
            metadata; the underlying loop currently uses ``AprilCam.process_frame()``.
            Defaults to ``["detect_tags"]``.
        family: AprilTag family (default ``"36h11"``).
        proc_width: Processing width in pixels; 0 means no downscale.
        robot_tag_id: Tag ID of the robot. When set, ``get_tags`` includes
            a ``gripper_world_xy`` field on this tag's record.
        gripper_offset_cm: Distance from the robot tag center to the gripper
            center, in cm along the tag's forward direction (default 14.0).

    Returns:
        On success: ``{"stream_id": "<id>", "operations": [...], "status": "started"}``.
        On error: ``{"error": "<message>"}``.
    """
    if operations is None:
        operations = ["detect_tags"]

    try:
        if source_id in detection_registry:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Detection already running on '{source_id}'"}
            ))]

        import cv2

        # Resolve source to a capture object and optional playfield data
        cap = None
        homography = None
        playfield_poly = None
        camera_id: str | None = None
        camera_index: int | None = None
        exclusive_cap = None

        try:
            pf_entry = playfield_registry.get(source_id)
            camera_id = pf_entry.camera_id
            try:
                cap = registry.get(camera_id)
            except KeyError:
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"Underlying camera '{camera_id}' is no longer open"}
                ))]
            homography = pf_entry.homography
            poly = pf_entry.playfield.get_polygon()
            if poly is not None:
                playfield_poly = poly
        except KeyError:
            # Not a playfield_id — treat source_id as a camera handle.
            camera_id = source_id
            try:
                cap = registry.get(source_id)
            except KeyError:
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"Unknown source_id '{source_id}'"}
                ))]
            # Hardening: use the camera's associated playfield (if any) so tag
            # world_xy is computed even when the caller passes the camera id.
            _assoc = _resolve_source_playfield(source_id)
            if _assoc is not None:
                homography = _assoc.homography
                _assoc_poly = _assoc.playfield.get_polygon()
                if _assoc_poly is not None:
                    playfield_poly = _assoc_poly

        # For real cameras (cam_N handles), open an exclusive capture
        if camera_id and camera_id.startswith("cam_"):
            try:
                camera_index = int(camera_id.split("_", 1)[1])
            except (ValueError, IndexError):
                camera_index = None

            if camera_index is not None:
                try:
                    registry.close(camera_id)
                except KeyError:
                    pass

                exclusive_cap = cv2.VideoCapture(camera_index)
                if exclusive_cap.isOpened():
                    cap = exclusive_cap
                else:
                    exclusive_cap = None
                    try:
                        shared_cap = cv2.VideoCapture(camera_index)
                        if shared_cap.isOpened():
                            registry.open(shared_cap, handle=camera_id)
                            cap = registry.get(camera_id)
                    except Exception:
                        pass

        # Daemon-owned cameras have None in the registry; wrap with DaemonCapture
        if cap is None and camera_id is not None:
            try:
                daemon_client = _ensure_daemon_client()
                cap = DaemonCapture(daemon_client, camera_id)
            except Exception as exc:
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"Cannot reach daemon for camera '{camera_id}': {exc}"}
                ))]

        cam = AprilCam(
            index=camera_index if camera_index is not None else 0,
            backend=None,
            speed_alpha=0.3,
            family=family,
            proc_width=proc_width,
            detect_interval=1,
            use_clahe=False,
            use_sharpen=False,
            headless=True,
            cap=cv2.VideoCapture(),
            homography=homography,
            playfield_poly_init=playfield_poly,
        )

        buf = RingBuffer(maxlen=300)
        coord_transform = None
        _pf = _resolve_source_playfield(source_id, camera_id)
        if _pf is not None:
            ox, oy = _get_playfield_origin(_pf)
            if ox != 0.0 or oy != 0.0:
                coord_transform = _a1_coord_transform(ox, oy)
        loop = DetectionLoop(source=cap, aprilcam=cam, ring_buffer=buf,
                             coord_transform=coord_transform)
        loop.start()

        detection_registry[source_id] = DetectionEntry(
            source_id=source_id,
            loop=loop,
            ring_buffer=buf,
            aprilcam=cam,
            operations=list(operations),
            robot_tag_id=robot_tag_id,
            gripper_offset_cm=gripper_offset_cm,
        )
        detection_registry[source_id]._camera_id = camera_id  # type: ignore[attr-defined]
        detection_registry[source_id]._camera_index = camera_index  # type: ignore[attr-defined]
        detection_registry[source_id]._exclusive_cap = exclusive_cap  # type: ignore[attr-defined]

        return [TextContent(type="text", text=json.dumps(
            {"stream_id": source_id, "operations": operations, "status": "started"}
        ))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def stop_stream(source_id: str) -> list[TextContent]:
    """Stop a running tag detection stream.

    This is the counterpart to ``stream_tags``.  It wraps the same teardown
    logic as ``stop_detection``: stops the loop, releases the exclusive
    capture, and re-opens the shared camera handle.

    If you used ``start_detection``, call ``stop_detection`` instead.

    Args:
        source_id: The source identifier passed to ``stream_tags``.

    Returns:
        On success: ``{"stream_id": "<id>", "status": "stopped"}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        entry = detection_registry.pop(source_id, None)
        if entry is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"No stream running on '{source_id}'"}
            ))]

        entry.loop.stop()

        # Release the exclusive capture and re-open the shared camera
        exclusive_cap = getattr(entry, "_exclusive_cap", None)
        if exclusive_cap is not None:
            try:
                exclusive_cap.release()
            except Exception:
                pass
        camera_id = getattr(entry, "_camera_id", None)
        camera_index = getattr(entry, "_camera_index", 0)
        if camera_id is not None:
            try:
                import cv2
                shared_cap = cv2.VideoCapture(camera_index)
                if shared_cap.isOpened():
                    registry.open(shared_cap, handle=camera_id)
            except Exception:
                pass

        return [TextContent(type="text", text=json.dumps(
            {"stream_id": source_id, "status": "stopped"}
        ))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def get_tags(
    source_id: str,
) -> list[TextContent]:
    """Return the latest tag detections from a running detection loop.

    **Primary tool for tag world coordinates.** When the source is a
    calibrated playfield, each tag record already includes ``world_xy``
    (x, y in cm) in the A1-centred coordinate system (AprilTag 1 at
    origin, x right, y up) — no separate conversion step needed. Use
    ``pixel_to_world`` only when you have raw pixel coordinates from
    somewhere else.

    Angles (``orientation_yaw``, ``heading_rad``) share that frame:
    radians, 0°=+X, counter-clockwise positive (ROS REP-103 "math
    angles"), so a tag's forward direction is ``(cos yaw, sin yaw)`` in
    world coordinates.

    Parallax correction and origin translation are applied automatically
    by the detection pipeline using ``data/aprilcam/tags.json``.

    Requires an active detection loop. Recommended workflow:
    ``open_camera`` → ``create_playfield`` → ``stream_tags`` (preferred) →
    ``get_tags`` (poll as needed) → ``stop_stream``.

    Args:
        source_id: The playfield_id (or camera handle) passed to ``stream_tags``
            (or ``start_detection``).

    Returns:
        On success: ``{"source_id": "<id>", "frame": <int>, "tags": [...]}``.
        Each tag dict contains:
          - ``id``: marker ID (int)
          - ``center_px``: [x, y] pixel position
          - ``corners_px``: list of 4 [x, y] corner points
          - ``world_xy``: [x_cm, y_cm] A1-centred world position, or null if uncalibrated
          - ``orientation_yaw``: tag heading, radians, 0°=+X, CCW positive (forward = (cos, sin))
          - ``vel_px``: [vx, vy] velocity in pixels/s, or null if not yet computed
          - ``in_playfield``: bool, true if the tag center is inside the playfield polygon
          - ``gripper_world_xy``: [x_cm, y_cm] (only present when ``robot_tag_id``
            matches this tag's id and the playfield is calibrated)
        Returns ``{"frame": null, "tags": []}`` if no frames processed yet.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_get_tags(source_id)
    if "error" in result:
        return [TextContent(type="text", text=json.dumps(result))]

    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def pixel_to_world(
    source_id: str,
    pixels: list,
) -> list[TextContent]:
    """Convert one or more pixel coordinates to world coordinates (cm).

    Use this tool only for ad-hoc pixel-to-world conversion of arbitrary
    screen positions. If you want world coordinates for detected tags, use
    ``get_tags`` directly — it already includes ``world_xy`` for each tag
    when the playfield is calibrated.

    Requires the source to have a calibrated homography. Calibration comes
    from ``calibrate_playfield`` or a stored calibration.json loaded
    automatically by ``create_playfield``.

    Args:
        source_id: Playfield or camera ID (the same ID used with ``stream_tags``
            or ``start_detection``).
        pixels: List of ``[x, y]`` pixel coordinates to convert.
            Example: ``[[320, 240], [100, 50]]``

    Returns:
        ``{"source_id": "<id>", "world_points": [[x_cm, y_cm], ...]}``
        Each entry corresponds to the input pixel at the same index.
        Returns ``null`` for any point that could not be projected.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_pixel_to_world(source_id, pixels)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def get_tag_history(
    source_id: str,
    num_frames: int = 30,
) -> list[TextContent]:
    """Return recent tag detection history from a running detection loop's ring buffer.

    Requires an active detection loop. Recommended workflow:
    ``open_camera`` → ``create_playfield`` → ``stream_tags`` (preferred) →
    ``get_tag_history`` → ``stop_stream``.

    Use ``start_detection`` only for legacy code; ``stream_tags`` is preferred.

    Args:
        source_id: The camera handle or playfield_id passed to ``stream_tags``
            (or ``start_detection``).
        num_frames: Number of most-recent frames to return (default 30,
            max 300 which is the ring buffer capacity).

    Returns:
        On success: ``{"source_id": "<id>", "frames": [...]}``. Each frame
        record contains:
          - ``frame``: frame counter (int)
          - ``timestamp``: capture time as a float (seconds since epoch)
          - ``tags``: list of tag dicts, each containing:
              - ``id``: marker ID (int)
              - ``center_px``: [x, y] pixel position
              - ``corners_px``: list of 4 [x, y] corner points
              - ``world_xy``: [x_cm, y_cm] world position, or null if uncalibrated
              - ``orientation_yaw``: tag heading, radians, 0°=+X, CCW positive (forward = (cos, sin))
              - ``vel_px``: [vx, vy] velocity in pixels/s, or null if not yet computed
              - ``in_playfield``: bool
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_get_tag_history(source_id, num_frames=num_frames)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def get_objects(source_id: str) -> list[TextContent]:
    """Return detected non-tag objects from a running detection loop.

    Requires an active detection loop. Recommended workflow:
    ``open_camera`` → ``create_playfield`` → ``stream_tags`` (preferred) →
    ``get_objects`` → ``stop_stream``.

    Use ``start_detection`` only for legacy code; ``stream_tags`` is preferred.

    Runs colored-square detection on the latest frame, excluding regions
    covered by known AprilTag / ArUco markers, and filtering to objects
    inside the playfield polygon (if available). World coordinates are
    included when the source has a calibrated playfield homography.

    Args:
        source_id: The camera handle or playfield_id passed to ``stream_tags``
            (or ``start_detection``).

    Returns:
        On success: ``{"source_id": "<id>", "objects": [...]}``.
        Each object dict contains:
          - ``center_px``: [x, y] pixel center of the detected object
          - ``world_xy``: [x_cm, y_cm] world position, or null if uncalibrated
          - ``color``: dominant color name (str, e.g. ``"red"``, ``"green"``,
            ``"blue"``, ``"yellow"``, ``"orange"``, ``"purple"``, ``"unknown"``)
          - ``bbox``: [x, y, width, height] bounding rectangle in pixels
          - ``area_px``: contour area in square pixels (float)
          - ``object_type``: classifier-assigned type string (e.g. ``"square"``)
          - ``confidence``: detection confidence score (float, 0.0-1.0)
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_get_objects(source_id)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def where(query: str, source_id: str = "") -> list[TextContent]:
    """Find a playfield feature by asking in natural language.

    Answers questions like *"where is the northwest orange dot"*, *"where is
    the eastern red square"*, *"where is the blue dot"*, or *"where is april
    tag one"*.  Features come from the static playfield map
    (``data/aprilcam/playfield.json``): AprilTags, ArUco tags, colored
    rectangles, and colored dots — each with a world position in cm
    (A1-centred: origin at AprilTag 1, +x east, +y north).

    Resolution is two-stage:

    1. **Keyword search.** The query is matched against each feature's
       ``type`` (you can say "april tag" or "aruco tag"), ``color``,
       ``cardinal`` direction, tag name/``id`` and dot ``size``. A handful of
       synonyms are understood (e.g. *square*→rectangle, *eastern*→east,
       *one*→1).
    2. **LLM fallback.** If nothing matches, the tool returns
       ``status="needs_resolution"`` together with the whole ``playfield``
       map. Read it, decide which feature the user meant, and call ``where``
       again with a more specific phrase (an exact ``slug`` works too).

    Args:
        query: The natural-language question, e.g. ``"where is the blue dot"``.
        source_id: Optional playfield/camera id of a running detection loop
            (from ``stream_tags``/``start_detection``). When supplied, the
            live detected position of any matched tag is merged into the
            result as ``live_detection``.

    Returns:
        ``{"status": ..., "query": ..., "tokens": [...], "matches": [...]}``.

          - ``status``: ``"ok"`` (one match), ``"ambiguous"`` (several — pick
            from ``matches`` or refine the query), or ``"needs_resolution"``
            (no keyword match; resolve from the returned ``playfield`` map).
          - Each entry in ``matches`` has ``slug``, ``type``, ``category``,
            ``location`` (``{x, y, units, frame}`` from playfield.json), the
            full ``record``, and — for currently-detected tags —
            ``live_detection`` (``world_xy`` + ``in_playfield``).
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_where(query, source_id)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def list_playfields() -> list[TextContent]:
    """List all named playfield definitions known to the server.

    Playfield definitions live in ``data/aprilcam/playfields/<name>.json`` and
    are loaded at startup. Use this to discover the ``name`` to pass to
    ``get_playfield``.

    Returns:
        On success: ``{"playfields": [{"name", "display_name", "width_cm",
        "height_cm"}, ...]}`` (empty list when none are configured).
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_list_playfields()
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def get_playfield(name: str = "") -> list[TextContent]:
    """Return a playfield's entire structure — every component on it.

    Unlike ``where`` (which searches for a single feature), this returns the
    whole map: field dimensions, coordinate origin, and the full lists of
    AprilTags, ArUco tags, rectangles, and dots — each with its world position
    in cm (origin at AprilTag 1 / playfield center; +x east, +y north).

    Args:
        name: The playfield name (from ``list_playfields``). When omitted,
            returns the first/only registered playfield.

    Returns:
        On success: ``{"name", "display_name", "playfield": {"width_cm",
        "height_cm", "origin"}, "april_tags": [...], "aruco_tags": [...],
        "rectangles": [...], "dots": [...]}``.
        On error: ``{"error": "<message>"}`` (unknown name includes the list of
        available names).
    """
    result = _handle_get_playfield(name)
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# Image processing tools
# ---------------------------------------------------------------------------

_motion_prev_frames: dict[str, Any] = {}


@server.tool()
async def get_frame(
    source_id: str,
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Capture a raw frame from a camera or playfield (no processing applied).

    Args:
        source_id: A camera UUID or playfield_id. If a playfield, the
            frame is automatically deskewed.
        format: ``"base64"`` (default) or ``"file"``.
        quality: JPEG encoding quality (0-100, default 85).

    Returns:
        On success (base64): an ``ImageContent`` with inline JPEG data.
        On success (file): ``{"path": "<temp_file_path>"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_get_frame(source_id, format=format, quality=quality)
    return _image_result_to_mcp(result)


@server.tool()
async def crop_region(
    source_id: str,
    x: int,
    y: int,
    w: int,
    h: int,
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Crop a rectangular region from a camera or playfield frame.

    The crop rectangle is clipped to the frame boundaries. Returns an
    error if the clipped region has zero area.

    Args:
        source_id: A camera UUID or playfield_id.
        x: Left edge of the crop rectangle in pixels.
        y: Top edge of the crop rectangle in pixels.
        w: Width of the crop rectangle in pixels.
        h: Height of the crop rectangle in pixels.
        format: ``"base64"`` (default) or ``"file"``.
        quality: JPEG encoding quality (0-100, default 85).

    Returns:
        On success (base64): an ``ImageContent`` with inline JPEG data.
        On success (file): ``{"path": "<temp_file_path>"}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            frame = resolve_source(source_id)
        except (KeyError, RuntimeError) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        fh, fw = frame.shape[:2]
        # Clip to frame bounds
        x1 = max(0, min(x, fw))
        y1 = max(0, min(y, fh))
        x2 = max(0, min(x + w, fw))
        y2 = max(0, min(y + h, fh))
        if x2 <= x1 or y2 <= y1:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Crop region is entirely outside frame bounds"}
            ))]
        cropped = frame[y1:y2, x1:x2]
        return format_image_output(cropped, format, quality)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def detect_lines(
    source_id: str,
    threshold: int = 50,
    min_length: int = 50,
    max_gap: int = 10,
) -> list[TextContent]:
    """Detect line segments in a frame using probabilistic Hough transform.

    Args:
        source_id: A camera UUID or playfield_id.
        threshold: Hough accumulator threshold (default 50).
        min_length: Minimum line length in pixels (default 50).
        max_gap: Maximum gap between line segments to merge (default 10).

    Returns:
        On success: ``{"source_id": "<id>", "lines": [[x1,y1,x2,y2],...]}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            frame = resolve_source(source_id)
        except (KeyError, RuntimeError) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        # Track frame in registry for pipeline integration
        entry = frame_registry.add(frame, source_id)
        try:
            lines = process_detect_lines(entry.processed, threshold, min_length, max_gap)
            entry.results["detect_lines"] = lines
            entry.operations_applied.append("detect_lines")
            return [TextContent(type="text", text=json.dumps(
                {"source_id": source_id, "lines": lines}
            ))]
        finally:
            frame_registry.release(entry.frame_id)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def detect_circles(
    source_id: str,
    min_radius: int = 0,
    max_radius: int = 0,
    param1: float = 100.0,
    param2: float = 30.0,
) -> list[TextContent]:
    """Detect circles in a frame using Hough circle transform.

    Args:
        source_id: A camera UUID or playfield_id.
        min_radius: Minimum circle radius in pixels (0 = no minimum).
        max_radius: Maximum circle radius in pixels (0 = no maximum).
        param1: Canny edge detector upper threshold (default 100).
        param2: Accumulator threshold for circle centers (default 30).

    Returns:
        On success: ``{"source_id": "<id>", "circles": [{"x":..,"y":..,"radius":..},...]}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            frame = resolve_source(source_id)
        except (KeyError, RuntimeError) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        # Track frame in registry for pipeline integration
        entry = frame_registry.add(frame, source_id)
        try:
            circles = process_detect_circles(entry.processed, min_radius, max_radius, param1, param2)
            entry.results["detect_circles"] = circles
            entry.operations_applied.append("detect_circles")
            return [TextContent(type="text", text=json.dumps(
                {"source_id": source_id, "circles": circles}
            ))]
        finally:
            frame_registry.release(entry.frame_id)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def detect_contours(
    source_id: str,
    min_area: float = 100.0,
) -> list[TextContent]:
    """Detect contours in a frame, filtered by minimum area.

    Args:
        source_id: A camera UUID or playfield_id.
        min_area: Minimum contour area in pixels squared (default 100).
            Contours smaller than this are discarded.

    Returns:
        On success: ``{"source_id": "<id>", "contours": [...]}``. Each
        contour is a list of ``[x, y]`` vertex points.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            frame = resolve_source(source_id)
        except (KeyError, RuntimeError) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        # Track frame in registry for pipeline integration
        entry = frame_registry.add(frame, source_id)
        try:
            contours = process_detect_contours(entry.processed, min_area)
            entry.results["detect_contours"] = contours
            entry.operations_applied.append("detect_contours")
            return [TextContent(type="text", text=json.dumps(
                {"source_id": source_id, "contours": contours}
            ))]
        finally:
            frame_registry.release(entry.frame_id)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def detect_motion(source_id: str) -> list[TextContent]:
    """Detect motion between the current and previous frame using frame differencing.

    The first call for a given source establishes the baseline frame and
    returns ``is_baseline: true`` with no motion regions. Subsequent calls
    compare the current frame against the previous one.

    Args:
        source_id: A camera UUID or playfield_id.

    Returns:
        On success: ``{"source_id": "<id>", "motion_regions": [...],
        "is_baseline": <bool>}``. Each region is a bounding rectangle.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            frame = resolve_source(source_id)
        except (KeyError, RuntimeError) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        import cv2

        from aprilcam.vision.image_processing import process_detect_motion

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        prev = _motion_prev_frames.get(source_id)
        regions = process_detect_motion(frame, prev)
        _motion_prev_frames[source_id] = gray
        return [TextContent(type="text", text=json.dumps(
            {"source_id": source_id, "motion_regions": regions, "is_baseline": prev is None}
        ))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def detect_qr_codes(source_id: str) -> list[TextContent]:
    """Detect and decode QR codes in a frame.

    Args:
        source_id: A camera UUID or playfield_id.

    Returns:
        On success: ``{"source_id": "<id>", "qr_codes": [...]}``. Each
        QR code entry includes the decoded ``data`` string and ``points``
        (corner coordinates).
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            frame = resolve_source(source_id)
        except (KeyError, RuntimeError) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        # Track frame in registry for pipeline integration
        entry = frame_registry.add(frame, source_id)
        try:
            codes = process_detect_qr_codes(entry.processed)
            entry.results["detect_qr"] = codes
            entry.operations_applied.append("detect_qr")
            return [TextContent(type="text", text=json.dumps(
                {"source_id": source_id, "qr_codes": codes}
            ))]
        finally:
            frame_registry.release(entry.frame_id)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


@server.tool()
async def apply_transform(
    source_id: str,
    operation: str,
    params: str = "{}",
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Apply an image transform to a live frame from a camera or playfield.

    Supported operations and their params:
      - ``"rotate"``: rotate the frame by an angle.
        Params: ``{"angle": <degrees>}`` (default 90).
      - ``"scale"``: resize the frame by a scale factor.
        Params: ``{"factor": <float>}`` (default 0.5).
      - ``"threshold"``: convert to grayscale and apply binary threshold.
        Params: ``{"value": <0-255>}`` (default 127).
      - ``"canny"``: apply Canny edge detection.
        Params: ``{"low": <threshold>, "high": <threshold>}`` (defaults 50, 150).
      - ``"blur"``: apply Gaussian blur.
        Params: ``{"kernel_size": <odd int>}`` (default 5).

    Args:
        source_id: A camera UUID or playfield_id.
        operation: The transform operation name: ``"rotate"``, ``"scale"``,
            ``"threshold"``, ``"canny"``, or ``"blur"``.
        params: JSON string with operation-specific parameters
            (e.g. ``'{"angle": 45}'`` for rotate). Defaults to ``"{}"``.
        format: ``"base64"`` (default) or ``"file"``.
        quality: JPEG encoding quality (0-100, default 85).

    Returns:
        On success (base64): an ``ImageContent`` with the transformed JPEG.
        On success (file): ``{"path": "<temp_file_path>"}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        try:
            frame = resolve_source(source_id)
        except (KeyError, RuntimeError) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        import json as _json

        try:
            p = _json.loads(params) if isinstance(params, str) else params
        except Exception:
            p = {}
        from aprilcam.vision.image_processing import process_apply_transform

        try:
            result = process_apply_transform(frame, operation, p)
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        return format_image_output(result, format, quality)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]


# ---------------------------------------------------------------------------
# Live view tools
# ---------------------------------------------------------------------------


@server.tool()
async def start_live_view(
    camera_id: str,
    deskew: bool = True,
    family: str = "36h11",
    proc_width: int = 0,
    use_clahe: bool = False,
    use_sharpen: bool = False,
    robot_tag_id: Optional[int] = None,
    gripper_offset_cm: float = 14.0,
) -> list[TextContent]:
    """Open a live visualization window with tag detection overlays.

    Agent-drawn paths created with ``create_path`` are rendered in the live
    view window each frame.

    The returned view_id is also a valid source_id for ``get_tags`` and
    ``get_tag_history``.

    Spawns a subprocess that opens an OpenCV window showing the camera
    feed with the playfield deskewed to a proportional rectangle.
    Detected tags are drawn with:
    - Green peaked "house" shape indicating the tag's front direction
    - Yellow arrow showing velocity vector
    - Red tag ID number centered on the tag
    - White playfield outline
    - Blue circle at the gripper position (if robot_tag_id is set)

    Detection data also feeds into a ring buffer accessible via
    ``get_tags`` and ``get_tag_history`` using the returned view_id.

    Args:
        camera_id: An open camera handle from ``open_camera`` or a
            playfield_id from ``create_playfield``. When a playfield_id is
            provided, the viewer shows the deskewed playfield view.
        deskew: Warp the playfield to a top-down rectangle (default True).
        family: AprilTag family (default ``"36h11"``).
        proc_width: Processing width for detection downscale (0 = full).
        use_clahe: Apply CLAHE contrast enhancement before detection.
        use_sharpen: Apply sharpening before detection.
        robot_tag_id: Tag ID of the robot. When set, a blue circle is
            drawn forward from this tag along its orientation to indicate
            the gripper center position.
        gripper_offset_cm: Distance from the robot tag center to the
            gripper center, in cm along the tag's forward direction
            (default 14.0).

    Returns:
        On success: ``{"view_id": "<id>", "status": "started"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_start_live_view(
        camera_id, deskew=deskew, family=family, proc_width=proc_width,
        use_clahe=use_clahe, use_sharpen=use_sharpen,
        robot_tag_id=robot_tag_id, gripper_offset_cm=gripper_offset_cm,
    )
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def stop_live_view(view_id: str) -> list[TextContent]:
    """Stop a running live visualization window.

    Args:
        view_id: The view_id returned by ``start_live_view``.

    Returns:
        On success: ``{"view_id": "<id>", "status": "stopped"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_stop_live_view(view_id)
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# Path tool handlers
# ---------------------------------------------------------------------------


def _handle_create_path(playfield_id: str, waypoints_json: str, name: str = "") -> dict:
    """Core logic for create_path — returns result dict or error dict."""
    # 1. Validate playfield exists
    try:
        playfield_registry.get(playfield_id)
    except KeyError:
        return {"error": f"Unknown playfield_id '{playfield_id}'"}

    # 2. Parse waypoints JSON
    try:
        raw = json.loads(waypoints_json)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"Invalid waypoints JSON: {exc}"}

    if not isinstance(raw, list) or len(raw) == 0:
        return {"error": "waypoints must be a non-empty list"}

    # 3-6. Validate each waypoint
    required_keys = {"x", "y", "size_cm", "symbol", "symbol_color", "line_color"}
    waypoints: list[Waypoint] = []
    for wp_dict in raw:
        if not isinstance(wp_dict, dict):
            return {"error": "waypoints must be a non-empty list"}

        missing = required_keys - wp_dict.keys()
        if missing:
            return {"error": f"Waypoint missing required keys: {sorted(missing)}"}

        # 4. Numeric finiteness / positivity
        import math
        for key in ("x", "y", "size_cm"):
            val = wp_dict[key]
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                return {"error": f"'{key}' must be a finite number"}
        if wp_dict["size_cm"] <= 0:
            return {"error": "size_cm must be positive"}

        # 5. Symbol validation
        symbol = wp_dict["symbol"]
        if symbol not in paths_module.VALID_SYMBOLS:
            return {"error": f"Invalid symbol '{symbol}'"}

        # 6. Color validation
        for color_key in ("symbol_color", "line_color"):
            color = wp_dict[color_key]
            if (
                not isinstance(color, (list, tuple))
                or len(color) != 3
                or not all(isinstance(c, int) and 0 <= c <= 255 for c in color)
            ):
                return {"error": f"'{color_key}' must be a list of 3 ints in [0, 255]"}

        waypoints.append(
            Waypoint(
                x=float(wp_dict["x"]),
                y=float(wp_dict["y"]),
                size_cm=float(wp_dict["size_cm"]),
                symbol=symbol,
                symbol_color=tuple(wp_dict["symbol_color"]),
                line_color=tuple(wp_dict["line_color"]),
            )
        )

    path = path_registry.create(playfield_id, waypoints, name=name)

    # Persist current path list to paths.json so the live view subscriber
    # can reload it without IPC.
    _write_paths_json(playfield_id)

    return {"path_id": path.path_id}


def _handle_delete_path(path_id: str) -> dict:
    """Core logic for delete_path — returns result dict or error dict."""
    deleted = path_registry.delete(path_id)
    if deleted is None:
        return {"error": f"Unknown path_id '{path_id}'"}

    # Persist updated path list to paths.json.
    _write_paths_json(deleted.playfield_id)

    return {"deleted": True, "path_id": path_id}


def _handle_list_paths(playfield_id: str) -> dict:
    """Core logic for list_paths — returns result dict or error dict."""
    try:
        playfield_registry.get(playfield_id)
    except KeyError:
        return {"error": f"Unknown playfield_id '{playfield_id}'"}
    paths = path_registry.list_for(playfield_id)
    return {"playfield_id": playfield_id, "paths": [p.to_dict() for p in paths]}


def _handle_clear_paths(playfield_id: str) -> dict:
    """Core logic for clear_paths — returns result dict or error dict."""
    try:
        playfield_registry.get(playfield_id)
    except KeyError:
        return {"error": f"Unknown playfield_id '{playfield_id}'"}
    cleared = path_registry.clear_for(playfield_id)

    # Persist empty path list to paths.json.
    _write_paths_json(playfield_id)

    return {"cleared": cleared}


# ---------------------------------------------------------------------------
# Path MCP tools
# ---------------------------------------------------------------------------


@server.tool()
async def create_path(
    playfield_id: str,
    waypoints_json: str,
    name: str = "",
) -> list[TextContent]:
    """Create an agent-drawn path on a playfield.

    Workflow: open_camera → create_playfield → create_path.

    x and y are world coordinates in cm. Requires a calibrated playfield
    (run ``calibrate_playfield`` first, or use a camera with a stored
    calibration.json); paths are silently invisible on uncalibrated playfields.

    Paths are rendered in the live view window (``start_live_view``) each frame.

    Args:
        playfield_id: The playfield to attach the path to.
        waypoints_json: JSON-encoded list of waypoint dicts.  Each dict must
            contain ``x``, ``y``, ``size_cm``, ``symbol``, ``symbol_color``,
            and ``line_color``.  Colors are ``[R, G, B]`` with values 0-255.
            Valid symbols: square, filled_square, circle, filled_circle,
            triangle, filled_triangle, x, none.

            Example::

                [{"x": 20, "y": 15, "size_cm": 3, "symbol": "filled_circle",
                  "symbol_color": [0, 200, 0], "line_color": [0, 200, 0]},
                 {"x": 60, "y": 45, "size_cm": 3, "symbol": "filled_circle",
                  "symbol_color": [0, 200, 0], "line_color": [0, 200, 0]}]

        name: Optional display label for the path (shown in the viewer panel).
            Defaults to ``""`` (viewer falls back to ``path_id``).

    Returns:
        On success: ``{"path_id": "path_NNN"}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_create_path(playfield_id, waypoints_json, name=name)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def delete_path(path_id: str) -> list[TextContent]:
    """Delete a previously created path by its path_id.

    Use ``list_paths`` to find path_ids for a playfield.

    Args:
        path_id: The path_id returned by ``create_path``.

    Returns:
        On success: ``{"deleted": true, "path_id": "..."}``.
        On error: ``{"error": "Unknown path_id '<id>'"}``.
    """
    result = _handle_delete_path(path_id)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def list_paths(playfield_id: str) -> list[TextContent]:
    """List all paths registered for a playfield.

    Use this to find path_ids for ``delete_path``.

    Args:
        playfield_id: The playfield whose paths should be listed.

    Returns:
        On success: ``{"playfield_id": "...", "paths": [...]}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_list_paths(playfield_id)
    return [TextContent(type="text", text=json.dumps(result))]


@server.tool()
async def clear_paths(playfield_id: str) -> list[TextContent]:
    """Remove all paths for a playfield.

    Args:
        playfield_id: The playfield whose paths should be cleared.

    Returns:
        On success: ``{"cleared": ["path_000", ...]}``.
        On error: ``{"error": "<message>"}``.
    """
    result = _handle_clear_paths(playfield_id)
    return [TextContent(type="text", text=json.dumps(result))]




# ---------------------------------------------------------------------------
# Operation pipeline
# ---------------------------------------------------------------------------

# Canonical set of operations the pipeline understands.
_KNOWN_OPERATIONS = frozenset({
    "deskew",
    "detect_tags",
    "detect_aruco",
    "detect_lines",
    "detect_circles",
    "detect_contours",
    "detect_qr",
})


def _detect_tags_on_frame(frame_bgr: np.ndarray, family: str = "36h11") -> list[dict]:
    """Detect AprilTags on a BGR frame and return JSON-serializable results.

    Uses ``cv2.aruco`` with the AprilTag dictionary corresponding to
    *family* (default ``"36h11"``).  Each result dict contains ``id``,
    ``family``, ``center_px``, ``corners_px``, and ``orientation_yaw``.
    """
    import cv2

    family_map = {
        "36h11": cv2.aruco.DICT_APRILTAG_36h11,
        "25h9": cv2.aruco.DICT_APRILTAG_25h9,
        "16h5": cv2.aruco.DICT_APRILTAG_16h5,
    }
    aruco_dict_id = family_map.get(family, cv2.aruco.DICT_APRILTAG_36h11)
    aruco_dict = cv2.aruco.getPredefinedDictionary(aruco_dict_id)
    params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, params)

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = detector.detectMarkers(gray)

    results: list[dict] = []
    if ids is None or len(ids) == 0:
        return results

    for c, tid in zip(corners, ids.flatten()):
        pts = np.array(c, dtype=np.float32).reshape(-1, 2)
        tag = AprilTag.from_corners(
            tag_id=int(tid),
            corners_px=pts,
            family=family,
        )
        results.append({
            "id": tag.id,
            "family": tag.family,
            "center_px": list(tag.center_px),
            "corners_px": tag.corners_px.tolist(),
            "orientation_yaw": tag.orientation_yaw,
        })
    return results


def _detect_aruco_on_frame(frame_bgr: np.ndarray) -> list[dict]:
    """Detect 4x4 ArUco markers and return JSON-serializable results."""
    import cv2

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    detections = detect_aruco_4x4(gray)
    results: list[dict] = []
    for pts, tid in detections:
        center = pts.mean(axis=0)
        results.append({
            "id": int(tid),
            "center_px": [float(center[0]), float(center[1])],
            "corners_px": pts.tolist(),
        })
    return results


def run_operations(entry: FrameEntry, operations: list[str]) -> dict[str, Any]:
    """Execute a batch of operations on a :class:`FrameEntry` in order.

    Parameters
    ----------
    entry:
        The frame entry whose image slots and results will be mutated.
    operations:
        Ordered list of operation names to run.

    Returns
    -------
    dict
        Combined results keyed by operation name.

    Raises
    ------
    ValueError
        If any operation name is not recognised.
    """
    unknown = [op for op in operations if op not in _KNOWN_OPERATIONS]
    if unknown:
        raise ValueError(
            f"Unknown operation(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(_KNOWN_OPERATIONS))}"
        )

    combined: dict[str, Any] = {}

    for op in operations:
        if op == "deskew":
            combined[op] = _run_deskew(entry)
        elif op == "detect_tags":
            result = _detect_tags_on_frame(entry.processed)
            entry.results["detect_tags"] = result
            entry.apriltags = result
            combined[op] = result
        elif op == "detect_aruco":
            result = _detect_aruco_on_frame(entry.processed)
            entry.results["detect_aruco"] = result
            entry.aruco_corners = {d["id"]: d["corners_px"] for d in result}
            combined[op] = result
        elif op == "detect_lines":
            result = process_detect_lines(entry.processed)
            entry.results["detect_lines"] = result
            combined[op] = result
        elif op == "detect_circles":
            result = process_detect_circles(entry.processed)
            entry.results["detect_circles"] = result
            combined[op] = result
        elif op == "detect_contours":
            result = process_detect_contours(entry.processed)
            entry.results["detect_contours"] = result
            combined[op] = result
        elif op == "detect_qr":
            result = process_detect_qr_codes(entry.processed)
            entry.results["detect_qr"] = result
            combined[op] = result

        entry.operations_applied.append(op)

    return combined


def _run_deskew(entry: FrameEntry) -> dict[str, Any]:
    """Apply deskew to *entry* using its source's playfield, if available."""
    source_id = entry.source

    # Try to find a playfield for this source.
    # For file-based sources, the playfield camera_id is "file:<path>".
    pf_entry = None
    try:
        pf_entry = playfield_registry.get(source_id)
    except KeyError:
        pass

    # Also try find_by_camera in case source is a camera handle
    if pf_entry is None:
        pf_id = playfield_registry.find_by_camera(source_id)
        if pf_id is not None:
            pf_entry = playfield_registry.get(pf_id)

    if pf_entry is None:
        return {"applied": False, "reason": "no playfield for source"}

    warped = pf_entry.playfield.deskew(entry.original)
    h, w = warped.shape[:2]
    entry.deskewed = warped
    entry.processed = entry.deskewed
    entry.is_deskewed = True
    return {"applied": True, "width": w, "height": h}


# ---------------------------------------------------------------------------
# Frame lifecycle tools
# ---------------------------------------------------------------------------


@server.tool()
async def create_frame(
    source_id: str,
    operations: list[str] | None = None,
) -> list[TextContent]:
    """Capture a frame from a camera or playfield and store it in the frame registry.

    Workflow: open_camera [→ create_playfield] → create_frame → process_frame
    → get_frame_image / save_frame → release_frame.

    The frame is stored with three identical image slots (original, deskewed,
    processed).  If *operations* is provided, the operation pipeline runs
    immediately after capture and results are included in the response.

    Args:
        source_id: A camera or playfield handle (e.g. ``"cam_0"``).
        operations: Optional list of pipeline operations to run on the
            frame immediately after capture (e.g.
            ``["deskew", "detect_tags"]``).

    Returns:
        On success: ``{"frame_id": "<id>", "source": "<source_id>"}``
        (plus ``"results"`` when *operations* is provided).
        On error: ``{"error": "<message>"}``.
    """
    try:
        frame = resolve_source(source_id)
    except (KeyError, RuntimeError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    entry = frame_registry.add(raw=frame, source=source_id)
    response: dict[str, Any] = {
        "frame_id": entry.frame_id,
        "source": source_id,
    }

    if operations:
        try:
            results = run_operations(entry, operations)
            response["results"] = results
        except ValueError as exc:
            response["error"] = str(exc)

    return [
        TextContent(
            type="text",
            text=json.dumps(response),
        )
    ]


@server.tool()
async def create_frame_from_image(
    image_path: str,
    operations: list[str] | None = None,
) -> list[TextContent]:
    """Load an image file from disk and store it in the frame registry.

    If *operations* is provided, the operation pipeline runs immediately
    after loading and results are included in the response.

    Args:
        image_path: Absolute path to an image file (JPEG, PNG, etc.).
        operations: Optional list of pipeline operations to run on the
            frame immediately after loading (e.g.
            ``["detect_tags", "detect_lines"]``).

    Returns:
        On success: ``{"frame_id": "<id>", "source": "file:<path>"}``
        (plus ``"results"`` when *operations* is provided).
        On error: ``{"error": "<message>"}``.
    """
    import os

    import cv2

    if not os.path.isfile(image_path):
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": f"File not found: {image_path}"}),
            )
        ]

    img = cv2.imread(image_path)
    if img is None:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": f"Failed to load image: {image_path}"}),
            )
        ]

    source = f"file:{image_path}"
    entry = frame_registry.add(raw=img, source=source)
    response: dict[str, Any] = {
        "frame_id": entry.frame_id,
        "source": source,
    }

    if operations:
        try:
            results = run_operations(entry, operations)
            response["results"] = results
        except ValueError as exc:
            response["error"] = str(exc)

    return [
        TextContent(
            type="text",
            text=json.dumps(response),
        )
    ]


@server.tool()
async def process_frame(
    frame_id: str,
    operations: list[str],
) -> list[TextContent]:
    """Run one or more operations on an existing frame in the registry.

    Requires frame_id from ``create_frame`` or ``create_frame_from_image``.

    Operations execute in order on the frame's ``processed`` image slot.
    Detection operations store structured results without modifying the
    image; the ``deskew`` operation replaces the ``deskewed`` and
    ``processed`` slots with a perspective-warped image.

    Supported operations: ``deskew``, ``detect_tags``, ``detect_aruco``,
    ``detect_lines``, ``detect_circles``, ``detect_contours``,
    ``detect_qr``.

    Args:
        frame_id: The frame handle from ``create_frame`` or
            ``create_frame_from_image``.
        operations: Ordered list of operation names to execute.

    Returns:
        On success: ``{"frame_id": "<id>", "results": {<op>: <data>, ...}}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        entry = frame_registry.get(frame_id)
    except KeyError:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": f"Frame '{frame_id}' not found"}),
            )
        ]

    try:
        results = run_operations(entry, operations)
    except ValueError as exc:
        return [
            TextContent(type="text", text=json.dumps({"error": str(exc)}))
        ]

    return [
        TextContent(
            type="text",
            text=json.dumps({"frame_id": frame_id, "results": results}),
        )
    ]


@server.tool()
async def get_frame_image(
    frame_id: str,
    stage: str = "processed",
    format: str = "base64",
    quality: int = 85,
) -> list[TextContent | ImageContent]:
    """Retrieve an image from a stored frame at the specified processing stage.

    Requires frame_id from ``create_frame`` or ``create_frame_from_image``.

    Args:
        frame_id: The frame handle returned by ``create_frame`` or
            ``create_frame_from_image``.
        stage: Which image slot to return — ``"original"``, ``"deskewed"``,
            or ``"processed"`` (default).
        format: ``"base64"`` (inline image) or ``"file"`` (temp file path).
        quality: JPEG encoding quality (0–100).

    Returns:
        The encoded image, or ``{"error": "<message>"}`` on failure.
    """
    try:
        entry = frame_registry.get(frame_id)
    except KeyError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    stage_map = {
        "original": entry.original,
        "deskewed": entry.deskewed,
        "processed": entry.processed,
    }

    if stage not in stage_map:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": f"Invalid stage '{stage}'. "
                        f"Must be one of: original, deskewed, processed"
                    }
                ),
            )
        ]

    image = stage_map[stage]
    try:
        return format_image_output(image, format=format, quality=quality)
    except RuntimeError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


@server.tool()
async def save_frame(
    frame_id: str,
    output_dir: str,
) -> list[TextContent]:
    """Save all image stages and metadata for a frame to a directory.

    Requires frame_id from ``create_frame`` or ``create_frame_from_image``.

    Creates ``original.jpg``, ``deskewed.jpg``, ``processed.jpg``, and
    ``metadata.json`` in *output_dir*.

    Args:
        frame_id: The frame handle to save.
        output_dir: Directory path where files will be written (created if
            it does not exist).

    Returns:
        On success: ``{"path": "<dir>", "files": [...]}``.
        On error: ``{"error": "<message>"}``.
    """
    import os

    import cv2

    try:
        entry = frame_registry.get(frame_id)
    except KeyError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    os.makedirs(output_dir, exist_ok=True)

    files_written: list[str] = []
    for name, img in [
        ("original.jpg", entry.original),
        ("deskewed.jpg", entry.deskewed),
        ("processed.jpg", entry.processed),
    ]:
        path = os.path.join(output_dir, name)
        cv2.imwrite(path, img)
        files_written.append(name)

    metadata = {
        "frame_id": entry.frame_id,
        "source": entry.source,
        "timestamp": entry.timestamp,
        "operations_applied": list(entry.operations_applied),
        "is_deskewed": entry.is_deskewed,
        "results": entry.results,
    }
    meta_path = os.path.join(output_dir, "metadata.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    files_written.append("metadata.json")

    return [
        TextContent(
            type="text",
            text=json.dumps({"path": output_dir, "files": files_written}),
        )
    ]


@server.tool()
async def release_frame(frame_id: str) -> list[TextContent]:
    """Remove a frame from the registry, freeing its memory.

    Releases memory for a frame_id from ``create_frame`` or
    ``create_frame_from_image``. Call when done with a frame.

    Args:
        frame_id: The frame handle to release.

    Returns:
        On success: ``{"released": true, "frame_id": "<id>"}``.
        On error: ``{"error": "<message>"}``.
    """
    try:
        frame_registry.release(frame_id)
    except KeyError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return [
        TextContent(
            type="text",
            text=json.dumps({"released": True, "frame_id": frame_id}),
        )
    ]


@server.tool()
async def list_frames() -> list[TextContent]:
    """List all frames currently stored in the frame registry.

    Returns:
        A JSON array of frame summary objects, each containing
        ``frame_id``, ``source``, ``timestamp``, ``operations_applied``,
        and ``is_deskewed``.
    """
    summaries = frame_registry.list_frames()
    return [TextContent(type="text", text=json.dumps(summaries))]


# ---------------------------------------------------------------------------
# Live overlay tools
# ---------------------------------------------------------------------------


@server.tool()
async def set_live_overlay(camera_id: str, elements_json: str, ttl: float = 1.0) -> list[TextContent]:
    """Push graphical overlay elements to the live view for a camera.

    Workflow: open_camera → set_live_overlay (no playfield required).

    elements_json: JSON array of element dicts. Each element has:
      type (str): "arc", "arrow", "point", "polyline", "text", "rect", or "polygon"
      params (list[float]): type-specific coordinates in world cm:
        arc:      [cx, cy, radius, start_deg, end_deg]
        arrow:    [x1, y1, x2, y2]
        point:    [x, y, radius_cm]
        polyline: [x0, y0, x1, y1, ...]
        text:     params=[x, y] or [x, y, font_scale]; text field holds the string
        rect:     params=[x1, y1, x2, y2]; thickness=-1 fills
        polygon:  params=[x0, y0, x1, y1, ...]; closed; thickness=-1 fills
      color (list[int]): [R, G, B] each 0-255 (optional, default white)
      thickness (int): line width in pixels; -1 = filled (optional, default 2)
      text (str): string content for "text" type elements (optional for other types)

    ttl: Seconds before the view automatically drops the overlay (default 1.0).
         Call repeatedly at your desired update rate (5-10 Hz for robot state).

    For robot programs that push overlays at 5-50 Hz, call
    get_robot_api_guide() to get the DaemonControl.publish_overlay() Python
    API — it skips MCP entirely and talks directly to the daemon over gRPC.

    Returns "ok" or an error string.
    """
    import json as _json
    try:
        elements = _json.loads(elements_json)
    except _json.JSONDecodeError as exc:
        return [TextContent(type="text", text=f"Error: invalid JSON in elements_json: {exc}")]
    try:
        client = _ensure_daemon_client()
        ok = client.publish_overlay(camera_id, elements, ttl)
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]
    if ok:
        return [TextContent(type="text", text="ok")]
    return [TextContent(type="text", text="Error: overlay not published (camera not found or not streaming)")]


@server.tool()
async def clear_live_overlay(camera_id: str) -> list[TextContent]:
    """Immediately remove the live overlay from the camera's live view.

    Equivalent to calling set_live_overlay with an empty element list and ttl=0.

    Any process with DaemonControl access can also call
    DaemonControl.publish_overlay(camera_id, [], ttl=0) directly.

    Returns "ok" or an error string.
    """
    try:
        client = _ensure_daemon_client()
        ok = client.publish_overlay(camera_id, [], ttl=0)
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]
    if ok:
        return [TextContent(type="text", text="ok")]
    return [TextContent(type="text", text="Error: could not clear overlay (camera not found or not streaming)")]


# ---------------------------------------------------------------------------
# Documentation resources — exposed via MCP Resources protocol so agents
# can read them with list_resources / read_resource, and also via a tool
# for clients that do not support the Resources protocol.
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).parent.parent  # src/aprilcam/


@server.resource("aprilcam://docs/robot-api")
def _resource_robot_api() -> str:
    """Robot Direct API guide — DaemonControl, publish_overlay, tag stream."""
    return (_PACKAGE_DIR / "ROBOT_API_GUIDE.md").read_text()


@server.resource("aprilcam://docs/agent-guide")
def _resource_agent_guide() -> str:
    """AprilCam Agent Guide — MCP tool overview and quick-start examples."""
    return (_PACKAGE_DIR / "AGENT_GUIDE.md").read_text()


@server.tool()
async def get_robot_api_guide() -> list[TextContent]:
    """Return the Robot Direct API guide as text.

    Read this before writing any robot program that needs high-frequency tag
    access or live overlay drawing. The guide covers:

      - DaemonControl: connect, open_camera, get_tags, get_tag_stream,
        get_image_stream, publish_overlay, close
      - Live overlay element types: arc, arrow, point, polyline (world cm coords)
      - Persistent paths: write paths.json directly for waypoint display
      - TagStreamConsumer: reading the multiplexed tag+overlay socket
      - Configuration via Config.load() and environment variables
      - Full 10 Hz robot control-loop code example

    Also available as an MCP Resource at: aprilcam://docs/robot-api

    For agents using the MCP tools interactively, the robot guide shows the
    equivalent Python API you can hand off to a robot program so it can
    operate autonomously without MCP at runtime.
    """
    return [TextContent(type="text", text=(_PACKAGE_DIR / "ROBOT_API_GUIDE.md").read_text())]


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server on stdio transport.

    On shutdown, all detection loops are stopped and all open cameras
    are released, regardless of whether the server exits cleanly or
    due to an exception.

    Args:
        argv: Unused; accepted for CLI entry-point compatibility.
    """
    cfg = Config.load()
    playfield_def_registry.load_all(cfg.playfields_dir)
    try:
        server.run(transport="stdio")
    finally:
        # Stop all live views first
        for entry in list(live_view_registry.values()):
            try:
                entry.process.stop()
            except Exception:
                pass
        live_view_registry.clear()
        # Stop all detection loops before closing cameras
        for entry in list(detection_registry.values()):
            try:
                entry.loop.stop()
            except Exception:
                pass
        detection_registry.clear()
        registry.close_all()


if __name__ == "__main__":
    main()
