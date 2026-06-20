"""Lazy OpenCV loader for the aprilcam client package.

Importing this module never requires OpenCV. Call ``require_cv2()`` at the
point of use; it returns the ``cv2`` module or raises RuntimeError with an
install hint.
"""


def require_cv2():
    """Return the cv2 module, raising RuntimeError if OpenCV is not installed."""
    try:
        import cv2  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "This operation requires OpenCV. "
            "Install it with `pip install 'aprilcam[daemon]'`."
        ) from exc
    return cv2
