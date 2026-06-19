"""Tests for ticket 015-005: view_cli.py imports without cv2.

Verifies that aprilcam.cli.view_cli can be imported even when cv2 is
unavailable, confirming that all cv2 usage has been replaced with Pillow
and numpy-only alternatives.
"""

from __future__ import annotations

import importlib
import sys


def test_view_cli_imports_without_cv2() -> None:
    """view_cli must import successfully even when cv2 raises ImportError.

    Monkeypatches builtins.__import__ so that any ``import cv2`` raises
    ImportError.  The real cv2 module (if present in sys.modules) is
    temporarily hidden.  view_cli is also evicted from the module cache so
    it is freshly re-imported under the blocked-cv2 environment.

    After the test everything is restored so that later tests are unaffected.
    """
    import builtins

    # Hide the real cv2 from the module cache
    real_cv2 = sys.modules.pop("cv2", None)

    # Override builtins.__import__ to raise for cv2
    original_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("cv2 is not available (blocked by test)")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = _blocking_import  # type: ignore[assignment]

    # Remove any previously cached view_cli from sys.modules so it is re-imported
    _cached = sys.modules.pop("aprilcam.cli.view_cli", None)

    try:
        importlib.import_module("aprilcam.cli.view_cli")  # must not raise
    finally:
        builtins.__import__ = original_import
        if real_cv2 is not None:
            sys.modules["cv2"] = real_cv2
        else:
            sys.modules.pop("cv2", None)
        sys.modules.pop("aprilcam.cli.view_cli", None)
        if _cached is not None:
            sys.modules["aprilcam.cli.view_cli"] = _cached


def test_point_in_rect_poly_inside() -> None:
    """_point_in_rect_poly returns True for a point clearly inside."""
    import numpy as np
    from aprilcam.cli.view_cli import _point_in_rect_poly

    poly = np.array([[10, 10], [90, 10], [90, 90], [10, 90]], dtype=np.float32)
    assert _point_in_rect_poly(poly, 50.0, 50.0) is True


def test_point_in_rect_poly_outside() -> None:
    """_point_in_rect_poly returns False for a point clearly outside."""
    import numpy as np
    from aprilcam.cli.view_cli import _point_in_rect_poly

    poly = np.array([[10, 10], [90, 10], [90, 90], [10, 90]], dtype=np.float32)
    assert _point_in_rect_poly(poly, 5.0, 5.0) is False
