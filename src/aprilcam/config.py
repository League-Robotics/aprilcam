from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import dotenv_values
import json
import numpy as np


@dataclass
class AppConfig:
    """
    Loads configuration from a .env file discovered by walking up from CWD.

    Required vars in .env:
      - JTL_APRILCAM=1  (guard to ensure the correct environment)
      - ROOT_DIR=<path|envdir>  (root of project; if 'envdir', use parent of .env file)

    Derived:
      - root_dir: Path
      - data_dir: Path (defaults to <root_dir>/data)
    """

    env_path: Path
    env: Dict[str, str]
    root_dir: Path
    data_dir: Path

    @staticmethod
    def find_env(start: Optional[Path] = None) -> Path:
        start = start or Path.cwd()
        cur = start.resolve()
        while True:
            candidate = cur / ".env"
            if candidate.exists():
                return candidate
            if cur.parent == cur:
                raise FileNotFoundError("No .env file found when walking up from CWD")
            cur = cur.parent

    @classmethod
    def load(cls, start: Optional[Path] = None) -> "AppConfig":
        env_path = cls.find_env(start)
        env_map: Dict[str, str] = {k: v for k, v in dotenv_values(env_path).items() if v is not None}

        # Guard variable required
        if env_map.get("JTL_APRILCAM") != "1":
            raise RuntimeError(".env missing required JTL_APRILCAM=1")

        # Resolve ROOT_DIR
        raw_root = env_map.get("ROOT_DIR")
        env_dir = env_path.parent
        if not raw_root or raw_root.strip().lower() == "envdir":
            root_dir = env_dir
        else:
            p = Path(raw_root)
            root_dir = p if p.is_absolute() else (env_dir / p)
        root_dir = root_dir.resolve()

        # Data directory (default ROOT_DIR/data)
        data_dir = Path(env_map.get("DATA_DIR", str(root_dir / "data"))).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        return cls(env_path=env_path, env=env_map, root_dir=root_dir, data_dir=data_dir)

    # --- Camera helpers ---
    def get_camera(
        self,
        arg: Optional[object] = None,
        *,
        backend: Optional[str] = None,
        max_cams: int = 10,
        quiet: bool = False,
    ):
        """
        Resolve a camera from --camera arg (int or pattern) or .env CAMERA, open and return cv.VideoCapture.

        - arg may be int or str; if str and numeric, it's treated as an index; otherwise substring pattern.
        - backend can be one of: None/"auto", "avfoundation", "v4l2", "msmf", "dshow".
        - Falls back to the first available camera if none specified.
        Returns an opened VideoCapture or None on failure.
        """
        import cv2 as cv  # noqa: PLC0415
        from .camera.camutil import (  # noqa: PLC0415
            list_cameras,
            default_backends,
            select_camera_by_pattern,
        )
        # Decode backend string to API preference
        be_map = {
            None: None,
            "auto": None,
            "avfoundation": getattr(cv, "CAP_AVFOUNDATION", 1200),
            "v4l2": getattr(cv, "CAP_V4L2", 200),
            "msmf": getattr(cv, "CAP_MSMF", 1400),
            "dshow": getattr(cv, "CAP_DSHOW", 700),
        }
        be_value = be_map.get(backend if backend is None or isinstance(backend, str) else None)
        # Choose backends list for enumeration
        backends = None if be_value is None else [be_value, getattr(cv, "CAP_ANY", 0)]
        # Parse input
        index: Optional[int] = None
        pattern: Optional[str] = None
        if arg is not None:
            if isinstance(arg, int):
                index = int(arg)
            else:
                s = str(arg).strip()
                try:
                    index = int(s)
                except ValueError:
                    pattern = s
        else:
            cam_env = self.env.get("CAMERA")
            if cam_env:
                s = str(cam_env).strip()
                try:
                    index = int(s)
                except ValueError:
                    pattern = s

        # If we have an index, try to open directly
        if index is not None:
            cap = cv.VideoCapture(int(index), 0 if be_value is None else int(be_value))
            if cap.isOpened():
                return cap
            cap.release()

        # Else enumerate and select by pattern or pick first
        cams = list_cameras(max_index=max_cams, backends=backends, quiet=quiet)
        if pattern:
            sel = select_camera_by_pattern(pattern, cams)
            if sel is not None:
                cap = cv.VideoCapture(int(sel), 0 if be_value is None else int(be_value))
                if cap.isOpened():
                    return cap
                cap.release()
        # Fallback to first camera if any
        if cams:
            cap = cv.VideoCapture(int(cams[0].index), 0 if be_value is None else int(be_value))
            if cap.isOpened():
                return cap
            cap.release()
        return None

    # --- Homography helpers ---
    def load_homography(
        self,
        path: Optional[Path] = None,
        device_name: Optional[str] = None,
        resolution: Optional[tuple[int, int]] = None,
    ) -> Optional[np.ndarray]:
        """Load a 3x3 homography matrix from JSON.

        When *device_name* and *resolution* are provided, uses
        :func:`~aprilcam.homography.discover_homography` to find the
        best matching file (per-camera first, then global fallback).
        Otherwise defaults to ``<DATA_DIR>/homography.json``.

        Returns numpy array (float64) or None if not found/invalid.
        """
        if path is not None:
            p = Path(path)
        elif device_name is not None and resolution is not None:
            from .calibration.homography import discover_homography

            found = discover_homography(
                device_name, resolution[0], resolution[1], self.data_dir
            )
            if found is None:
                return None
            p = found
        else:
            p = self.data_dir / "homography.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            H = np.array(data.get("homography", []), dtype=float)
            if H.shape == (3, 3):
                return H
        except Exception:
            return None
        return None


# ---------------------------------------------------------------------------
# Multi-source Config (daemon and client entry points)
# ---------------------------------------------------------------------------


CONFIG_VARS: list[dict] = [
    {
        "key": "APRILCAM_DATA_DIR",
        "default": "(FHS: /var/lib/aprilcam · XDG: ~/.local/share/aprilcam)",
        "description": "Root directory for persistent state (cameras, calibrations, playfields).",
    },
    {
        "key": "APRILCAM_SOCKET_DIR",
        "default": "(FHS: /run/aprilcam · XDG: $XDG_RUNTIME_DIR/aprilcam)",
        "description": "Directory for the control socket, stream sockets, and pidfile.",
    },
    {
        "key": "APRILCAM_LOG_DIR",
        "default": "(FHS: /var/log/aprilcam · XDG: ~/.local/state/aprilcam)",
        "description": "Directory for aprilcamd.log.",
    },
    {
        "key": "APRILCAM_LOG_LEVEL",
        "default": "INFO",
        "description": "Python logging level for the daemon (DEBUG, INFO, WARNING, ERROR).",
    },
    {
        "key": "APRILCAM_DAEMON_PIDFILE",
        "default": "<socket_dir>/aprilcamd.pid",
        "description": "Pidfile path.",
    },
    {
        "key": "APRILCAM_DETECTION_FPS",
        "default": "10",
        "description": "Detection loop frame-rate cap in frames per second.",
    },
    {
        "key": "APRILCAM_STATIC_DESKEW",
        "default": "1",
        "description": "Enable homography-derived static-camera deskew (0 to disable).",
    },
    {
        "key": "APRILCAM_DESKEW_PX_PER_CM",
        "default": "0",
        "description": "Output resolution for the deskewed view in pixels/cm (0 = auto).",
    },
    {
        "key": "APRILCAM_UNDISTORT",
        "default": "0",
        "description": "Apply lens undistortion before deskew warp when intrinsics are present.",
    },
    {
        "key": "APRILCAM_MOVEMENT_THRESHOLD_PX",
        "default": "0",
        "description": "Movement-invalidation threshold in source pixels (0 = auto).",
    },
    {
        "key": "APRILCAM_SYSTEM",
        "default": "auto",
        "description": "Force FHS directory layout (1) or XDG (0); auto selects by euid.",
    },
    {
        "key": "APRILCAM_DAEMON_HOST",
        "default": "(unset — auto-discover via local Unix socket or mDNS)",
        "description": (
            "Hostname or IP of the AprilCam daemon for TCP gRPC connections. "
            "When set, mDNS discovery is skipped entirely. "
            "Use this to point clients at a remote Pi daemon."
        ),
    },
    {
        "key": "APRILCAM_DAEMON_PORT",
        "default": "5280",
        "description": "TCP port the AprilCam daemon's gRPC server listens on.",
    },
]


def _find_dotfile(name: str, start: Path) -> Optional[Path]:
    """Walk up from *start* to filesystem root looking for a file named *name*.

    Returns the first match found or ``None`` if no match exists.
    """
    cur = start.resolve()
    while True:
        candidate = cur / name
        if candidate.exists():
            return candidate
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _parse_dotfile(path: Path) -> Dict[str, str]:
    """Read a KEY=value dotfile, stripping ``#`` comments and blank lines.

    Returns a dict of string keys to string values.
    """
    result: Dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            # Strip inline and full-line comments
            if "#" in line:
                line = line[: line.index("#")].strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _default_dirs() -> tuple[Path, Path, Path]:
    """Return (data_dir, socket_dir, log_dir) for FHS or XDG mode.

    FHS mode triggers when os.geteuid() == 0 OR APRILCAM_SYSTEM env var == '1'.
    XDG mode is used otherwise (APRILCAM_SYSTEM=0 forces XDG even for root).
    This function performs no I/O.
    """
    import os as _os

    system_env = _os.environ.get("APRILCAM_SYSTEM", "").strip()
    is_root = _os.geteuid() == 0
    use_fhs = (system_env == "1") or (is_root and system_env != "0")

    if use_fhs:
        return (
            Path("/var/lib/aprilcam"),
            Path("/run/aprilcam"),
            Path("/var/log/aprilcam"),
        )
    # XDG paths with fallbacks
    uid = _os.getuid()
    data = Path(_os.environ.get("XDG_DATA_HOME", "") or Path.home() / ".local/share") / "aprilcam"
    run = Path(_os.environ.get("XDG_RUNTIME_DIR", "") or f"/run/user/{uid}") / "aprilcam"
    log = Path(_os.environ.get("XDG_STATE_HOME", "") or Path.home() / ".local/state") / "aprilcam"
    return data, run, log


@dataclass
class Config:
    """Priority-ordered configuration for the AprilCam daemon and clients.

    Loading priority (highest wins):
      6. ``APRILCAM_*`` process environment variables
      5. ``.env`` file found by walking up from *start* (via python-dotenv)
      4. ``.aprilcam`` file found by walking up from *start* (project-local)
      3. ``~/.aprilcam`` (user-global dotfile)
      2. ``/etc/aprilcam/aprilcam.env`` (system-wide, directory form)
      1. ``/etc/aprilcam.env`` (system-wide, single-file form, lowest priority)

    Call ``Config.load()`` at startup; do not instantiate directly.

    Key directories
    ---------------
    ``data_dir``
        Root AprilCam data directory (``APRILCAM_DATA_DIR``).
    ``cameras_dir``
        Per-camera subdirectory: ``<data_dir>/cameras/<slug>/``.
        Each camera directory contains ``calibration.json`` and ``paths.json``.
    ``socket_dir``
        Directory for the Unix socket and pidfile (``APRILCAM_SOCKET_DIR``).
    ``log_dir``
        Directory for log files (``APRILCAM_LOG_DIR``).
    """

    data_dir: Path = field(default_factory=lambda: Path("./data/aprilcam/"))
    socket_dir: Path = field(default_factory=lambda: Path("/tmp/aprilcam/"))
    env_dir: Optional[Path] = None  # directory containing the .env file (project root)
    log_level: str = "INFO"
    daemon_pidfile: Optional[Path] = None
    calibration_dir: Optional[Path] = None
    log_dir: Path = field(default_factory=lambda: Path("~/.local/state/aprilcam").expanduser())
    detection_fps: int = 10

    # --- Static-camera deskew (sprint 011, ticket 007) ---
    # Master switch for the homography-derived static-camera deskew path.  When
    # True (the default), static-camera mode engages AUTOMATICALLY for any camera
    # that has a saved homography — the seeded-geometry deskew and static-marker
    # fill-in / movement-invalidation run without live ArUco corner detection.
    # Set ``APRILCAM_STATIC_DESKEW=0`` to force the legacy live-corner path even
    # when a calibration exists.
    static_deskew: bool = True
    # Output resolution of the metric top-down deskew, in pixels per cm.  0 means
    # "use geometry.DEFAULT_PX_PER_CM".  This is the single user-facing knob for
    # the deskewed view's resolution (APRILCAM_DESKEW_PX_PER_CM).
    deskew_px_per_cm: float = 0.0
    # Optional pre-warp undistortion: when True AND the calibration carries
    # camera_matrix + dist_coeffs, the frame is undistorted before the deskew
    # warp for a flatter top-down result.  Off by default; a no-op when
    # intrinsics are absent (APRILCAM_UNDISTORT).
    undistort: bool = False
    # Movement-invalidation threshold in source pixels.  0 means "use
    # geometry.DEFAULT_MOVEMENT_THRESHOLD_PX" (APRILCAM_MOVEMENT_THRESHOLD_PX).
    movement_threshold_px: float = 0.0

    # --- Remote daemon connection (sprint 014) ---
    # Hostname or IP of the AprilCam daemon for TCP gRPC connections.
    # When set (via APRILCAM_DAEMON_HOST), mDNS discovery is skipped.
    # None means "auto-discover via local Unix socket or mDNS".
    daemon_host: Optional[str] = None
    # TCP port the daemon's gRPC server listens on (APRILCAM_DAEMON_PORT).
    daemon_port: int = 5280

    @property
    def cameras_dir(self) -> Path:
        """Directory containing one subdirectory per camera keyed by slug."""
        return self.data_dir / "cameras"

    @property
    def playfields_dir(self) -> Path:
        """Directory containing one JSON file per named playfield definition."""
        return self.data_dir / "playfields"

    def __post_init__(self) -> None:
        if self.daemon_pidfile is None:
            self.daemon_pidfile = self.socket_dir / "aprilcamd.pid"
        if self.calibration_dir is None:
            self.calibration_dir = self.data_dir / "calibration"

    @classmethod
    def load(cls, start: Optional[Path] = None) -> "Config":
        """Load configuration from all sources, highest priority last (env wins)."""
        start = start or Path.cwd()

        sources: Dict[str, str] = {}

        # 0a. System-wide lowest priority
        for etc_path in (Path("/etc/aprilcam.env"), Path("/etc/aprilcam/aprilcam.env")):
            sources.update(_parse_dotfile(etc_path))

        # 1. User-global dotfile (~/.aprilcam)
        user_dot = Path.home() / ".aprilcam"
        if user_dot.exists():
            sources.update(_parse_dotfile(user_dot))

        # 2. Project-local dotfile (.aprilcam, walk up from start)
        proj_dot = _find_dotfile(".aprilcam", start)
        if proj_dot:
            sources.update(_parse_dotfile(proj_dot))

        # 3. .env file (via dotenv_values, walk up from start)
        env_file = _find_dotfile(".env", start)
        if env_file:
            sources.update(
                {k: v for k, v in dotenv_values(env_file).items() if v is not None}
            )

        # 4. Process environment (highest priority — only APRILCAM_ keys)
        sources.update(
            {k: v for k, v in os.environ.items() if k.startswith("APRILCAM_")}
        )

        # Build field values from merged sources; resolve relative paths against
        # the start directory so daemon and clients agree on absolute locations.
        def _path(key: str, default: Path) -> Path:
            p = Path(sources[key]) if key in sources else default
            return p.resolve() if not p.is_absolute() else p

        def _opt_path(key: str) -> Optional[Path]:
            if key not in sources:
                return None
            p = Path(sources[key])
            return p.resolve() if not p.is_absolute() else p

        _dd, _sd, _ld = _default_dirs()
        data_dir = _path("APRILCAM_DATA_DIR", _dd)
        socket_dir = _path("APRILCAM_SOCKET_DIR", _sd)
        log_dir = _path("APRILCAM_LOG_DIR", _ld)

        # Parse detection_fps — default 10, must be a positive integer
        _fps_raw = sources.get("APRILCAM_DETECTION_FPS", "10")
        try:
            _fps = max(1, int(_fps_raw))
        except (ValueError, TypeError):
            _fps = 10

        def _bool(key: str, default: bool) -> bool:
            raw = sources.get(key)
            if raw is None:
                return default
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        def _float(key: str, default: float) -> float:
            raw = sources.get(key)
            if raw is None:
                return default
            try:
                return float(raw)
            except (ValueError, TypeError):
                return default

        # Parse daemon_host — None when unset (triggers mDNS / local unix probe)
        _daemon_host_raw = sources.get("APRILCAM_DAEMON_HOST", "").strip()
        _daemon_host: Optional[str] = _daemon_host_raw if _daemon_host_raw else None

        # Parse daemon_port — default 5280
        _daemon_port_raw = sources.get("APRILCAM_DAEMON_PORT", "5280")
        try:
            _daemon_port = int(_daemon_port_raw)
        except (ValueError, TypeError):
            _daemon_port = 5280

        cfg = cls(
            data_dir=data_dir,
            socket_dir=socket_dir,
            env_dir=env_file.parent.resolve() if env_file else None,
            log_level=sources.get("APRILCAM_LOG_LEVEL", "INFO"),
            daemon_pidfile=_path(
                "APRILCAM_DAEMON_PIDFILE", socket_dir / "aprilcamd.pid"
            ),
            log_dir=log_dir,
            detection_fps=_fps,
            static_deskew=_bool("APRILCAM_STATIC_DESKEW", True),
            deskew_px_per_cm=_float("APRILCAM_DESKEW_PX_PER_CM", 0.0),
            undistort=_bool("APRILCAM_UNDISTORT", False),
            movement_threshold_px=_float("APRILCAM_MOVEMENT_THRESHOLD_PX", 0.0),
            daemon_host=_daemon_host,
            daemon_port=_daemon_port,
        )

        # Ensure runtime directories exist; guarded to emit an actionable message
        # when the process lacks permission (e.g. system install without systemd).
        _dir_labels = [
            (cfg.socket_dir, "RuntimeDirectory=aprilcam"),
            (cfg.data_dir,   "StateDirectory=aprilcam"),
            (cfg.log_dir,    "LogsDirectory=aprilcam"),
        ]
        for _dir, _label in _dir_labels:
            try:
                _dir.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                import sys as _sys
                print(
                    f"aprilcam: cannot create {_dir} (permission denied).\n"
                    f"  For system installs, add to the systemd unit:\n"
                    f"    {_label}",
                    file=_sys.stderr,
                )
                raise

        return cfg
