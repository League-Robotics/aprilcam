from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Dict
import subprocess
import re
import shutil
import os


import cv2 as cv


@dataclass
class CameraInfo:
    index: int
    name: str
    backend: Optional[str] = None
    device_name: Optional[str] = None  # raw OS device name (no backend suffix)
    # Optional hardware-identity fields (populated by camera.identity when
    # available; absent fields leave behavior unchanged). See camera/identity.py.
    unique_id: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial: Optional[str] = None
    location: Optional[str] = None


def camera_slug(device_name: str, width: int, height: int) -> str:
    """Create a filesystem-safe slug from camera device name and resolution.

    Example: camera_slug("Brio 501", 1920, 1080) → "brio-501-1920x1080"
    """
    # Lowercase and replace non-alphanumeric chars with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", device_name.lower())
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    # Collapse multiple hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    return f"{slug}-{width}x{height}"


def default_backends() -> List[int]:
    if os.name == "nt":  # Windows
        return [getattr(cv, "CAP_MSMF", 1400), getattr(cv, "CAP_DSHOW", 700), getattr(cv, "CAP_ANY", 0)]
    elif sys.platform == "darwin":  # macOS
        return [getattr(cv, "CAP_AVFOUNDATION", 1200), getattr(cv, "CAP_ANY", 0)]
    else:  # Linux/Unix
        return [getattr(cv, "CAP_V4L2", 200), getattr(cv, "CAP_ANY", 0)]


class _SilenceStderr:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._orig_fd = None
        self._devnull_fd = None

    def __enter__(self):
        if not self.enabled:
            return self
        try:
            # Duplicate original stderr fd
            self._orig_fd = os.dup(2)
            # Open devnull and redirect fd 2 there
            self._devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(self._devnull_fd, 2)
        except Exception:
            # If anything fails, best-effort: mark as disabled
            self.enabled = False
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        try:
            if self._orig_fd is not None:
                os.dup2(self._orig_fd, 2)
        finally:
            if self._devnull_fd is not None:
                try:
                    os.close(self._devnull_fd)
                except Exception:
                    pass
            if self._orig_fd is not None:
                try:
                    os.close(self._orig_fd)
                except Exception:
                    pass
        return False


def _macos_avfoundation_device_names() -> Dict[int, str]:
    names: Dict[int, str] = {}
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return names
    try:
        # ffmpeg prints device list to stderr
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        text = proc.stderr
        # Parse lines under "AVFoundation video devices:" like "[0] FaceTime HD Camera"
        in_video = False
        for line in text.splitlines():
            if "AVFoundation video devices:" in line:
                in_video = True
                continue
            if "AVFoundation audio devices:" in line:
                in_video = False
            if not in_video:
                continue
            m = re.search(r"\[(\d+)\]\s+(.+)$", line.strip())
            if m:
                idx = int(m.group(1))
                nm = m.group(2).strip()
                names[idx] = nm
    except Exception:
        pass
    return names


def macos_avfoundation_device_names() -> Dict[int, str]:
    """Public helper to fetch AVFoundation device names via ffmpeg.
    Keys are the AVFoundation indices used by ffmpeg/imagesnap, not necessarily cv2 index mapping for CAP_ANY.
    """
    if sys.platform != "darwin":
        return {}
    return _macos_avfoundation_device_names()


def _list_v4l2_usb_cameras() -> List[CameraInfo]:
    """List real USB/UVC V4L2 cameras (driver ``uvcvideo``), one per device.

    Surfaces USB webcams (e.g. a Logitech C920) alongside the libcamera CSI
    cameras on a Raspberry Pi. Excludes the CSI/ISP/codec nodes and the
    v4l2loopback bridge devices (none use the ``uvcvideo`` driver). The camera's
    ``index`` is its ``/dev/videoN`` number, so ``cv2.VideoCapture(index)`` opens
    it directly.
    """
    import glob

    cams: List[CameraInfo] = []
    seen: set = set()

    def _num(p: str) -> int:
        m = re.search(r"video(\d+)$", p)
        return int(m.group(1)) if m else (1 << 30)

    for path in sorted(glob.glob("/sys/class/video4linux/video*"), key=_num):
        idx = _num(path)
        if idx == (1 << 30):
            continue
        try:
            drv = os.path.basename(
                os.path.realpath(os.path.join(path, "device", "driver"))
            )
        except Exception:
            continue
        if drv != "uvcvideo":
            continue
        # uvcvideo exposes a capture node + a metadata node per camera; keep only
        # the first (lowest-index = capture) node per physical USB device.
        try:
            dev = os.path.realpath(os.path.join(path, "device"))
        except Exception:
            dev = path
        if dev in seen:
            continue
        seen.add(dev)
        try:
            name = open(os.path.join(path, "name")).read().strip()
        except Exception:
            name = f"USB Camera {idx}"
        cams.append(CameraInfo(index=idx, name=name, backend="v4l2", device_name=name))
    return cams


def list_cameras(max_index: int = 10, backends: Optional[List[int]] = None, stop_after_failures: int = 4, quiet: bool = False, detailed_names: bool = False) -> List[CameraInfo]:
    """List available cameras.

    Uses ``cv2-enumerate-cameras`` when available for accurate OS device
    names and ordering.  Falls back to the legacy OpenCV probe loop.
    """
    # --- Raspberry Pi / libcamera backend ---
    # On a Pi the CSI cameras are not V4L2-capturable; enumerate the *real*
    # cameras via libcamera so the daemon reports exactly the physical cameras
    # (e.g. two on a dual-CSI Pi 5), not the dozens of ISP /dev/video* nodes.
    try:
        from . import libcam

        if libcam.backend_enabled():
            cams = [
                CameraInfo(
                    index=c.position,
                    name=c.friendly_name,
                    backend="libcamera",
                    device_name=c.slug,
                    unique_id=c.camera_id,
                    location=c.camera_id,
                )
                for c in libcam.list_cameras()
            ]
            # Surface real USB/UVC cameras alongside the CSI cameras so a USB
            # webcam works even when the libcamera backend is enabled.
            cams.extend(_list_v4l2_usb_cameras())
            return cams
    except Exception:
        pass

    # --- Primary path: cv2-enumerate-cameras ---
    try:
        from cv2_enumerate_cameras import enumerate_cameras

        avf_offset = getattr(cv, "CAP_AVFOUNDATION", 1200)
        # Resolve stable hardware identities once for all enumerated devices.
        # Best-effort: never let identity resolution break enumeration.
        identities = {}
        try:
            from .identity import resolve_all

            identities = resolve_all()
        except Exception:
            identities = {}
        cameras: List[CameraInfo] = []
        for cam in enumerate_cameras():
            # Derive the OpenCV index from the raw backend index
            idx = cam.index - avf_offset if cam.index >= avf_offset else cam.index
            backend_name = "AVFOUNDATION" if cam.index >= avf_offset else None
            device_name = cam.name or None
            name = (device_name or f"Camera {idx}") + (f" ({backend_name})" if backend_name else "")
            info = CameraInfo(index=idx, name=name, backend=backend_name, device_name=device_name)
            ident = identities.get(idx)
            if ident is not None:
                info.unique_id = ident.unique_id
                info.vid = ident.vid
                info.pid = ident.pid
                info.serial = ident.serial
                info.location = ident.location
            cameras.append(info)
        return cameras
    except ImportError:
        pass
    except Exception:
        pass

    # --- Fallback: legacy OpenCV probe loop ---
    cameras = []
    backends = backends or default_backends()
    backend_failures: Dict[int, int] = {be: 0 for be in backends}
    av_names: Dict[int, str] = {}
    if sys.platform == "darwin" and detailed_names:
        av_names = _macos_avfoundation_device_names()
    for idx in range(max_index):
        for be in list(backends):
            if backend_failures.get(be, 0) >= max(1, int(stop_after_failures)) and idx > 1:
                continue
            with _SilenceStderr(quiet):
                # PROBE-ONLY: brief open to check name/availability, then
                # released immediately.  This is camera enumeration, not
                # sustained capture.  The daemon's CameraPipeline is the sole
                # sustained camera opener.
                cap = cv.VideoCapture(idx, be)
                try:
                    if cap.isOpened():
                        backend_name = None
                        try:
                            backend_name = cap.getBackendName() if hasattr(cap, "getBackendName") else None
                        except Exception:
                            pass
                        pretty = None
                        if sys.platform == "darwin" and backend_name == "AVFOUNDATION" and idx in av_names:
                            pretty = av_names.get(idx)
                        name = (pretty or f"Camera {idx}") + (f" ({backend_name})" if backend_name else "")
                        cameras.append(CameraInfo(index=idx, name=name, backend=backend_name, device_name=pretty))
                        backend_failures[be] = 0
                        break
                    else:
                        backend_failures[be] = backend_failures.get(be, 0) + 1
                finally:
                    cap.release()
    return cameras


def get_device_name(index: int) -> str:
    """Return the OS-reported device name for a camera index.

    Uses ``cv2-enumerate-cameras`` to get the correct AVFoundation
    device name for an OpenCV camera index.  The library returns
    indices in the form ``backend_offset + device_index`` (e.g. 1200,
    1201, 1202 for AVFoundation), which maps directly to OpenCV
    indices 0, 1, 2 when using CAP_AVFOUNDATION.

    Falls back to ``"camera-{index}"`` if the name cannot be determined.
    """
    # libcamera backend: the device key is the per-camera slug.
    try:
        from . import libcam

        if libcam.backend_enabled():
            c = libcam.camera_for_index(index)
            if c is not None:
                return c.slug
    except Exception:
        pass

    try:
        from cv2_enumerate_cameras import enumerate_cameras
        avf_offset = getattr(cv, "CAP_AVFOUNDATION", 1200)
        target = avf_offset + index
        for cam in enumerate_cameras():
            if cam.index == target:
                return cam.name
    except ImportError:
        pass
    except Exception:
        pass
    # Fallback: try the ffmpeg-based enumeration
    try:
        cams = list_cameras(max_index=10, quiet=True, detailed_names=True)
        for c in cams:
            if c.index == index and c.device_name:
                return c.device_name
    except Exception:
        pass
    return f"camera-{index}"


def diagnose_camera_failure(index: int) -> dict:
    """Diagnose why a camera failed to open.

    Returns ``{"exists": bool, "blocking_processes": [{"pid": int, "name": str}, ...]}``
    On unsupported platforms or diagnostic failure, assumes camera exists and returns
    an empty blocking list.
    """
    result: dict = {"exists": True, "blocking_processes": []}

    if sys.platform == "darwin":
        result = _diagnose_macos(index)
    elif sys.platform.startswith("linux"):
        result = _diagnose_linux(index)
    # else: unsupported platform — return defaults

    return result


def _diagnose_macos(index: int) -> dict:
    """macOS camera diagnostics using AVFoundation device list and lsof."""
    result: dict = {"exists": True, "blocking_processes": []}

    # Check if camera index exists via AVFoundation device names
    try:
        av_names = _macos_avfoundation_device_names()
        if av_names and index not in av_names:
            result["exists"] = False
            return result
    except Exception:
        pass

    # Try to find processes using camera devices via lsof
    try:
        proc = subprocess.run(
            ["lsof"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        blocking = []
        for line in proc.stdout.splitlines():
            lower = line.lower()
            if any(kw in lower for kw in ("vdc", "applecamera", "isight", "camera", "avfoundation")):
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0]
                    try:
                        pid = int(parts[1])
                    except (ValueError, IndexError):
                        continue
                    # Avoid duplicates
                    if not any(b["pid"] == pid for b in blocking):
                        blocking.append({"pid": pid, "name": name})
        result["blocking_processes"] = blocking
    except Exception:
        pass

    return result


def _diagnose_linux(index: int) -> dict:
    """Linux camera diagnostics using /dev/video* and fuser."""
    result: dict = {"exists": True, "blocking_processes": []}
    dev_path = f"/dev/video{index}"

    if not os.path.exists(dev_path):
        result["exists"] = False
        return result

    # Use fuser to find PIDs holding the device
    try:
        proc = subprocess.run(
            ["fuser", dev_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        # fuser outputs PIDs to stdout (or stderr depending on version)
        pid_text = (proc.stdout + " " + proc.stderr).strip()
        pids = []
        for token in pid_text.replace(dev_path + ":", "").split():
            token = token.strip().rstrip("m").rstrip("e").rstrip("f")
            try:
                pids.append(int(token))
            except ValueError:
                continue

        blocking = []
        for pid in pids:
            name = _get_process_name(pid)
            blocking.append({"pid": pid, "name": name})
        result["blocking_processes"] = blocking
    except Exception:
        pass

    return result


def _get_process_name(pid: int) -> str:
    """Get the command name for a PID via ps."""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        name = proc.stdout.strip()
        return name if name else f"PID {pid}"
    except Exception:
        return f"PID {pid}"


def select_camera_by_pattern(pattern: Optional[str], cameras: List[CameraInfo]) -> Optional[int]:
    if not pattern:
        return None
    pat = pattern.strip().lower()
    # Direct index forms: "@2", "#2", or plain "2"
    if pat.startswith("@") or pat.startswith("#"):
        try:
            return int(pat[1:])
        except ValueError:
            pass
    try:
        # If the whole string is an int, use as index
        return int(pat)
    except ValueError:
        pass
    for cam in cameras:
        if pat in cam.name.lower():
            return cam.index
    return None
