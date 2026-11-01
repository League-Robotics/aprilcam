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
"""

from __future__ import annotations

import os
from typing import FrozenSet, Optional

from aprilcam.camera.identity import (
    _SYS_V4L_ROOT,
    _read_usb_attrs,
    _read_v4l2_driver,
    _slug,
    _video_node_index,
    is_pi_internal_node,
)
from aprilcam.camera.interfaces import PresenceSource

__all__ = ["LinuxSysfsPresenceSource"]


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
