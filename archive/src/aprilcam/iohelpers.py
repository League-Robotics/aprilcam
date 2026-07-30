from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import json

import numpy as np
import cv2 as cv

from .config import AppConfig


def resolve_data_path(p: str | Path) -> Path:
    cfg = None
    try:
        cfg = AppConfig.load()
    except Exception:
        cfg = None
    path = Path(p)
    if cfg and not path.is_absolute():
        path = cfg.data_dir / path
    return path


def get_data_dir() -> Path:
    """Return the project's data directory from AppConfig."""
    cfg = None
    try:
        cfg = AppConfig.load()
    except Exception:
        cfg = None
    if cfg is None:
        # Fallback: current working dir / data
        return Path.cwd() / "data"
    return cfg.data_dir


def load_homography(homography: str | Path) -> Tuple[Optional[np.ndarray], Optional[Dict[str, Any]], Optional[list]]:
    H_path = resolve_data_path(homography)
    H = None
    H_meta = None
    H_pix = None
    try:
        if H_path.exists():
            data = json.loads(H_path.read_text())
            H = np.array(data.get("homography", []), dtype=float) if data.get("homography") is not None else None
            H_meta = data.get("source")
            H_pix = data.get("pixel_points")
    except Exception:
        H, H_meta, H_pix = None, None, None
    return H, H_meta, H_pix


def open_source_from_meta(H_meta: Dict[str, Any], quiet: bool = True):
    """Return an opened capture object (cv.VideoCapture-like or ScreenCaptureMSS) based on homography metadata."""
    from .config import AppConfig
    cfg = None
    try:
        cfg = AppConfig.load()
    except Exception:
        cfg = None

    if not H_meta:
        return None
    src_type = str(H_meta.get("type", "camera"))
    if src_type == "screen":
        region = H_meta.get("region")
        try:
            from .camera.screencap import ScreenCaptureMSS
            cap = ScreenCaptureMSS(
                monitor=int(H_meta.get("monitor", 1)),
                fps=float(H_meta.get("fps", 30.0)),
                region=tuple(region) if region else None,
            )
            return cap
        except Exception:
            return None
    else:
        cam_idx = H_meta.get("index")
        cap = cfg.get_camera(arg=int(cam_idx) if cam_idx is not None else None, backend=str(H_meta.get("backend", "auto")), quiet=bool(quiet)) if cfg else None
        if cap and cap.isOpened():
            cw = H_meta.get("cap_width")
            ch = H_meta.get("cap_height")
            if cw:
                cap.set(cv.CAP_PROP_FRAME_WIDTH, int(cw))
            if ch:
                cap.set(cv.CAP_PROP_FRAME_HEIGHT, int(ch))
            return cap
        return None
