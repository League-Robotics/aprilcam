"""OpenCV ArUco API compatibility shim (OpenCV 4.6 ↔ 4.7+).

OpenCV 4.7 introduced the object-oriented ``cv2.aruco.ArucoDetector``; OpenCV
4.6 (the version shipped by Ubuntu 24.04's ``python3-opencv``) only exposes the
free function ``cv2.aruco.detectMarkers(image, dictionary, parameters=...)``.

:func:`make_aruco_detector` returns a detector that exposes a uniform
``detectMarkers(image) -> (corners, ids, rejected)`` method on **both**
versions, so call sites are identical regardless of the installed OpenCV. This
lets the daemon run on a stock Raspberry Pi / Ubuntu OpenCV without pulling in a
pip OpenCV wheel (which, unlike the distro build, lacks GStreamer — needed for
the libcamera capture backend).

``cv2.aruco.DetectorParameters()`` and ``getPredefinedDictionary`` exist on both
4.6 and 4.7+, so only the detector construction needs shimming.

cv2 is imported lazily so cv2-free callers (and the import-cv2-free core
modules) can import this module without pulling in OpenCV.
"""
from __future__ import annotations


class _LegacyArucoDetector:
    """4.6-style detector wrapper exposing the 4.7+ ``.detectMarkers`` method."""

    def __init__(self, dictionary, params):
        self._dictionary = dictionary
        self._params = params

    def detectMarkers(self, image):  # noqa: N802 - mirror cv2 API name
        import cv2 as cv

        return cv.aruco.detectMarkers(image, self._dictionary, parameters=self._params)


def make_aruco_detector(dictionary, params):
    """Return an ArUco detector with a uniform ``detectMarkers(image)`` method.

    Uses ``cv2.aruco.ArucoDetector`` on OpenCV ≥4.7 and falls back to the free
    ``cv2.aruco.detectMarkers`` function on 4.6.
    """
    import cv2 as cv

    if hasattr(cv.aruco, "ArucoDetector"):
        return cv.aruco.ArucoDetector(dictionary, params)
    return _LegacyArucoDetector(dictionary, params)
