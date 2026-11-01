"""``PresenceSource`` implementations -- cheap "what is attached now?".

``LinuxSysfsPresenceSource`` lists ``/sys/class/video4linux`` and reads
a few sysfs attribute files per node. It never opens a device, so it is
safe to poll every 0.5 s even for cameras the daemon holds open. It
chose sysfs polling over udev/netlink: no new dependency, and the
spike showed an unplug is visible in ``/sys`` immediately.

Keys mirror ``camera.identity.resolve_identity``'s USB tiers so they
equal the registry's stored ``hardware_key`` for the same camera:
``serial:<serial>``, else ``vidpid:<vid>:<pid>@<port>``, else
``loc:<port>``. Non-USB nodes (v4l2loopback, platform, Pi-internal)
contribute no key.

``MacosAvfPresenceSource`` (ticket 047-008) lists AVFoundation devices
through ``cv2-enumerate-cameras`` -- the same enumeration, and the same
``AVCaptureDevice.uniqueID()`` ``path``, that ``resolve_identity`` turns
into the registry's ``avf:<uniqueID>`` hardware key, so keys match with
no translation. Choice of mechanism: AVFoundation applies device
connect/disconnect to the **main thread's** run loop, and the library
only pumps the *calling* thread's loop, so a worker-thread poll is
frozen at daemon start. We therefore keep the poll on the presence
thread (cheap, listing only) and have the daemon's otherwise idle main
thread pump its own run loop (``daemon.__main__._wait_for_shutdown``).
Rejected: marshalling every enumeration onto the main thread (needs a
dispatch queue the Python main thread does not service), IOKit
notifications (new pyobjc surface, still needs the run loop), and
``system_profiler`` polling (~0.5 s per call, spawns a process every
tick). Unlike ``_enumerate_cv2`` this lets enumeration errors
propagate, so the watcher keeps its previous snapshot instead of
reading a transient failure as "every camera was unplugged".
"""

from __future__ import annotations

import os
from typing import Callable, FrozenSet, Iterable, Optional

from aprilcam.camera.identity import (
    _SYS_V4L_ROOT,
    _read_usb_attrs,
    _read_v4l2_driver,
    _slug,
    _video_node_index,
    is_pi_internal_node,
)
from aprilcam.camera.interfaces import PresenceSource

__all__ = ["LinuxSysfsPresenceSource", "MacosAvfPresenceSource"]


def _hex_attr(usb_dev: str, name: str) -> Optional[int]:
    try:
        with open(os.path.join(usb_dev, name), encoding="utf-8") as f:
            return int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None


class LinuxSysfsPresenceSource(PresenceSource):
    """Hardware keys of USB video nodes under *sys_root*."""

    def __init__(self, sys_root: str = _SYS_V4L_ROOT) -> None:
        self._sys_root = sys_root

    def present_keys(self) -> FrozenSet[str]:
        try:
            names = os.listdir(self._sys_root)
        except OSError:
            return frozenset()
        keys = set()
        for name in names:
            idx = _video_node_index(name)
            if idx is None:
                continue
            driver = _read_v4l2_driver(idx, sys_root=self._sys_root)
            if is_pi_internal_node(driver):
                continue
            product, serial, port = _read_usb_attrs(idx, sys_root=self._sys_root)
            if port is None:
                continue  # not a USB device
            if serial:
                keys.add(f"serial:{serial}")
                continue
            usb_dev = os.path.dirname(
                os.path.realpath(os.path.join(self._sys_root, name, "device"))
            )
            vid = _hex_attr(usb_dev, "idVendor")
            pid = _hex_attr(usb_dev, "idProduct")
            if vid is not None and pid is not None:
                keys.add(f"vidpid:{vid:04x}:{pid:04x}@{_slug(port)}")
            else:
                keys.add(f"loc:{_slug(port)}")
        return frozenset(keys)


def _enumerate_avf_paths() -> Iterable[str]:
    """AVFoundation uniqueIDs of the attached cameras (raises on failure)."""
    from cv2_enumerate_cameras import enumerate_cameras  # noqa: PLC0415

    return [p for p in (getattr(c, "path", None) for c in enumerate_cameras()) if p]


class MacosAvfPresenceSource(PresenceSource):
    """``avf:<uniqueID>`` keys of the cameras AVFoundation lists now.

    Only sees hotplug while the main thread pumps its run loop (see the
    module docstring). *enumerate_paths* is injectable for tests.
    """

    def __init__(self, enumerate_paths: Callable[[], Iterable[str]] = _enumerate_avf_paths) -> None:
        self._enumerate_paths = enumerate_paths

    def present_keys(self) -> FrozenSet[str]:
        return frozenset(f"avf:{p}" for p in self._enumerate_paths())
