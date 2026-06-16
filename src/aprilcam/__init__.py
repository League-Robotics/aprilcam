"""AprilCam — AprilTag detection and playfield tracking.

Primary API::

    import aprilcam

    camera = aprilcam.Camera.find("Brio")
    field = aprilcam.Playfield(camera, width_cm=134.3, height_cm=89.3)
    field.start()
    tag = field.tag(42)
    if tag:
        tag.update()
        print(f"Tag 42 at ({tag.wx:.1f}, {tag.wy:.1f}) cm")
    field.stop()
"""

from pathlib import Path as _Path

# New OOP API — these require OpenCV (available in the [daemon] extra).
# Guard the import so that client-only installs (base or [imaging]) can still
# import ``aprilcam.client`` without OpenCV installed.
try:
    from aprilcam.camera import Camera, VideoCamera
    from aprilcam.core.playfield import Playfield
    from aprilcam.core.tag import Tag
    from aprilcam.core.detection import TagRecord
    from aprilcam.calibration import calibrate, CameraCalibration, FieldSpec
    from aprilcam.vision.objects import ObjectRecord
    from aprilcam.errors import (
        CameraError,
        CameraInUseError,
        CameraNotFoundError,
        CameraPermissionError,
    )
except ImportError:
    # OpenCV not installed — only the client subpackage is available.
    pass

__all__ = [
    "__version__",
    "help",
    # New OOP API
    "Camera",
    "VideoCamera",
    "Playfield",
    "Tag",
    "calibrate",
    "CameraCalibration",
    "FieldSpec",
    "TagRecord",
    "ObjectRecord",
    # Errors
    "CameraError",
    "CameraInUseError",
    "CameraNotFoundError",
    "CameraPermissionError",
]
__version__ = "0.1.0"

_AGENT_GUIDE = _Path(__file__).parent / "AGENT_GUIDE.md"


def help() -> str:
    """Return the AprilCam agent guide as a markdown string."""
    return _AGENT_GUIDE.read_text()
