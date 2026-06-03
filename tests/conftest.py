"""pytest configuration — marker registration and skip fixtures.

Markers
-------
needs_cv2
    Skip the test if ``cv2`` (opencv-contrib-python) is not importable.
    Absence means the package was installed with only the base extras.
    Reason reported to the user: "requires aprilcam[imaging]".

needs_daemon
    Skip the test if the aprilcam daemon stack is not importable
    (i.e. ``aprilcam[daemon]`` extras are absent).
    Reason reported to the user: "requires aprilcam[daemon]".
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "needs_cv2: skip test if opencv-contrib-python is not installed",
    )
    config.addinivalue_line(
        "markers",
        "needs_daemon: skip test if aprilcam[daemon] dependencies are not installed",
    )


def _cv2_available() -> bool:
    try:
        import cv2  # noqa: F401, PLC0415
        return True
    except ModuleNotFoundError:
        return False


def _daemon_available() -> bool:
    try:
        import aprilcam.daemon.grpc_server  # noqa: F401, PLC0415
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Apply runtime skip logic for needs_cv2 and needs_daemon markers.

    Tests that have *top-level* daemon/cv2 imports must use
    ``pytest.importorskip()`` at module level (before the import) so that
    the module is skipped at *collection* time.  Tests that only use the
    import inside the test function body can rely on these markers instead.
    """
    cv2_ok = _cv2_available()
    daemon_ok = _daemon_available()

    skip_cv2 = pytest.mark.skip(reason="requires aprilcam[imaging]")
    skip_daemon = pytest.mark.skip(reason="requires aprilcam[daemon]")

    for item in items:
        if not cv2_ok and item.get_closest_marker("needs_cv2"):
            item.add_marker(skip_cv2)
        if not daemon_ok and item.get_closest_marker("needs_daemon"):
            item.add_marker(skip_daemon)
