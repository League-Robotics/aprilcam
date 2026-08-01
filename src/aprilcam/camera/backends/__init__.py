"""One small adapter per capture technology (sprint 020 architecture,
module table).

``camera/camera.py``'s ``Camera`` class owns a physical camera end to
end — self-heal reopen, config application, state assembly — and does
so uniformly across whatever hardware it is backed by. A **backend** is
the small, swappable piece underneath that: it knows how to open, read,
and close *one kind* of capture device and nothing else. This keeps a
libcamera bring-up or a v4l2 quirk fix from touching ``camera.py``, and
vice versa (module table: "each backend changes independently of the
others").

:class:`CaptureBackend` is deliberately smaller than the public
``Camera`` Protocol (``camera/interfaces.py``) — no ``apply_config``,
no ``state``, no self-heal policy. Those live one layer up. A backend
is not itself ``Camera``-Protocol-conformant and is never handed
directly to a client; ``camera.py`` is the only consumer.

**Backends in this package, and what shipped this ticket (020-003):**

* :mod:`opencv_backend` — ``cv2.VideoCapture`` over USB/AVFoundation/
  V4L2 hardware. This is the backend both of this Mac's fixture
  cameras (``arducam-ov9782-usb-camera``, ``hd-usb-camera``) use, and
  the only one exercised against real hardware this ticket.
* :mod:`v4l2_loopback` — the ``v4l2-ctl`` subprocess reader for a Pi's
  v4l2loopback bridge device. Converted in full from v1's
  ``daemon/v4l2_reader.py`` (the ticket names no deferral option for
  it), but untestable against real frames on this Mac (no Linux/v4l2
  hardware) — its unit tests exercise the pure parsing/framing logic
  with a faked subprocess.
* :mod:`libcamera` — **enumeration only**. The ``/sys``-based
  v4l2loopback-label scan from v1's ``camera/libcam.py`` is ported
  (safe and headlessly testable: it degrades to an empty list on any
  non-Linux machine, this Mac included, via a plain ``glob`` miss).
  The ``cam``-tool / GStreamer "gst mode" half of v1's module, and an
  actual libcamera *capture* backend, are **not** ported this ticket —
  per the ticket's explicit allowance ("may be stubbed/deferred if it
  adds no test coverage this sprint"), since there is no Pi/libcamera
  device attached here to validate either against. The enumeration
  function is wired into ``camera/identity.py``'s ``_probe_libcamera``
  injection point (a safe, no-behaviour-change-on-this-Mac swap, since
  it is empty everywhere the v1 stub was empty); real capture is left
  for a future Raspberry Pi bring-up sprint.
* **Screen capture is not ported this ticket** — v1's
  ``camera/screencap.py`` (``mss``-based) is the lowest-priority item
  the ticket names and is not needed for any of this ticket's
  hardware-backed acceptance criteria. Deferred, no stub file; convert
  it if/when a use case actually needs a screen-capture "camera".
"""

from __future__ import annotations

from typing import Optional, Protocol, Tuple, runtime_checkable

import numpy as np

__all__ = ["CaptureBackend"]


@runtime_checkable
class CaptureBackend(Protocol):
    """The internal surface every ``backends/*.py`` adapter provides.

    Three methods, on purpose: a backend is *just* device I/O. Nothing
    here reports settings, nothing here decides how many times to
    retry — that policy is ``Camera``'s (``camera/camera.py``), applied
    uniformly across every backend so self-heal behaviour does not
    fork per capture technology.
    """

    @property
    def is_open(self) -> bool:
        """Whether the device is currently open."""
        ...

    def open(self) -> None:
        """Open the device.

        **Raises on failure** — never returns a sentinel "not open"
        value. This is the hook ``CameraDiscovery.probe()`` depends on
        to tell "present but unopenable" (UC-25.S-4) apart from a
        clean open: it calls this inside a ``try``/``except`` and
        reports ``usable=False`` on any exception, ``present`` coming
        from enumeration rather than from this call succeeding.
        """
        ...

    def read(self) -> Optional[np.ndarray]:
        """One frame, or ``None`` on a read failure.

        Never raises for an ordinary failed read (a dropped frame, a
        momentarily wedged driver) — only ``open()`` raises. This
        mirrors ``Camera.read()``'s own contract one layer up, so
        ``Camera`` can pass failures straight through into its
        consecutive-failure counter without translating exception
        types per backend.
        """
        ...

    def close(self) -> None:
        """Release the device. Safe to call when not open (no-op)."""
        ...

    def frame_size(self) -> Optional[Tuple[int, int]]:
        """``(width, height)`` once open, else ``None``.

        Feeds ``Camera.effective_config()``'s introspected-facts
        reporting (AC3) — never guessed, only read back from the open
        device.
        """
        ...
