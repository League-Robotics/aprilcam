"""aprilcam.client — typed client-side objects for the AprilCam daemon.

Application code imports from this package instead of touching
proto-generated types directly.
"""

from aprilcam.client.models import (
    CameraInfo,
    ImageFrame,
    PathRecord,
    StreamEndpoint,
    TagFrame,
    TagRecord,
)
from aprilcam.client.control import DaemonControl
from aprilcam.client.stream import ImageStreamConsumer, TagStreamConsumer

__all__ = [
    "CameraInfo",
    "DaemonControl",
    "ImageFrame",
    "ImageStreamConsumer",
    "PathRecord",
    "StreamEndpoint",
    "TagFrame",
    "TagRecord",
    "TagStreamConsumer",
]
