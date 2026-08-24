"""Shared parallax "displacement constant" helpers for real-hardware
``mount_z`` verification (sprint 035 tickets 011/015), extracted out of
``test_camera_geometry_hardware.py`` per that module's own real,
hand-measured ``camera_position`` (ticket 029-004: nadir ``(3.057,
-2.799)`` cm, height ``128.230`` cm).

**Why this module exists.** More than one hardware/acceptance test
needs to predict "what should the reported position become once
``mount_z`` is registered", against the *same* real ``camera_position``
this physical rig has actually solved --
``test_camera_geometry_hardware.py``'s own mount_z mechanism-proof, and
(sprint 035) ``tests/aprilcam/mcp/test_stdio_hardware.py`` (ticket 011)
and ``tests/acceptance/test_inventory.py`` (ticket 015). Ticket 011's
own acceptance criteria is explicit: "the displacement constants used
here are imported ... not redeclared -- grep-verified." Declaring
:data:`REAL_NADIR_X_CM`/:data:`REAL_NADIR_Y_CM`/:data:`REAL_HEIGHT_CM`
and the expected-position formula exactly once, here, and importing
them everywhere else, is what makes that literal rather than a
convention someone can silently drift away from.

**The formula is not reimplemented here either.**
:func:`expected_parallax_corrected_position` wraps
``calibration.geometry.project_tag_to_field_plane`` directly -- the one
real implementation this codebase corrects parallax with -- so a
hardware test's "expected" value can never diverge from what the daemon
itself actually computes on a live detection burst.
"""

from __future__ import annotations

from typing import Tuple

from aprilcam.calibration.geometry import project_tag_to_field_plane
from aprilcam.types import WorldPoint

__all__ = [
    "REAL_NADIR_X_CM",
    "REAL_NADIR_Y_CM",
    "REAL_HEIGHT_CM",
    "expected_parallax_corrected_position",
]

#: The real, checked-in ``camera_position`` this physical rig's arducam
#: (``arducam-ov9782-usb-camera`` / camera 3) actually holds --
#: sprint.md Decision 3, confirmed live via ``aprilcam camera config
#: arducam-ov9782-usb-camera --dump`` (ticket 029-004's completion
#: notes). Sacred: no test that imports this may ever push a different
#: value to the real config on disk (``tests/acceptance/conftest.py``'s
#: no-write guard is what makes that provable for ``tests/acceptance/``
#: specifically).
REAL_NADIR_X_CM = 3.0570142285481654
REAL_NADIR_Y_CM = -2.798700632324644
REAL_HEIGHT_CM = 128.22990496814816


def expected_parallax_corrected_position(
    raw_x_cm: float,
    raw_y_cm: float,
    *,
    mount_z_cm: float,
    nadir_x_cm: float = REAL_NADIR_X_CM,
    nadir_y_cm: float = REAL_NADIR_Y_CM,
    height_cm: float = REAL_HEIGHT_CM,
) -> Tuple[float, float]:
    """Where a tag raw-read at (*raw_x_cm*, *raw_y_cm*) should report
    once *mount_z_cm* of elevation is corrected for, against this real
    rig's own solved camera geometry (or an explicitly overridden one).

    Calls ``calibration.geometry.project_tag_to_field_plane`` directly
    rather than re-deriving its formula: ``ratio = mount_z_cm /
    camera_height_cm``, ``corrected = raw + ratio * (nadir - raw)``.
    Returns a plain ``(x, y)`` tuple, not a ``WorldPoint`` -- every
    caller so far only wants two floats to hand to ``pytest.approx``.
    """
    corrected = project_tag_to_field_plane(
        WorldPoint(x=raw_x_cm, y=raw_y_cm),
        mount_z_cm=mount_z_cm,
        camera_ground_position=WorldPoint(x=nadir_x_cm, y=nadir_y_cm),
        camera_height_cm=height_cm,
    )
    return corrected.x, corrected.y
