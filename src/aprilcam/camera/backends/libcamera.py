"""libcamera (Raspberry Pi CSI) camera **enumeration** — capture is not
ported this ticket.

Converted, in reduced form, from ``archive/src/aprilcam/camera/libcam.py``
(233 lines: enumeration in both "loopback" and "gst" mode, plus a
GStreamer pipeline builder for direct capture). This ticket (020-003)
ports only the **loopback enumeration** half:
:func:`list_loopback_cameras`, which reads
``/sys/class/video4linux/video*/name`` for devices whose card label
starts with a configured prefix (default ``aprilcam-``) — no
subprocess, so it is safe and cheap to call from
``camera/identity.py``'s ``_probe_libcamera`` on every platform,
including this Mac, where the ``/sys/class/video4linux`` tree simply
does not exist and the ``glob`` comes back empty.

**Deferred, per the ticket's explicit allowance** ("this backend may be
stubbed/deferred if it adds no test coverage this sprint"):

* v1's "gst mode" (the ``cam -l`` subprocess enumeration and the
  ``libcamerasrc`` GStreamer pipeline builder) — untestable without a
  ``cam`` binary and a libcamera device, neither present here.
* An actual libcamera **capture** backend (a
  :class:`~aprilcam.camera.backends.CaptureBackend` that reads frames
  from the enumerated loopback device) — v1 read loopback devices via
  :mod:`aprilcam.camera.backends.v4l2_loopback`'s
  ``v4l2-ctl``-subprocess technique keyed by the ``/dev/videoN`` index
  this module resolves, so wiring one up later is "construct a
  ``V4l2LoopbackBackend(f"/dev/video{n}")`` for the resolved device" —
  a small addition, not a new design, left for whichever sprint next
  brings up real Pi hardware to validate against.

Recorded here per the ticket's instruction to note the decision either
way, and in ``camera/backends/__init__.py``'s module table.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import List

__all__ = ["LibcameraDevice", "list_loopback_cameras", "loopback_label_prefix"]

_SYS_V4L_GLOB = "/sys/class/video4linux/video*/name"
_LOOPBACK_LABEL_ENV = "APRILCAM_LIBCAM_LOOPBACK_LABEL"
_DEFAULT_LABEL_PREFIX = "aprilcam-"


@dataclass(frozen=True)
class LibcameraDevice:
    """One libcamera camera reached through a labelled v4l2loopback
    device, as enumerated from ``/sys``.

    ``position`` is 0-based enumeration order (stable across calls for
    the same attached set — v1's convention); ``device_index`` is the
    ``/dev/video<device_index>`` node a capture backend would open.
    """

    position: int
    slug: str
    model: str
    device_index: int
    camera_id: str  # the raw /sys card label -- stable, used as the
                     # hardware-identity source (mirrors v1's usage as
                     # a unique id in camera/identity.py's libcamera tier)


def loopback_label_prefix() -> str:
    """Card-label prefix the bridge tags its v4l2loopback devices with."""
    return os.environ.get(_LOOPBACK_LABEL_ENV, _DEFAULT_LABEL_PREFIX)


def list_loopback_cameras(*, sys_glob: str = _SYS_V4L_GLOB) -> List[LibcameraDevice]:
    """Enumerate labelled v4l2loopback devices via ``/sys`` (no subprocess).

    Reads ``/sys/class/video4linux/video*/name`` and keeps devices
    whose card label starts with :func:`loopback_label_prefix` (the
    out-of-process ``libcamerasrc`` bridge sets these labels, e.g.
    ``aprilcam-imx296-88000``). Never raises: an unreadable path is
    skipped, an empty/missing ``/sys`` tree (every non-Linux machine,
    this Mac included) yields an empty list.

    ``sys_glob`` is overridable so tests can point this at a temporary
    directory tree standing in for ``/sys`` instead of the real one.
    """
    prefix = loopback_label_prefix()
    found: List[tuple] = []
    for path in glob.glob(sys_glob):
        try:
            with open(path, encoding="utf-8") as fh:
                name = fh.read().strip()
        except OSError:
            continue
        if not name.startswith(prefix):
            continue
        m = re.search(r"video(\d+)/name$", path)
        if not m:
            continue
        found.append((int(m.group(1)), name))
    found.sort()  # by device index, matching v1's enumeration order

    devices: List[LibcameraDevice] = []
    for position, (dev_idx, name) in enumerate(found):
        slug = name[len(prefix):] if name.startswith(prefix) else name
        model = slug.split("-", 1)[0] or "camera"
        devices.append(
            LibcameraDevice(
                position=position,
                slug=slug,
                model=model,
                device_index=dev_idx,
                camera_id=name,
            )
        )
    return devices
