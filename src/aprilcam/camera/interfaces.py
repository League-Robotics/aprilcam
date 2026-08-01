"""Camera identity, discovery, and hardware control.

**Daemon-only.** Nothing outside the daemon opens a camera (spec §2.1).
A client that wants pixels asks the daemon for a frame.

The three concerns here are deliberately separate: *identity* (which
physical camera is this, stably), *discovery* (what is attached right
now), and *control* (open it, configure it, read from it).
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

import numpy as np

from aprilcam.config.interfaces import CameraConfig
from aprilcam.types import CameraState


@runtime_checkable
class CameraIdentity(Protocol):
    """A stable name for a physical camera.

    Derived through a fallback chain — libcamera id, then serial /
    AVFoundation unique id, then ``vid:pid@location``, and finally a
    device-name-plus-resolution slug. It yields readable names like
    ``arducam-ov9782-usb-camera``.

    The property that matters is **stability across reboots and
    re-plugs** (UC-25.S-3): a camera moved to a different USB port
    derives the same name, so its installed config still matches it.
    Configuration-by-name stops working without this.

    Open (UC-25): when the chain degrades to the last-resort slug — two
    identical cameras with no serial numbers — names can collide. The
    registry must disambiguate rather than silently merge two cameras
    into one entry.
    """

    @property
    def slug(self) -> str: ...

    @property
    def device_name(self) -> str:
        """OS-reported hardware descriptor. **Not** the identity."""
        ...


@runtime_checkable
class CameraRegistry(Protocol):
    """The persistent identity → camera mapping.

    Lives in the **state** directory: regenerable, daemon-owned, never
    hand-edited. Delete it and re-probing rebuilds it.
    """

    def resolve(self, identity: CameraIdentity) -> str:
        """Return the stable slug for this camera, registering if new.

        Idempotent: the same physical camera always resolves to the same
        slug, and enumeration numbers never shuffle when a new camera
        appears.
        """
        ...

    def forget(self, slug: str) -> None:
        """Drop a camera from the registry entirely.

        Note this is *not* what happens when a camera is unplugged — an
        absent camera keeps its entry and is flagged not-present, because
        a config outliving its hardware is normal (UC-25).
        """
        ...


@runtime_checkable
class CameraDiscovery(Protocol):
    """Finding out what cameras exist (UC-25).

    Two mechanisms, both needed:

    * **Passive watching** — the daemon listens for devices arriving and
      disappearing and keeps its list current on its own. Plugging a
      camera into a running daemon needs no command (UC-25.S-5).
    * **Active probe** — enumerate every backend and re-interrogate.

    Passive watching is not sufficient on its own: libcamera / Raspberry
    Pi cameras are found by reading ``/sys`` or shelling out to the
    ``cam`` tool, neither triggered by a device event (UC-25.S-6). v1
    also cached a non-empty camera list *for the whole process lifetime*
    on the assumption that cameras are fixed hardware — an assumption a
    long-running daemon breaks. So probe both interrogates harder and
    **invalidates cached knowledge**.
    """

    def probe(self) -> tuple[CameraState, ...]:
        """Re-interrogate all backends and refresh the camera list.

        Must not disturb live subscribers: re-enumeration cannot mean
        dropping streams (UC-25.S-7). If a backend cannot be interrogated
        without disturbing an open camera, skip it and say so.

        Must not hang the daemon either — a wedged ``cam`` subprocess
        times out into a partial result rather than blocking a process
        that is serving live streams.
        """
        ...

    def watch(self) -> None:
        """Begin passive hotplug watching for the daemon's lifetime."""
        ...


@runtime_checkable
class Camera(Protocol):
    """One physical camera, owned end to end.

    The object the daemon manages one-of-per-camera. Owns its backend,
    its configuration, and its calibration when it has one.
    """

    @property
    def slug(self) -> str: ...

    @property
    def state(self) -> CameraState: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def read(self) -> Optional[np.ndarray]:
        """Grab one raw frame, or ``None`` if the read failed.

        Repeated failures trigger the self-heal reopen path — v1 used 5
        consecutive failures to reopen, capped at 10 reopens. What
        happens when that budget is exhausted (mark dead? keep trying?)
        is unspecified and needs deciding.
        """
        ...

    def apply_config(self, config: CameraConfig) -> None:
        """Apply a whole config document to the hardware, immediately.

        **Unsupported settings are hard errors** (UC-03). If the camera
        does not support a key, or will not take a value, the whole push
        fails naming the offending key and the camera is left exactly as
        it was — never half-configured. It must never report success for
        a setting that did not take effect; v1's blind success reporting
        here is precisely what this rule exists to kill.

        Control **ordering** matters: auto-exposure must be switched to
        manual before an exposure value will stick, and auto-white-
        balance off before WB temperature. Getting this wrong is how a
        setting silently does nothing (UC-03.S-3).
        """
        ...

    def effective_config(self) -> CameraConfig:
        """What is actually in force, introspected from the hardware.

        For an unconfigured camera this is the self-describing template:
        introspected facts plus **every settable key, blank** — the
        document every camera configuration starts from (UC-03.S-1,
        UC-25).
        """
        ...

    def reset(self) -> None:
        """Re-apply the stored on-disk config, discarding live overrides."""
        ...
