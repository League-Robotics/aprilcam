"""Camera identity, discovery, and hardware control.

**Daemon-only.** Nothing outside the daemon opens a camera (spec §2.1).
A client that wants pixels asks the daemon for a frame.

The three concerns here are deliberately separate: *identity* (which
physical camera is this, stably), *discovery* (what is attached right
now), and *control* (open it, configure it, read from it).

See ``aprilcam.daemon.interfaces``'s module docstring for the interface
design rule applied below: ``CameraRegistry``, ``CameraDiscovery``, and
``Camera`` are behavioral interfaces with ordinary-class implementations
(``camera.registry.CameraRegistry``, ``camera.discovery.CameraDiscovery``,
``camera.camera.Camera``), so they are plain base classes.
``CameraIdentity`` is value-shaped, so it is a frozen ``@dataclass``
declaring fields rather than properties — and, since its concrete
producer (``camera.identity.resolve_identity``) needs no fields beyond
these, ``camera.identity`` imports this exact class rather than
declaring a second one of the same name. There used to be two different
``CameraIdentity`` classes (this one, structural; ``camera.identity``'s,
a dataclass) that merely happened to share a name — they are now one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional

import numpy as np

from aprilcam.config.interfaces import CameraConfig
from aprilcam.types import CameraState, ProbeReport


class CameraLifecycle(Enum):
    """Where a held :class:`Camera` is in its device lifecycle (sprint 047).

    ``PRESENT``: device open (or being healed). ``ABSENT``: the hardware
    is gone (unplugged); device closed, config and subscribers kept.
    ``CLOSED``: deliberately closed (never opened, idle-reaped, explicit
    close). ``Camera.reopen`` returns ABSENT/CLOSED to PRESENT.
    """

    PRESENT = "present"
    ABSENT = "absent"
    CLOSED = "closed"


@dataclass(frozen=True, kw_only=True)
class CameraIdentity:
    """A stable name for a physical camera.

    Produced by ``camera.identity.resolve_identity``/``resolve_all`` —
    see that module's docstring for the fallback-chain algorithm that
    fills in these fields. Declared here, in the subsystem's interface
    module, because every other module that needs "a camera identity"
    (``camera.registry.CameraRegistry.resolve``, ``camera.discovery``)
    should depend on the contract, not on the resolver that happens to
    produce it.

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

    Two different questions, two different fields (``camera.identity``'s
    module docstring goes into this in more depth):

    * **"What should a human call this camera?"** — always the
      device-name slug, regardless of which hardware source resolved
      it. This is :attr:`slug`.
    * **"Is this the same physical camera I saw before?"** — the
      fallback chain, from most to least trustworthy, recorded in
      :attr:`hardware_key` / :attr:`source`. :mod:`aprilcam.camera.
      registry` reads :attr:`hardware_key` as its primary key.

    Fields beyond ``slug``/``device_name``:

    hardware_key:
        The fallback-chain result (``avf:...``, ``serial:...``,
        ``vidpid:vvvv:pppp@loc``, ``loc:...``, or ``name:<slug>``) —
        what actually varies with hardware source, and what
        :mod:`aprilcam.camera.registry` keys on for dedup/stability.
    source:
        Which fallback tier produced ``hardware_key`` (e.g.
        ``"libcamera_id"``, ``"usb_serial"``, ``"name_resolution_slug"``).
    is_fallback:
        Whether ``hardware_key`` came from a tier that is not genuinely
        unique to this physical device (a USB-port move or
        re-enumeration can produce a new key) — tiers 3-5 of the chain.
    vid, pid, serial, location, avfoundation_unique_id:
        Raw per-source diagnostics carried through for probe reporting;
        each is ``None`` when its source did not contribute.
    """

    slug: str
    device_name: str
    hardware_key: str
    source: str
    is_fallback: bool
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial: Optional[str] = None
    location: Optional[str] = None
    avfoundation_unique_id: Optional[str] = None


class CameraRegistry:
    """The persistent identity → camera mapping.

    A plain base class, not ``Protocol`` — see the module docstring.

    Lives in the **state** directory: regenerable, daemon-owned, never
    hand-edited. Delete it and re-probing rebuilds it.
    """

    def resolve(self, identity: CameraIdentity) -> str:
        """Return the stable slug for this camera, registering if new.

        Idempotent: the same physical camera always resolves to the same
        slug, and enumeration numbers never shuffle when a new camera
        appears.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement CameraRegistry.resolve")

    def forget(self, slug: str) -> None:
        """Drop a camera from the registry entirely.

        Note this is *not* what happens when a camera is unplugged — an
        absent camera keeps its entry and is flagged not-present, because
        a config outliving its hardware is normal (UC-25).
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement CameraRegistry.forget")


class CameraDiscovery:
    """Finding out what cameras exist (UC-25).

    A plain base class, not ``Protocol`` — see the module docstring.

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

    def probe(self) -> ProbeReport:
        """Re-interrogate all backends and refresh the camera list.

        Must not disturb live subscribers: re-enumeration cannot mean
        dropping streams (UC-25.S-7). If a backend cannot be interrogated
        without disturbing an open camera, skip it and say so — "say so"
        is the report's ``skipped`` entries, which is why this returns a
        :class:`ProbeReport` and not a bare camera list.

        Must not hang the daemon either — a wedged ``cam`` subprocess
        times out into a partial result rather than blocking a process
        that is serving live streams; the timed-out backend likewise
        lands in ``skipped``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement CameraDiscovery.probe")

    def watch(self) -> None:
        """Begin passive hotplug watching for the daemon's lifetime."""
        raise NotImplementedError(f"{type(self).__name__} does not implement CameraDiscovery.watch")


class PresenceSource:
    """Cheap answer to "which hardware keys are attached right now?".

    A plain base class, not ``Protocol`` (sprint 047). Implementations
    must only list -- never open a device -- so the daemon can poll
    them every fraction of a second, including for cameras it holds
    open (enumeration, not read failure, is the unplug signal). Keys
    use the same ``hardware_key`` strings ``CameraIdentity`` carries.
    A source covers only the keys it can express; a held camera whose
    key a source has never reported is never dropped on its account.
    """

    def present_keys(self) -> FrozenSet[str]:
        """Hardware keys of the cameras currently attached."""
        raise NotImplementedError(f"{type(self).__name__} does not implement PresenceSource.present_keys")


class Camera:
    """One physical camera, owned end to end.

    A plain base class, not ``Protocol`` — see the module docstring.

    The object the daemon manages one-of-per-camera. Owns its backend,
    its configuration, and its calibration when it has one.
    """

    @property
    def slug(self) -> str:
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.slug")

    @property
    def state(self) -> CameraState:
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.state")

    @property
    def lifecycle(self) -> CameraLifecycle:
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.lifecycle")

    def drop(self) -> None:
        """Hardware gone: close the device, mark ABSENT, keep config."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.drop")

    def reopen(self) -> bool:
        """Bring the camera back to PRESENT with its full stored config.
        Never raises; returns whether it is PRESENT afterwards."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.reopen")

    def open(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.open")

    def close(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.close")

    def read(self) -> Optional[np.ndarray]:
        """Grab one raw frame, or ``None`` if the read failed.

        Repeated failures trigger the self-heal reopen path — v1 used 5
        consecutive failures to reopen, capped at 10 reopens. What
        happens when that budget is exhausted (mark dead? keep trying?)
        is unspecified and needs deciding.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.read")

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
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.apply_config")

    def effective_config(self) -> CameraConfig:
        """What is actually in force, introspected from the hardware.

        For an unconfigured camera this is the self-describing template:
        introspected facts plus **every settable key, blank** — the
        document every camera configuration starts from (UC-03.S-1,
        UC-25).
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.effective_config")

    def reset(self) -> None:
        """Re-apply the stored on-disk config, discarding live overrides."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Camera.reset")
