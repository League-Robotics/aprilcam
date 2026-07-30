"""Core detection engine, data models, and playfield geometry.

cv2-free modules are imported eagerly so that client-side code (MCP server,
web server, view CLI, etc.) can ``from aprilcam.core.<submodule> import …``
without pulling in OpenCV.

cv2-requiring modules (detector, tracker, pipeline) are imported lazily via
``__getattr__`` so that importing *any* submodule of ``aprilcam.core`` does
NOT trigger a top-level ``import cv2``.  Daemon-side code that does
``from aprilcam.core import TagDetector`` (or the equivalent) still works
transparently — the lazy import fires on first attribute access.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Eagerly-imported, cv2-free symbols
# ---------------------------------------------------------------------------

from .motion import VelocityEstimator
from .tag import Tag
from .playfield import Playfield, PlayfieldBoundary
from .detection import TagRecord, FrameRecord, RingBuffer

# ---------------------------------------------------------------------------
# Lazy imports for cv2-requiring symbols
# ---------------------------------------------------------------------------

# Map each public name to (module_path, attr_in_module)
_LAZY: dict[str, tuple[str, str]] = {
    # detector.py — import cv2 at module scope
    "TagDetector": (".detector", "TagDetector"),
    "DetectorConfig": (".detector", "DetectorConfig"),
    "Detection": (".detector", "Detection"),
    # tracker.py — import cv2 at module scope
    "OpticalFlowTracker": (".tracker", "OpticalFlowTracker"),
    # pipeline.py — import cv2 at module scope
    "DetectionPipeline": (".pipeline", "DetectionPipeline"),
}


def __getattr__(name: str):  # noqa: N807
    if name in _LAZY:
        module_rel, attr = _LAZY[name]
        import importlib
        mod = importlib.import_module(module_rel, package=__name__)
        val = getattr(mod, attr)
        # Cache on the package so subsequent accesses are O(1)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # cv2-free
    "VelocityEstimator",
    "Tag",
    "Playfield",
    "PlayfieldBoundary",
    "TagRecord",
    "FrameRecord",
    "RingBuffer",
    # cv2-requiring (lazy)
    "TagDetector",
    "DetectorConfig",
    "Detection",
    "OpticalFlowTracker",
    "DetectionPipeline",
]
