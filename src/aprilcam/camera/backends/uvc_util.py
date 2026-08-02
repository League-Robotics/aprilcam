"""``uvc-util``-backed UVC control — the real per-control mechanism this
sprint's real-hardware validation (ticket 023-001) found necessary on
macOS.

**Validated live against camera 3** (``arducam-ov9782-usb-camera``, the
OV9782-class hardware this module exists for). Pushing
``auto_exposure``/``exposure``/``gain``/``auto_white_balance``/
``white_balance_temperature`` through ``OpenCVBackend.set_property`` —
i.e. ``cv2.VideoCapture.set()`` — returned ``False`` for **every one** of
these five controls on this exact camera's AVFoundation capture session;
``Camera.apply_config`` correctly turned each into ``UnsupportedSetting``
rather than a silent no-op, but the push could never actually succeed
through that path. This is not the ambiguous "reports success but
nothing changes" failure mode — OpenCV itself refuses — but it is still
the "a generic capture-library property setter is not to be trusted on
this hardware" outcome ``archive/src/aprilcam/camera/DESIGN.md`` and
``archive/camera_setup.sh`` already recorded for v1, now confirmed for
``cap.set()`` specifically rather than only for ``uvcc`` (sprint.md
Decision 2). This module is v1's proven answer, ported: shell out to the
native ``uvc-util`` CLI (present at ``~/.local/bin/uvc-util`` on this dev
machine; resolved generically via ``PATH``, never hardcoded).

**Invocation is always an argument list, never a shell string**
(``subprocess.run([...], shell=False)``) — a config-document value
reaching a subprocess command line is exactly the shape of thing that
must not become an injection surface, even on a trusted local network
(sprint.md Decision 2).

**Control names and ranges** mirror ``archive/camera_setup.sh``'s proven
sequence exactly: ``auto-exposure-mode`` (1=manual, 8=aperture-priority,
the default) before ``exposure-time-abs`` (1..5000, 100µs ticks);
``auto-white-balance-temp`` (boolean) before ``white-balance-temp``
(2800..6500 Kelvin, default 4600); ``gain`` (0..100, default 0).
Ordering itself is not this module's job — ``Camera.apply_config``'s
``_GATE_KEYS`` already pushes the two auto-mode keys first; this module
only has to get one control right per call.

**Never trusts its own write.** ``set_property`` reads the control back
after writing it and compares against what was requested, returning
``False`` (which ``Camera.apply_config`` turns into ``UnsupportedSetting``,
rolling back everything already applied this call) unless the hardware
actually reports the new value — "must never report success for a
setting that did not take effect" (UC-03, ``errors.UnsupportedSetting``'s
own docstring) applied literally at the backend boundary, not just as an
external verification step.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Dict, Optional, Set, Tuple

from aprilcam.camera.backends import ControlBackend

__all__ = ["UvcUtilBackend"]

logger = logging.getLogger(__name__)

#: ``CameraConfig.settings`` key -> (``uvc-util`` control name, is_boolean).
#: The five controls ticket 023-001 validated live; OV9782 ranges from
#: ``archive/camera_setup.sh``'s "queried against this exact hardware"
#: comment. Resolution (``frame_width``/``frame_height``) deliberately
#: does not appear here — it is not a UVC control and stays on the
#: OpenCV capture path regardless (sprint.md, ticket 001's Description).
_CONTROLS: Dict[str, Tuple[str, bool]] = {
    "auto_exposure": ("auto-exposure-mode", False),
    "exposure": ("exposure-time-abs", False),
    "gain": ("gain", False),
    "auto_white_balance": ("auto-white-balance-temp", True),
    "white_balance_temperature": ("white-balance-temp", False),
}

#: Wall-clock budget for one ``uvc-util`` invocation — a wedged
#: subprocess must not hang the daemon (mirrors
#: ``camera.discovery.CameraDiscovery.probe``'s own subprocess-timeout
#: discipline for the ``cam`` tool).
_SUBPROCESS_TIMEOUT_S = 5.0

_CURRENT_VALUE_RE = re.compile(r"current-value:\s*(\S+)")


def _format_value(value: float, *, is_boolean: bool) -> str:
    if is_boolean:
        return "true" if value else "false"
    return str(int(round(value)))


def _parse_value(raw: str, *, is_boolean: bool) -> Optional[float]:
    lowered = raw.strip().lower()
    if lowered == "true":
        return 1.0
    if lowered == "false":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return None


class UvcUtilBackend(ControlBackend):
    """Adapts the ``uvc-util`` CLI to
    :class:`~aprilcam.camera.backends.ControlBackend`.

    Derives from ``ControlBackend`` (a plain base class, not
    ``Protocol`` — see ``camera/backends/__init__.py``'s module
    docstring). Control-only: no ``open``/``read``/``close`` — pixels
    still come from ``OpenCVBackend`` (``camera/camera.py``'s ``Camera``
    holds both: one for capture, this one for settings).

    Parameters
    ----------
    device_name:
        The exact ``-N`` target ``uvc-util -d`` lists (e.g.
        ``"Arducam OV9782 USB Camera"``) — the OS-reported device
        descriptor (``CameraIdentity.device_name`` /
        ``CameraRecord.device_name``), not the daemon's stable slug.
    binary:
        Path to the ``uvc-util`` executable. Resolved via ``shutil.which``
        against ``PATH`` when not given — never a hardcoded path, so
        this works the same on any machine that has it installed
        anywhere on ``PATH`` (this dev machine: ``~/.local/bin/uvc-util``).

    Raises
    ------
    RuntimeError
        If *binary* is not given and ``uvc-util`` is not found on
        ``PATH`` — fails loudly at construction, never a silent
        fallback to a control path already found not to work
        (sprint.md Migration Concerns: "its absence must surface as a
        clear, typed error... never a silent fallback").
    """

    def __init__(self, device_name: str, *, binary: Optional[str] = None) -> None:
        resolved = binary if binary is not None else shutil.which("uvc-util")
        if resolved is None:
            raise RuntimeError(
                "uvc-util not found on PATH -- required for exposure/gain/"
                "white-balance control on this hardware (cv2.VideoCapture.set() "
                "does not work here; ticket 023-001's real-hardware finding). "
                "Install uvc-util and ensure it is on PATH."
            )
        self._binary = resolved
        self._device_name = device_name

    def settable_names(self) -> Set[str]:
        """The controls this backend can attempt — the five validated
        live against camera 3 (ticket 023-001), not an aspirational
        full UVC control list."""
        return set(_CONTROLS)

    def get_property(self, name: str) -> Optional[float]:
        """Current hardware value of *name*, or ``None`` if unknown to
        this backend or the query failed (device unplugged, ``uvc-util``
        error, malformed output) — never raises."""
        control = _CONTROLS.get(name)
        if control is None:
            return None
        ctrl_name, is_boolean = control
        try:
            result = subprocess.run(
                [self._binary, "-N", self._device_name, "-S", ctrl_name],
                shell=False,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("uvc-util -S %s failed: %s", ctrl_name, exc)
            return None
        if result.returncode != 0:
            return None
        match = _CURRENT_VALUE_RE.search(result.stdout)
        if match is None:
            return None
        return _parse_value(match.group(1), is_boolean=is_boolean)

    def _raw_set(self, ctrl_name: str, formatted: str) -> bool:
        """One ``uvc-util -s ctrl=value`` call, unverified -- the
        primitive :meth:`set_property` builds its verify-then-repair
        behaviour on top of, reused for the mismatch-repair write below
        without recursing back through verification."""
        try:
            result = subprocess.run(
                [self._binary, "-N", self._device_name, "-s", f"{ctrl_name}={formatted}"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("uvc-util -s %s=%s failed: %s", ctrl_name, formatted, exc)
            return False
        if result.returncode != 0:
            logger.warning(
                "uvc-util -s %s=%s exited %d: %s",
                ctrl_name,
                formatted,
                result.returncode,
                result.stderr.strip(),
            )
            return False
        return True

    def set_property(self, name: str, value: float) -> bool:
        """Attempt to set *name* to *value*, verifying by reading it
        back — see the module docstring's "never trusts its own write".
        Unknown names return ``False`` rather than raising, matching
        ``OpenCVBackend.set_property``'s contract (``Camera.
        apply_config`` is what turns a ``False`` into the typed
        ``UnsupportedSetting`` its callers see).

        **Self-repairs a mismatch, never just reports one.** Real-hardware
        testing (ticket 023-001, live against camera 3) found that
        ``uvc-util`` can report success (exit 0) for an out-of-range
        request while silently *clamping* it to the nearest valid bound
        instead of rejecting it outright — e.g. requesting
        ``exposure-time-abs=99999`` (max 5000) leaves the control at
        ``5000``, not at whatever it was before the call. A bare
        "verify and return False on mismatch" would report the push as
        correctly rejected while leaving the *hardware* changed anyway —
        exactly the half-configured state UC-03 forbids, and outside
        ``Camera.apply_config``'s own rollback loop, which only reverts
        keys this method already reported success for. So on a mismatch
        this method writes the pre-call value straight back (best
        effort -- its own success is not re-verified, to avoid
        unbounded recursion) before returning ``False``, making "this
        call returned ``False``" mean "the hardware is unchanged",
        unconditionally.
        """
        control = _CONTROLS.get(name)
        if control is None:
            return False
        ctrl_name, is_boolean = control
        formatted = _format_value(value, is_boolean=is_boolean)

        original = self.get_property(name)  # snapshot, for repair below

        if not self._raw_set(ctrl_name, formatted):
            return False

        readback = self.get_property(name)
        if is_boolean:
            matched = readback == (1.0 if value else 0.0)
        else:
            # Numeric controls: uvc-util may snap the requested value to
            # the nearest valid step -- a small tolerance absorbs that
            # without accepting a readback that is not really what was
            # requested.
            tolerance = max(1.0, abs(value) * 0.05)
            matched = readback is not None and abs(readback - value) <= tolerance
        if matched:
            return True

        if original is not None:
            self._raw_set(ctrl_name, _format_value(original, is_boolean=is_boolean))
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"UvcUtilBackend(device_name={self._device_name!r})"
