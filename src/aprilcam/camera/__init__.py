"""Camera hardware abstraction, enumeration, and multi-camera support.

``Camera`` and ``VideoCamera`` require OpenCV (the ``[daemon]`` extra).  They
are imported lazily via ``__getattr__`` so that client-side code can do
``from aprilcam.camera.composite import CompositeManager`` (or any other
cv2-free submodule import) without pulling in OpenCV.

Daemon-side code that does ``from aprilcam.camera import Camera`` still works
transparently — the lazy import fires on first attribute access.
"""

from __future__ import annotations

_LAZY: dict[str, tuple[str, str]] = {
    "Camera": (".camera", "Camera"),
    "VideoCamera": (".video_camera", "VideoCamera"),
}


def __getattr__(name: str):  # noqa: N807
    if name in _LAZY:
        import importlib
        module_rel, attr = _LAZY[name]
        mod = importlib.import_module(module_rel, package=__name__)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Camera", "VideoCamera"]
