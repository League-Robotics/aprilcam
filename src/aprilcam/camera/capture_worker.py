"""Per-camera capture worker child process (sprint 038 ticket 004).

Runs as ``python -m aprilcam.camera.capture_worker`` -- the standalone
child-process entry point ``camera/backends/worker_backend.py``'s
``WorkerBackend`` spawns one of per opened camera (the only thing that
spawns it; nothing else imports this module -- see that module's own
docstring and the sprint's ``design/camera-DESIGN.md`` overlay
component diagram: this is a new *process* boundary, not a new
*import* edge).

**Why a worker process at all.** The sprint's root-cause finding
(``camera-DESIGN.md`` §3, "The AVFoundation capture-session wedge is
process-local") is that a wedged ``cv2.VideoCapture`` cannot be cleared
by anything that stays inside the daemon process -- only a fresh
process reading the same camera index works. This module is that fresh
process: it owns exactly one real
:class:`~aprilcam.camera.backends.opencv_backend.OpenCVBackend`
(imported lazily, see below), publishes every frame it reads into a
:class:`~aprilcam.camera.backends._frame_ring.FrameRing` it creates the
moment the first frame's shape/dtype is known, and answers the
parent's control-channel requests
(``get_property``/``set_property``/``settable_names``/``frame_size``,
ticket 003's ``_control_channel.py``) against that same backend
object -- so the settle-after-open reads ``Camera.apply_config``
relies on one layer up (``camera/DESIGN.md`` §3's "Settle-after-open"
constraint) fall out automatically: they are simply this worker's own
ordinary read loop, since the real ``cv2.VideoCapture`` only exists in
here.

**Lazy ``cv2`` import.** ``OpenCVBackend`` (and therefore ``cv2``) is
imported only inside :func:`main`'s non-synthetic branch, mirroring
``camera/identity.py``'s "never import cv2 at module top level" rule
-- so this module, and in particular :class:`SyntheticBackend`, stay
importable (and this file's tests runnable) with no ``cv2`` installed
at all.

**The synthetic frame source.** :class:`SyntheticBackend` is the
"internal seam" the ticket calls for instead of monkeypatching
``cv2.VideoCapture`` directly: a fake :class:`CaptureBackend` that
generates frames of a fixed shape at a fixed rate, with a couple of
fake settable properties, selected via ``--synthetic WxH@fps``. This
is what lets ``main()`` run as a *real* subprocess in tests (sprint.md
Test Strategy: no real camera hardware in this ticket's tests) while
still exercising the real process boundary, ring hand-off, and
control-channel proxying end to end.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from types import FrameType
from multiprocessing.connection import Client, Connection
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from aprilcam.camera.backends import CaptureBackend
from aprilcam.camera.backends._control_channel import ControlHandler, serve_one
from aprilcam.camera.backends._frame_ring import DEFAULT_RING_SLOTS, FrameRing

__all__ = ["SyntheticBackend", "main", "run_worker"]

logger = logging.getLogger(__name__)

_SYNTHETIC_RE = re.compile(r"^\s*(\d+)x(\d+)@([0-9.]+)\s*$")

#: Idle sleep between loop iterations when the backend's read() returned
#: no frame -- avoids a busy CPU spin for a backend whose read() itself
#: returns instantly on an ordinary miss instead of blocking.
_IDLE_SLEEP = 0.01


class SyntheticBackend(CaptureBackend):
    """A fake capture source: generates ``(height, width, channels)``
    ``uint8`` frames at a fixed rate, with a couple of fake settable
    properties (``brightness``, ``gain``) mirroring
    :class:`~aprilcam.camera.backends.opencv_backend.OpenCVBackend`'s
    duck-typed ``get_property``/``set_property``/``settable_names``
    surface -- so control-channel round trips have something real to
    exercise with no camera hardware and no ``cv2`` import at all.

    Each frame's pixels encode a monotonically increasing counter (mod
    256) so a test can tell distinct frames apart without needing a
    real image.
    """

    def __init__(self, width: int, height: int, fps: float, *, channels: int = 3) -> None:
        self._width = width
        self._height = height
        self._fps = fps
        self._channels = channels
        self._open = False
        self._frame_count = 0
        self._properties: Dict[str, float] = {"brightness": 50.0, "gain": 1.0}

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True
        self._frame_count = 0

    def read(self) -> Optional[np.ndarray]:
        if not self._open:
            return None
        if self._fps > 0:
            time.sleep(1.0 / self._fps)
        value = self._frame_count % 256
        frame = np.full((self._height, self._width, self._channels), value, dtype=np.uint8)
        self._frame_count += 1
        return frame

    def close(self) -> None:
        self._open = False

    def frame_size(self) -> Optional[Tuple[int, int]]:
        if not self._open:
            return None
        return (self._width, self._height)

    # -- best-effort settings, beyond the CaptureBackend base class,
    # mirroring OpenCVBackend's own duck-typed surface ------------------

    def get_property(self, name: str) -> Optional[float]:
        return self._properties.get(name)

    def set_property(self, name: str, value: float) -> bool:
        if name not in self._properties:
            return False
        self._properties[name] = value
        return True

    def settable_names(self) -> Set[str]:
        return set(self._properties)


class _WorkerControlHandler(ControlHandler):
    """Answers control-channel requests against whichever backend this
    worker owns (a real ``OpenCVBackend`` or a test
    :class:`SyntheticBackend`) -- duck-typed the same way
    ``OpenCVBackend``'s own module docstring already treats
    ``get_property``/``set_property``/``settable_names`` as optional,
    beyond the plain :class:`CaptureBackend` base."""

    def __init__(self, backend: CaptureBackend) -> None:
        self._backend = backend

    def get_property(self, name: str) -> Optional[float]:
        getter = getattr(self._backend, "get_property", None)
        if getter is None:
            return None
        result = getter(name)
        return None if result is None else float(result)

    def set_property(self, name: str, value: float) -> bool:
        setter = getattr(self._backend, "set_property", None)
        if setter is None:
            return False
        return bool(setter(name, value))

    def settable_names(self) -> List[str]:
        names = getattr(self._backend, "settable_names", None)
        if names is None:
            return []
        return sorted(names())

    def frame_size(self) -> Optional[Tuple[int, int]]:
        return self._backend.frame_size()


def _parse_synthetic(spec: str) -> Tuple[int, int, float]:
    match = _SYNTHETIC_RE.match(spec)
    if not match:
        raise ValueError(f"--synthetic value {spec!r} is not of the form WxH@fps, e.g. '640x480@30'")
    width, height, fps = match.groups()
    return int(width), int(height), float(fps)


def _send_ready_ok(conn: Connection, ring: FrameRing, frame: np.ndarray) -> None:
    message: Dict[str, Any] = {
        "type": "ready",
        "ok": True,
        "ring_name": ring.name,
        "shape": list(frame.shape),
        "dtype": str(frame.dtype),
    }
    conn.send_bytes(json.dumps(message).encode("utf-8"))


def _send_ready_error(conn: Connection, error: str) -> None:
    message: Dict[str, Any] = {"type": "ready", "ok": False, "error": error}
    conn.send_bytes(json.dumps(message).encode("utf-8"))


def run_worker(
    backend: CaptureBackend,
    conn: Connection,
    *,
    ring_slots: int = DEFAULT_RING_SLOTS,
) -> int:
    """Own *backend* end to end and return the process exit code.

    Opens *backend*, then loops: read a frame, publish it into a
    :class:`FrameRing` (created lazily the first time a frame's shape/
    dtype is known, then reported to the parent over *conn* as a
    ``{"type": "ready", ...}`` handshake -- distinct from, and sent
    before, any of ticket 003's request/response traffic on the same
    connection), answer at most one pending control-channel request per
    iteration via :func:`~aprilcam.camera.backends._control_channel.serve_one`,
    and repeat until SIGTERM is received or the parent process exits
    (``os.getppid()`` changing identifies a re-parented, orphaned
    worker).

    Split out from :func:`main` so tests can drive it directly against
    an already-connected ``Connection`` and an injected backend without
    going through argument parsing or a real subprocess spawn, and so
    ``main()`` itself stays a thin argument-parsing/wiring shell.

    If *backend* itself fails to ``open()``, a ``{"ok": False, ...}``
    ready message is sent instead and this returns ``1`` -- matching
    ``CaptureBackend.open()``'s "raises on failure" contract one layer
    down, translated into an exit code plus a diagnosable message for
    the parent (``WorkerBackend.open()`` turns that into a raised
    ``WorkerStartupError``) rather than a bare nonzero exit with no
    detail.
    """
    stop_requested = False

    def _handle_sigterm(signum: int, frame: Optional[FrameType]) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    parent_pid = os.getppid()
    handler = _WorkerControlHandler(backend)
    ring: Optional[FrameRing] = None
    try:
        try:
            backend.open()
        except Exception as exc:  # noqa: BLE001 -- any open failure must be reported, never crash silently
            _send_ready_error(conn, str(exc))
            return 1

        while not stop_requested and os.getppid() == parent_pid:
            frame = backend.read()
            if frame is None:
                time.sleep(_IDLE_SLEEP)
            else:
                if ring is None:
                    ring = FrameRing.create(shape=frame.shape, dtype=frame.dtype, num_slots=ring_slots)
                    _send_ready_ok(conn, ring, frame)
                ring.publish(frame)
            serve_one(conn, handler, timeout=0)
        return 0
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001 -- shutdown must not raise past this point
            logger.exception("capture_worker: error closing backend during shutdown")
        if ring is not None:
            ring.close()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        signal.signal(signal.SIGTERM, previous_handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aprilcam.camera.capture_worker")
    parser.add_argument(
        "--control-address",
        required=True,
        help="multiprocessing.connection address to connect back to the parent's Listener",
    )
    parser.add_argument("--index", type=int, default=None, help="camera index for a real OpenCVBackend")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--backend", type=int, default=None, help="force a specific cv2 backend constant")
    parser.add_argument(
        "--synthetic",
        default=None,
        help="WxH@fps synthetic frame source instead of a real camera (tests only)",
    )
    parser.add_argument("--ring-slots", type=int, default=DEFAULT_RING_SLOTS)
    args = parser.parse_args(argv)

    backend: CaptureBackend
    if args.synthetic is not None:
        width, height, fps = _parse_synthetic(args.synthetic)
        backend = SyntheticBackend(width, height, fps)
    else:
        if args.index is None:
            parser.error("--index is required unless --synthetic is given")
        from aprilcam.camera.backends.opencv_backend import OpenCVBackend  # noqa: PLC0415 -- lazy, see module docstring

        backend = OpenCVBackend(args.index, width=args.width, height=args.height, backend=args.backend)

    conn = Client(args.control_address)
    return run_worker(backend, conn, ring_slots=args.ring_slots)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
