"""CLI subcommand: aprilcam view — Live view via the AprilCam daemon."""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Optional


class CollapsibleFrame(tk.Frame):
    """A tk.Frame with a clickable header that hides/shows its content sub-frame.

    Parameters
    ----------
    parent:
        Parent widget.
    title:
        Section title displayed in the header row.
    bg:
        Background color for the frame and header.
    header_fg:
        Foreground color for the title text.
    on_expand:
        Callable invoked (no args) when the section is expanded.
    on_collapse:
        Callable invoked (no args) when the section is collapsed.
    """

    def __init__(
        self,
        parent,
        title: str,
        bg: str = "#1e1e1e",
        header_fg: str = "#aaaaaa",
        on_expand=None,
        on_collapse=None,
        expandable: bool = True,
        start_collapsed: bool = False,
        **kwargs,
    ):
        super().__init__(parent, bg=bg, **kwargs)
        self._expanded = True
        self._expandable = expandable
        self._on_expand = on_expand
        self._on_collapse = on_collapse

        # Header row
        self._header = tk.Frame(self, bg=bg, cursor="hand2")
        self._header.grid(row=0, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)

        self._toggle_lbl = tk.Label(
            self._header, text="▼", bg=bg, fg=header_fg,
            font=("Helvetica", 10, "bold"),
        )
        self._toggle_lbl.pack(side=tk.LEFT, padx=(4, 2))

        self._title_lbl = tk.Label(
            self._header, text=title, bg=bg, fg=header_fg,
            font=("Helvetica", 10, "bold"), anchor="w",
        )
        self._title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Content frame — callers pack/grid their widgets into this
        self.content = tk.Frame(self, bg=bg)
        self.content.grid(row=1, column=0, sticky="nsew")
        self.rowconfigure(1, weight=1)

        # Bind click on both header widgets
        for w in (self._header, self._toggle_lbl, self._title_lbl):
            w.bind("<Button-1>", lambda _e: self.toggle())

        # Optionally start in the collapsed state (content hidden). Callbacks
        # are not fired here — the caller owns the initial side effects.
        if start_collapsed:
            self.content.grid_remove()
            self._toggle_lbl.config(text="▶")
            self._expanded = False

    def toggle(self):
        """Toggle expanded/collapsed state."""
        if self._expanded:
            self.content.grid_remove()
            self._toggle_lbl.config(text="▶")
            self._expanded = False
            self.pack_configure(expand=False, fill=tk.X)
            if self._on_collapse:
                self._on_collapse()
        else:
            self.content.grid()
            self._toggle_lbl.config(text="▼")
            self._expanded = True
            if self._expandable:
                self.pack_configure(expand=True, fill=tk.BOTH)
            if self._on_expand:
                self._on_expand()


_OBJ_BGR: dict[str, tuple[int, int, int]] = {
    "black":   (80,  80,  80),
    "red":     (0,   0,   220),
    "orange":  (0,   128, 255),
    "yellow":  (0,   220, 220),
    "green":   (0,   200, 0),
    "blue":    (220, 100, 0),
    "purple":  (180, 0,   180),
    "magenta": (200, 0,   200),
}


def _draw_object_boxes(img: "np.ndarray", objects: list) -> None:
    import cv2 as _cv
    for obj in objects:
        x, y, w, h = obj.bbox
        bgr = _OBJ_BGR.get(obj.color, (180, 180, 180))
        _cv.rectangle(img, (x, y), (x + w, y + h), bgr, 2)
        label = obj.color
        if obj.world_xy is not None:
            label += f" ({obj.world_xy[0]:.0f},{obj.world_xy[1]:.0f})"
        _cv.putText(img, label, (x, max(y - 4, 12)),
                    _cv.FONT_HERSHEY_SIMPLEX, 0.45, bgr, 1, _cv.LINE_AA)


def _tag_record_to_dict(tag_record) -> dict:
    """Convert a TagRecord Pydantic model to a legacy-format dict for display helpers.

    The panel formatters and _tag_dict_to_aprilcam() were written against the
    old FrameMessage.tags dict schema.  This shim maps TagRecord fields to
    the expected keys so the display code needs no further changes.
    """
    vel_px_raw = tag_record.vel_px
    vel_world_raw = tag_record.vel_world
    world_xy = tag_record.world_xy

    # corners_px in TagRecord is list[tuple[float, float]]; flatten to list[list]
    corners = [[c[0], c[1]] for c in tag_record.corners_px]

    return {
        "id": tag_record.id,
        "center_px": list(tag_record.center_px),
        "corners_px": corners,
        "orientation_yaw": tag_record.yaw,
        "world_xy": list(world_xy) if world_xy is not None else None,
        "in_playfield": tag_record.in_playfield,
        "vel_px": list(vel_px_raw) if vel_px_raw is not None else [0.0, 0.0],
        "vel_world": list(vel_world_raw) if vel_world_raw is not None else None,
    }


def _tag_dict_to_aprilcam(tag_dict: dict):
    """Convert a TagRecord dict into an AprilTag object."""
    import numpy as np
    from aprilcam.core.models import AprilTag

    corners_raw = tag_dict.get("corners_px", [[0, 0]] * 4)
    corners_px = np.array(corners_raw, dtype=np.float32)

    center_raw = tag_dict.get("center_px", [0.0, 0.0])
    center_px = (float(center_raw[0]), float(center_raw[1]))

    c = corners_px.mean(axis=0)
    p0, p1 = corners_px[0], corners_px[1]
    top_mid = (p0 + p1) / 2.0
    n = top_mid - c
    n_norm = float(np.linalg.norm(n))
    if n_norm > 1e-6:
        top_dir_px = (float(n[0]) / n_norm, float(n[1]) / n_norm)
    else:
        top_dir_px = (1.0, 0.0)

    orientation_yaw = float(tag_dict.get("orientation_yaw", 0.0))

    world_raw = tag_dict.get("world_xy")
    world_xy = (float(world_raw[0]), float(world_raw[1])) if world_raw is not None else None

    tag = AprilTag(
        id=int(tag_dict["id"]),
        family="36h11",
        corners_px=corners_px,
        center_px=center_px,
        top_dir_px=top_dir_px,
        orientation_yaw=orientation_yaw,
        world_xy=world_xy,
        in_playfield=bool(tag_dict.get("in_playfield", False)),
    )

    vel_raw = tag_dict.get("vel_px")
    tag.vel_px = (float(vel_raw[0]), float(vel_raw[1])) if vel_raw is not None else (0.0, 0.0)

    return tag


def _load_paths(paths_file: Path) -> dict:
    try:
        raw = json.loads(paths_file.read_text())
    except Exception:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        result = {}
        for item in raw:
            if isinstance(item, dict):
                pid = item.get("path_id") or item.get("id") or str(len(result))
                result[str(pid)] = item
        return result
    return {}


def _decode_frame(frame_bytes, np, cv):
    if isinstance(frame_bytes, (bytes, bytearray)):
        buf = np.frombuffer(frame_bytes, dtype=np.uint8)
    else:
        buf = np.array(frame_bytes, dtype=np.uint8)
    return cv.imdecode(buf, cv.IMREAD_COLOR)


# ── Tag panel formatting ─────────────────────────────────────────────────────

def _vel_mag(t: dict) -> float:
    vp = t.get("vel_px", [0, 0])
    return math.hypot(float(vp[0]), float(vp[1]))


# Mobility classification tuning.
_VEL_THRESHOLD = 1.0       # px/s — above this a tag counts as moving this frame
_PROMOTE_FRAMES = 10       # consecutive over-threshold frames before stamping
_STILL_TIMEOUT_S = 10.0    # revert mobile -> stationary after this long with no movement


def _classify_tag_mobility(
    raw_tags: list[dict],
    vel_counts: dict[int, int],
    last_moving: dict[int, float],
    now: float,
    *,
    vel_threshold: float = _VEL_THRESHOLD,
    promote_frames: int = _PROMOTE_FRAMES,
    still_timeout_s: float = _STILL_TIMEOUT_S,
) -> tuple[list[dict], list[dict]]:
    """Partition *raw_tags* into ``(mobile, stationary)`` lists.

    A tag is **mobile** when it is over *vel_threshold* this frame, or it moved
    within the last *still_timeout_s* seconds; otherwise it is **stationary**.
    The last-movement time is stamped only after *promote_frames* consecutive
    over-threshold frames, so a single jittery frame shows mobile for just that
    frame rather than pinning a still tag to mobile for the whole timeout.

    *vel_counts* and *last_moving* are per-tag state dicts, mutated in place so
    state persists across calls.  *now* is a monotonic timestamp (seconds).
    """
    for t in raw_tags:
        tid = int(t.get("id", -1))
        if _vel_mag(t) > vel_threshold:
            vel_counts[tid] = vel_counts.get(tid, 0) + 1
            if vel_counts[tid] >= promote_frames:
                last_moving[tid] = now
        else:
            vel_counts[tid] = 0

    mobile: list[dict] = []
    stationary: list[dict] = []
    for t in sorted(raw_tags, key=lambda x: int(x.get("id", 0))):
        tid = int(t.get("id", -1))
        last = last_moving.get(tid)
        recently_moved = last is not None and (now - last) < still_timeout_s
        if recently_moved or _vel_mag(t) > vel_threshold:
            mobile.append(t)
        else:
            stationary.append(t)
    return mobile, stationary



_MOB_HDR = (
    f"{'ID':>2} {'PxX':>4} {'PxY':>4} {'WldX':>6} {'WldY':>6} {'Ang':>4} {'VelX':>5} {'VelY':>5}\n"
    + "-" * 43 + "\n"
)

_STAT_HDR = (
    f"{'ID':>2} {'PxX':>4} {'PxY':>4} {'WldX':>6} {'WldY':>6} {'Ang':>4}\n"
    + "-" * 31 + "\n"
)


def _fmt_mobile_row(t: dict) -> str:
    tid = int(t.get("id", 0))
    cx, cy = t.get("center_px", [0, 0])
    wxy = t.get("world_xy")
    wx = f"{float(wxy[0]):6.1f}" if wxy else "    --"
    wy = f"{float(wxy[1]):6.1f}" if wxy else "    --"
    ang = math.degrees(float(t.get("orientation_yaw", 0.0)))
    vw = t.get("vel_world")
    vp = t.get("vel_px", [0, 0])
    vx, vy = (float(vw[0]), float(vw[1])) if vw is not None else (float(vp[0]), float(vp[1]))
    return f"{tid:>2} {int(cx):>4} {int(cy):>4} {wx} {wy} {ang:>4.0f} {vx:>5.1f} {vy:>5.1f}\n"


def _fmt_stat_row(t: dict) -> str:
    tid = int(t.get("id", 0))
    cx, cy = t.get("center_px", [0, 0])
    wxy = t.get("world_xy")
    wx = f"{float(wxy[0]):6.1f}" if wxy else "    --"
    wy = f"{float(wxy[1]):6.1f}" if wxy else "    --"
    ang = math.degrees(float(t.get("orientation_yaw", 0.0)))
    return f"{tid:>2} {int(cx):>4} {int(cy):>4} {wx} {wy} {ang:>4.0f}\n"


def _display_enum(registry, cam_name: Optional[str], enum_no: Optional[int]) -> Optional[int]:
    """Resolve the enumeration number to show in the viewer's "Camera" status row.

    The status panel must display the stable enumeration number the user typed
    (the ``#`` shown by ``aprilcam cameras``), never the raw live OS index.

    * When the user selected by number, ``enum_no`` is that number and is
      returned directly.
    * When the user selected by name, the daemon returns the registry-assigned
      per-camera dir as ``cam_name``; look up the record whose ``dir`` matches
      and return its ``enum``.
    * When it cannot be determined, return ``None`` (the caller shows ``--``).

    ``registry`` is any object exposing a ``records()`` iterable of objects with
    ``dir`` and ``enum`` attributes (a :class:`CameraRegistry`); it is passed in
    rather than constructed here so this helper is pure and unit-testable.
    """
    if enum_no is not None:
        return enum_no
    if registry is None or not cam_name:
        return None
    for record in registry.records():
        if getattr(record, "dir", None) == cam_name:
            return getattr(record, "enum", None)
    return None


# ── main ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aprilcam view",
        description="Open a live view window fed by the AprilCam daemon",
    )
    parser.add_argument(
        "camera",
        metavar="CAMERA",
        help="Camera name or number (the # shown by `aprilcam cameras`)",
    )
    parser.add_argument(
        "--unix-path",
        default=None,
        metavar="PATH",
        help="Unix socket path for the daemon control socket",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=None,
        metavar="N",
        help="TCP port the daemon is listening on",
    )
    args = parser.parse_args(argv)

    import numpy as np
    import cv2 as cv

    from aprilcam.config import Config
    from aprilcam.client.control import DaemonControl
    from aprilcam.core.playfield import PlayfieldBoundary
    from aprilcam.ui.display import PlayfieldDisplay
    from aprilcam.camera.identity import resolve_all
    from aprilcam.camera.registry import (
        CameraRegistry,
        CameraSelectError,
        resolve_enum_to_index,
    )

    config = Config.load()
    dc = DaemonControl.connect_default(
        config, unix_path=args.unix_path, tcp_port=args.tcp_port
    )

    cam_name: Optional[str] = None
    cam_index: Optional[int] = None
    enum_no: Optional[int] = None
    _camera_dir: Optional[str] = None
    try:
        camera_arg = args.camera
        try:
            # An integer CAMERA is the stable enumeration number shown by
            # `aprilcam cameras` — resolve it to the live OS index before
            # opening, so the number the user types matches the listing.
            enum_no = int(camera_arg)
            registry = CameraRegistry(config.cameras_dir)
            try:
                cam_index = resolve_enum_to_index(enum_no, registry, resolve_all())
            except CameraSelectError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                dc.close()
                return 1
            cam_name, _camera_dir = dc.open_camera(cam_index)
        except ValueError:
            # camera_arg is a name, not an index — verify it is open
            info = dc.get_camera_info(camera_arg)
            cam_name = info.cam_name

        if cam_name is None:
            print(f"Error: could not resolve camera '{camera_arg}'", file=sys.stderr)
            dc.close()
            return 1

    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        dc.close()
        return 1

    # Open image and tag streams
    try:
        image_consumer = dc.get_image_stream(cam_name)
        tag_consumer = dc.get_tag_stream(cam_name)
    except Exception as exc:
        print(f"Error: could not open streams for camera '{cam_name}': {exc}", file=sys.stderr)
        dc.close()
        return 1
    finally:
        # DaemonControl is no longer needed after streams are open
        dc.close()

    # Read first image frame
    try:
        first_frame = image_consumer.read()
    except (EOFError, RuntimeError) as exc:
        print(f"Error: could not read first frame: {exc}", file=sys.stderr)
        image_consumer.close()
        tag_consumer.close()
        return 1

    # Read first tag frame (non-blocking with a short timeout via separate read)
    first_tag_frame = None
    try:
        first_tag_frame = tag_consumer.read()
    except (EOFError, RuntimeError):
        pass

    _DISPLAY_W = 1000  # canvas is always this wide; height scales proportionally

    boundary = PlayfieldBoundary(
        px_per_cm=config.deskew_px_per_cm,
        movement_threshold_px=config.movement_threshold_px,
        static_mode=None if config.static_deskew else False,
    )
    # Load the camera's calibration so optional pre-warp undistortion
    # (APRILCAM_UNDISTORT) can flatten residual barrel curvature in the
    # deskewed view; a no-op when intrinsics are absent.
    _view_cal = None
    try:
        from aprilcam.calibration.calibration import load_calibration_from_camera_dir
        _cam_dir = Path(_camera_dir) if _camera_dir else (config.cameras_dir / cam_name)
        _view_cal = load_calibration_from_camera_dir(_cam_dir)
    except Exception:
        _view_cal = None
    display = PlayfieldDisplay(
        playfield=boundary,
        window_name="aprilcam view",
        deskew_overlay=True,
        calibration=_view_cal,
        undistort=config.undistort,
    )

    from aprilcam.client.models import TagFrame

    # State shared between reader threads and main (Tk) thread
    _latest_tag_frame: list = [first_tag_frame]  # mutable container for thread sharing
    _tag_lock = threading.Lock()
    _latest_overlay: list = [None]
    _overlay_lock = threading.Lock()

    _detect_objects = threading.Event()
    _latest_objects: list = [[]]
    _obj_lock = threading.Lock()
    _classifier_holder: list = [None]  # lazy-init ColorClassifier
    _show_paths: list = [True]  # mutable container; render loop checks [0]

    _paths_file = (
        Path(_camera_dir) / "paths.json"
        if _camera_dir
        else config.cameras_dir / cam_name / "paths.json"
    )

    def _process_frame_and_tags(frame_bgr: "np.ndarray", tag_frame):
        """Apply tag overlay to frame_bgr; return (disp, status_dict, raw_tags_dicts)."""
        nonlocal boundary

        if tag_frame is not None and tag_frame.playfield_corners:
            raw_corners = tag_frame.playfield_corners
            if len(raw_corners) == 4:
                poly = np.array([[c[0], c[1]] for c in raw_corners], dtype=np.float32)
                boundary.polygon = poly
                boundary._poly = poly
                display._update_deskew(frame_bgr)

        homography: Optional[np.ndarray] = None
        if tag_frame is not None and tag_frame.homography is not None:
            try:
                homography = np.array(tag_frame.homography, dtype=np.float64)
                if homography.shape != (3, 3):
                    homography = None
            except Exception:
                homography = None

        disp = display.prepare_display(frame_bgr)

        raw_tags_dicts: list[dict] = []
        tags = []
        if tag_frame is not None:
            for tr in tag_frame.tags:
                try:
                    td = _tag_record_to_dict(tr)
                    raw_tags_dicts.append(td)
                    tags.append(_tag_dict_to_aprilcam(td))
                except Exception:
                    pass

        # Origin offsets for inverting the daemon's A1-centred world transform.
        # MUST match the origin the daemon applied to world_xy (it prefers the
        # AprilTag-1 world position, not the geometric field centre), so the
        # daemon now publishes it explicitly.  Fall back to half the field
        # dimensions for older daemons that don't send origin_x/origin_y.
        origin_x = 0.0
        origin_y = 0.0
        if tag_frame is not None:
            if tag_frame.origin_x or tag_frame.origin_y:
                origin_x = tag_frame.origin_x
                origin_y = tag_frame.origin_y
            elif tag_frame.field_width_cm > 0:
                origin_x = tag_frame.field_width_cm / 2.0
                origin_y = tag_frame.field_height_cm / 2.0

        display.draw_overlays(disp, tags, homography,
                              origin_x=origin_x, origin_y=origin_y)

        paths = _load_paths(_paths_file)
        if paths and _show_paths[0]:
            display.draw_paths(disp, paths, boundary, homography,
                               origin_x=origin_x, origin_y=origin_y)

        with _overlay_lock:
            overlay = _latest_overlay[0]
        if overlay is not None and homography is not None:
            display.draw_live_overlay(disp, overlay, homography,
                                      origin_x=origin_x, origin_y=origin_y)

        if _detect_objects.is_set():
            color_clf = _classifier_holder[0]
            if color_clf is not None:
                # Build containment polygon from the disp frame dimensions.
                # boundary._poly is in original camera coords which may not match
                # the deskewed disp frame, so we use the frame bounds instead.
                dh_f, dw_f = disp.shape[:2]
                INSET = 60
                shrunk_poly = np.array([
                    [INSET, INSET],
                    [dw_f - INSET, INSET],
                    [dw_f - INSET, dh_f - INSET],
                    [INSET, dh_f - INSET],
                ], dtype=np.float32).reshape(-1, 1, 2)

                from dataclasses import replace as _dc_replace
                raw_objs = color_clf.classify(disp, homography=homography)
                detected = []
                for obj in raw_objs:
                    cx, cy = obj.center_px
                    if cv.pointPolygonTest(shrunk_poly, (float(cx), float(cy)), False) < 0:
                        continue
                    x, y, bw, bh = obj.bbox
                    aspect = max(bw, bh) / max(min(bw, bh), 1)
                    if aspect > 2.0 or min(bw, bh) < 15 or max(bw, bh) > 200:
                        continue
                    if obj.world_xy is not None:
                        rx, ry = obj.world_xy
                        obj = _dc_replace(obj, world_xy=(rx - origin_x, origin_y - ry))
                    detected.append(obj)
                _draw_object_boxes(disp, detected)
                with _obj_lock:
                    _latest_objects[0] = detected

        fps_val = tag_frame.fps if tag_frame is not None else 0.0
        calibrated = homography is not None
        deskew_mode = getattr(display, "_mode", "full") == "deskew"
        status_dict = {
            "fps": fps_val,
            "tag_count": len(tags),
            "calibrated": calibrated,
            "deskew_mode": deskew_mode,
        }
        return disp, status_dict, raw_tags_dicts

    first_disp, first_status, first_raw_tags = _process_frame_and_tags(
        first_frame, first_tag_frame
    )

    # Compute initial canvas height from the first display frame's aspect ratio
    _dh, _dw = first_disp.shape[:2]
    _display_h = int(round(_dh * _DISPLAY_W / _dw))

    frame_queue: queue.Queue = queue.Queue(maxsize=2)
    stop_event = threading.Event()
    frame_queue.put_nowait((first_disp, first_status, first_raw_tags))

    def _image_reader_thread():
        """Continuously read image frames and push processed results to frame_queue."""
        while not stop_event.is_set():
            try:
                frame_bgr = image_consumer.read()
            except (EOFError, RuntimeError):
                print("Image stream closed — daemon may have stopped.", file=sys.stderr)
                stop_event.set()
                break

            with _tag_lock:
                current_tag_frame = _latest_tag_frame[0]

            disp, status_dict, raw_tags = _process_frame_and_tags(
                frame_bgr, current_tag_frame
            )
            try:
                frame_queue.put_nowait((disp, status_dict, raw_tags))
            except queue.Full:
                pass

    def _tag_reader_thread():
        """Continuously read tag frames and update _latest_tag_frame or _latest_overlay."""
        while not stop_event.is_set():
            try:
                msg = tag_consumer.read()
                if isinstance(msg, TagFrame):
                    with _tag_lock:
                        _latest_tag_frame[0] = msg
                else:  # OverlayFrame proto
                    with _overlay_lock:
                        _latest_overlay[0] = msg
            except (EOFError, RuntimeError):
                stop_event.set()
                break

    image_reader = threading.Thread(target=_image_reader_thread, daemon=True)
    tag_reader = threading.Thread(target=_tag_reader_thread, daemon=True)

    # ── Build tkinter window ──────────────────────────────────────────────
    import tkinter.font as tkfont
    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title(f"aprilcam view — {cam_name}")
    root.configure(bg="#111")
    root.resizable(False, True)

    # Top-level split: left (video, fixed size) | right (info panel, expands)
    left_frame = tk.Frame(root, bg="#111")
    left_frame.pack(side=tk.LEFT, fill=tk.Y)

    right_frame = tk.Frame(root, bg="#1e1e1e")
    right_frame.pack(side=tk.LEFT, fill=tk.Y)  # fixed width — no horizontal expansion

    # ── Left: canvas — always DISPLAY_W wide, height proportional ────────
    canvas = tk.Canvas(
        left_frame, width=_DISPLAY_W, height=_display_h,
        bg="black", highlightthickness=0,
    )
    canvas.pack()
    img_item = canvas.create_image(0, 0, anchor=tk.NW)

    # ── Right panel layout ────────────────────────────────────────────────
    mono = tkfont.Font(family="Courier", size=11)
    label_font = ("Helvetica", 10)
    value_font = ("Helvetica", 10, "bold")
    PANEL_BG = "#1e1e1e"
    FG = "#dddddd"
    MOB_FG = "#ffcc44"
    STAT_FG = "#88ccff"

    # ── Status block (top of right panel) ────────────────────────────────
    cf_status = CollapsibleFrame(right_frame, title="Camera Status", bg=PANEL_BG, header_fg="#aaaaaa", expandable=False)
    cf_status.pack(fill=tk.X, padx=8, pady=(8, 4))
    status_frame = cf_status.content

    def _kv_row(parent, row, key, init="--"):
        tk.Label(parent, text=key, font=label_font, fg="#aaaaaa", bg=PANEL_BG,
                 anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=0)
        var = tk.StringVar(value=init)
        tk.Label(parent, textvariable=var, font=value_font, fg=FG, bg=PANEL_BG,
                 anchor="w").grid(row=row, column=1, sticky="w", pady=0)
        return var

    # Show the stable enumeration number (what the user typed / `aprilcam
    # cameras` prints), never the raw live OS index.
    display_enum = _display_enum(
        CameraRegistry(config.cameras_dir), cam_name, enum_no
    )
    var_idx = _kv_row(status_frame, 0, "Camera", init=str(display_enum) if display_enum is not None else "--")
    var_name = _kv_row(status_frame, 1, "Name", init=cam_name or "--")
    var_fps = _kv_row(status_frame, 2, "FPS")
    var_tags = _kv_row(status_frame, 3, "Tags")
    var_cal = _kv_row(status_frame, 4, "Calibrated")
    var_deskew = _kv_row(status_frame, 5, "Deskew")

    # ── Mobile tags section ───────────────────────────────────────────────
    cf_mob = CollapsibleFrame(right_frame, title="Mobile Tags", bg=PANEL_BG, header_fg=MOB_FG)
    cf_mob.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 2))
    mob_frame = cf_mob.content

    mobile_text = tk.Text(
        mob_frame, font=mono, bg="#111", fg=MOB_FG,
        state=tk.DISABLED, height=8, width=44,
        relief=tk.FLAT, padx=4, pady=2, wrap=tk.NONE,
    )
    mob_sb = tk.Scrollbar(mob_frame, command=mobile_text.yview)
    mobile_text.configure(yscrollcommand=mob_sb.set)
    mob_sb.pack(side=tk.RIGHT, fill=tk.Y)
    mobile_text.pack(fill=tk.BOTH, expand=True)

    # ── Stationary tags section ───────────────────────────────────────────
    cf_stat = CollapsibleFrame(right_frame, title="Stationary Tags", bg=PANEL_BG, header_fg=STAT_FG)
    cf_stat.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))
    stat_outer = cf_stat.content

    stat_text = tk.Text(
        stat_outer, font=mono, bg="#111", fg=STAT_FG,
        state=tk.DISABLED, height=8, width=44,
        relief=tk.FLAT, padx=4, pady=2, wrap=tk.NONE,
    )
    stat_sb = tk.Scrollbar(stat_outer, command=stat_text.yview)
    stat_text.configure(yscrollcommand=stat_sb.set)
    stat_sb.pack(side=tk.RIGHT, fill=tk.Y)
    stat_text.pack(fill=tk.BOTH, expand=True)

    # ── Object detection collapsible panel ───────────────────────────────
    OBJ_FG = "#44ff88"

    def _lazy_start_objects():
        if _classifier_holder[0] is None:
            from aprilcam.vision.color_classifier import ColorClassifier
            _classifier_holder[0] = ColorClassifier(min_area=400, max_area=8000)
        _detect_objects.set()

    def _on_obj_collapse():
        _detect_objects.clear()
        with _obj_lock:
            _latest_objects[0] = []

    cf_obj = CollapsibleFrame(
        right_frame, title="Objects", bg=PANEL_BG, header_fg=OBJ_FG,
        on_collapse=_on_obj_collapse,
        on_expand=_lazy_start_objects,
        start_collapsed=True,
    )
    # Object detection is OFF by default: the panel starts collapsed and the
    # detection thread only runs once the user expands it (on_expand handler).
    cf_obj.pack(fill=tk.X, expand=False, padx=8, pady=(4, 2))
    obj_outer = cf_obj.content

    obj_text = tk.Text(
        obj_outer, font=mono, bg="#111", fg=OBJ_FG,
        state=tk.DISABLED, height=6, width=44,
        relief=tk.FLAT, padx=4, pady=2, wrap=tk.NONE,
    )
    obj_sb = tk.Scrollbar(obj_outer, command=obj_text.yview)
    obj_text.configure(yscrollcommand=obj_sb.set)
    obj_sb.pack(side=tk.RIGHT, fill=tk.Y)
    obj_text.pack(fill=tk.BOTH, expand=True)

    # ── Paths section ─────────────────────────────────────────────────────
    PATH_FG = "#88aaff"

    def _on_paths_collapse():
        _show_paths[0] = False

    def _on_paths_expand():
        _show_paths[0] = True

    cf_paths = CollapsibleFrame(
        right_frame, title="Paths", bg=PANEL_BG, header_fg=PATH_FG,
        on_collapse=_on_paths_collapse,
        on_expand=_on_paths_expand,
    )
    cf_paths.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

    def _clear_all_paths():
        tmp = _paths_file.with_suffix(".tmp")
        try:
            _paths_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("[]")
            os.replace(tmp, _paths_file)
        except Exception:
            pass
        _refresh_paths()

    tk.Button(
        cf_paths._header,
        text="Clear all",
        font=("Helvetica", 8),
        bg="#000000", fg="#cc4444",
        activebackground="#1a0000", activeforeground="#ff4444",
        relief=tk.FLAT, bd=0, highlightthickness=0,
        padx=4, pady=1,
        command=_clear_all_paths,
    ).pack(side=tk.RIGHT, padx=(0, 6))

    # Inner scrollable container for path rows
    paths_canvas = tk.Canvas(cf_paths.content, bg=PANEL_BG, highlightthickness=0, height=120)
    paths_vsb = tk.Scrollbar(cf_paths.content, orient=tk.VERTICAL, command=paths_canvas.yview)
    paths_canvas.configure(yscrollcommand=paths_vsb.set)
    paths_vsb.pack(side=tk.RIGHT, fill=tk.Y)
    paths_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    paths_inner = tk.Frame(paths_canvas, bg=PANEL_BG)
    paths_canvas_window = paths_canvas.create_window((0, 0), window=paths_inner, anchor="nw")

    def _on_paths_inner_configure(event):
        paths_canvas.configure(scrollregion=paths_canvas.bbox("all"))
        paths_canvas.itemconfig(paths_canvas_window, width=paths_canvas.winfo_width())

    paths_inner.bind("<Configure>", _on_paths_inner_configure)
    paths_canvas.bind("<Configure>", lambda e: paths_canvas.itemconfig(
        paths_canvas_window, width=e.width
    ))

    # ── Paths helpers ─────────────────────────────────────────────────────

    def _rgb_to_hex(rgb) -> str:
        """Convert an RGB list/tuple to a Tkinter hex color string."""
        r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_symbol_on_canvas(c: tk.Canvas, symbol: str, color_hex: str) -> None:
        """Draw the given symbol on a 16x16 canvas."""
        c.delete("all")
        pad = 2
        s = 16 - 2 * pad
        cx, cy = 8, 8
        if symbol == "square":
            c.create_rectangle(pad, pad, pad + s, pad + s, outline=color_hex, width=1)
        elif symbol == "filled_square":
            c.create_rectangle(pad, pad, pad + s, pad + s, fill=color_hex, outline="")
        elif symbol == "circle":
            c.create_oval(pad, pad, pad + s, pad + s, outline=color_hex, width=1)
        elif symbol == "filled_circle":
            c.create_oval(pad, pad, pad + s, pad + s, fill=color_hex, outline="")
        elif symbol in ("triangle", "filled_triangle"):
            pts = [cx, pad, pad, pad + s, pad + s, pad + s]
            if symbol == "triangle":
                c.create_polygon(pts, outline=color_hex, fill="", width=1)
            else:
                c.create_polygon(pts, fill=color_hex, outline="")
        elif symbol == "x":
            c.create_line(pad, pad, pad + s, pad + s, fill=color_hex, width=1)
            c.create_line(pad + s, pad, pad, pad + s, fill=color_hex, width=1)
        # symbol == "none": draw nothing

    def _delete_path(path_id: str) -> None:
        """Remove path_id from paths.json, keeping list format. Then refresh."""
        try:
            raw = json.loads(_paths_file.read_text())
        except Exception:
            raw = []
        if isinstance(raw, dict):
            # normalize old dict format to list
            raw = list(raw.values())
        remaining = [
            item for item in raw
            if isinstance(item, dict)
            and (item.get("path_id") or item.get("id")) != path_id
        ]
        tmp = _paths_file.with_suffix(".tmp")
        try:
            _paths_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(remaining, indent=2))
            os.replace(tmp, _paths_file)
        except Exception:
            pass
        _refresh_paths()

    def _refresh_paths() -> None:
        """Rebuild the Paths panel rows from _load_paths()."""
        for w in paths_inner.winfo_children():
            w.destroy()

        paths = _load_paths(_paths_file)
        if not paths:
            tk.Label(
                paths_inner, text="(no paths)", bg=PANEL_BG, fg="#666666",
                font=("Helvetica", 9),
            ).pack(anchor="w", padx=6, pady=2)
            return

        for path_id, path_dict in sorted(paths.items()):
            wps = path_dict.get("waypoints", [])
            first_wp = wps[0] if wps else {}
            sym = first_wp.get("symbol", "none")
            sym_color = first_wp.get("symbol_color", [180, 180, 180])
            line_color = first_wp.get("line_color", [180, 180, 180])
            label_text = path_dict.get("name") or path_id

            row = tk.Frame(paths_inner, bg=PANEL_BG)
            row.pack(fill=tk.X, padx=4, pady=1)

            # Symbol preview canvas
            sym_canvas = tk.Canvas(row, width=16, height=16, bg=PANEL_BG,
                                   highlightthickness=0)
            sym_canvas.pack(side=tk.LEFT, padx=(0, 3))
            _draw_symbol_on_canvas(sym_canvas, sym, _rgb_to_hex(sym_color))

            # Line color swatch
            swatch = tk.Canvas(row, width=20, height=4, bg=PANEL_BG,
                               highlightthickness=0)
            swatch.pack(side=tk.LEFT, padx=(0, 6))
            swatch.create_rectangle(0, 0, 20, 4, fill=_rgb_to_hex(line_color), outline="")

            # Name/id label
            tk.Label(
                row, text=label_text, bg=PANEL_BG, fg=PATH_FG,
                font=("Helvetica", 9), anchor="w",
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)

            # Delete button
            del_btn = tk.Button(
                row,
                text="✕",
                font=("Helvetica", 9, "bold"),
                bg="#000000", fg="#cc4444",
                activebackground="#1a0000", activeforeground="#ff4444",
                relief=tk.FLAT, bd=0, highlightthickness=0,
                padx=4, pady=1,
                command=lambda pid=path_id: _delete_path(pid),
            )
            del_btn.pack(side=tk.RIGHT, padx=(4, 0))

    # ── Mobility tracking (main-thread only) ──────────────────────────────
    # Per-tag state for _classify_tag_mobility: a tag stays "mobile" until it
    # has been still for _STILL_TIMEOUT_S seconds, then reverts to "stationary".
    _vel_counts: dict[int, int] = {}
    _last_moving: dict[int, float] = {}

    def _set_text(widget, text: str) -> None:
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.config(state=tk.DISABLED)

    def _update_tag_panel(raw_tags: list[dict]) -> None:
        mobile, stationary = _classify_tag_mobility(
            raw_tags, _vel_counts, _last_moving, time.monotonic()
        )

        mob_str = _MOB_HDR + "".join(_fmt_mobile_row(t) for t in mobile) if mobile else "(none)\n"
        st_str = _STAT_HDR + "".join(_fmt_stat_row(t) for t in stationary) if stationary else "(none)\n"
        _set_text(mobile_text, mob_str)
        _set_text(stat_text, st_str)

    # ── Window close handlers ─────────────────────────────────────────────
    def _on_close() -> None:
        stop_event.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.bind("<q>", lambda _e: _on_close())
    root.bind("<Escape>", lambda _e: _on_close())

    # ── Poll callback (tkinter main thread) ───────────────────────────────
    def _poll() -> None:
        if stop_event.is_set():
            try:
                root.destroy()
            except tk.TclError:
                pass
            return

        try:
            frame_bgr, status_dict, raw_tags = frame_queue.get_nowait()
        except queue.Empty:
            root.after(33, _poll)
            return

        # Scale to fixed display width, maintaining aspect ratio
        fh, fw = frame_bgr.shape[:2]
        dh = int(round(fh * _DISPLAY_W / fw))
        frame_disp = cv.resize(frame_bgr, (_DISPLAY_W, dh), interpolation=cv.INTER_AREA)

        # Resize canvas + window if the display height changed (e.g. homography engaged)
        if abs(dh - canvas.winfo_height()) > 2:
            canvas.config(height=dh)
            root.update_idletasks()
            _locked_w = _DISPLAY_W + right_frame.winfo_reqwidth()
            root.geometry(f"{_locked_w}x{root.winfo_reqheight()}")

        rgb = cv.cvtColor(frame_disp, cv.COLOR_BGR2RGB)
        photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        canvas.itemconfig(img_item, image=photo)
        canvas._photo_ref = photo

        fps_val = status_dict.get("fps")
        var_fps.set(f"{fps_val:.1f}" if isinstance(fps_val, (int, float)) else "--")
        var_tags.set(str(status_dict.get("tag_count", 0)))
        var_cal.set("Yes" if status_dict.get("calibrated") else "No")
        var_deskew.set("On" if status_dict.get("deskew_mode") else "Off")

        _update_tag_panel(raw_tags)

        with _obj_lock:
            current_objects = list(_latest_objects[0])
        if current_objects:
            _OBJ_HDR = f"{'Color':<8} {'PxX':>4} {'PxY':>4} {'WldX':>6} {'WldY':>6}\n" + "-" * 34 + "\n"
            rows = []
            for obj in current_objects:
                cx, cy = obj.center_px
                wx = f"{obj.world_xy[0]:6.1f}" if obj.world_xy else "    --"
                wy = f"{obj.world_xy[1]:6.1f}" if obj.world_xy else "    --"
                rows.append(f"{obj.color:<8} {int(cx):>4} {int(cy):>4} {wx} {wy}\n")
            _set_text(obj_text, _OBJ_HDR + "".join(rows))
        elif _detect_objects.is_set():
            _set_text(obj_text, "(none detected)\n")
        else:
            _set_text(obj_text, "(detection off)\n")

        _refresh_paths()

        root.after(33, _poll)

    # Snap window to exact content size: locked width = canvas + right panel.
    root.update_idletasks()
    _locked_w = _DISPLAY_W + right_frame.winfo_reqwidth()
    root.geometry(f"{_locked_w}x{_display_h}")
    root.resizable(False, True)

    image_reader.start()
    tag_reader.start()
    root.after(33, _poll)

    try:
        root.mainloop()
    finally:
        stop_event.set()
        try:
            image_consumer.close()
        except Exception:
            pass
        try:
            tag_consumer.close()
        except Exception:
            pass

    return 0
