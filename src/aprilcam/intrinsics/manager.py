"""Owns every live intrinsics session: one per camera, idle watchdog, shutdown.

The manager is the only thing :class:`~aprilcam.daemon.service.DaemonService`
talks to for intrinsics. It owns session lifecycle (Decision 5): at most one
session per camera slug, a 120 s no-poll auto-abort, and abort-everything on
shutdown. It knows nothing about cameras or stores -- the service supplies a
frame-source factory and an install callback.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple

import numpy as np

from aprilcam.calibration.lens import LensModel
from aprilcam.intrinsics.interfaces import (
    IntrinsicsFinishResult,
    IntrinsicsSessionActive,
    IntrinsicsStatus,
    NoIntrinsicsSession,
    resolve_board_spec,
)
from aprilcam.intrinsics.session import CAPTURE_FPS, IntrinsicsSession

logger = logging.getLogger(__name__)

#: A session nobody polls for this long is aborted (seconds).
IDLE_TIMEOUT_S = 120.0
#: How often the watchdog thread checks for idle sessions (seconds).
WATCHDOG_INTERVAL_S = 5.0

IDLE_ABORT_MESSAGE = "aborted: no status poll for {secs:.0f}s"


class IntrinsicsManager:
    """At most one :class:`IntrinsicsSession` per camera slug."""

    def __init__(
        self,
        *,
        frame_source_for: Callable[[str], Callable[[], Optional[np.ndarray]]],
        install: Callable[[str, LensModel], None],
        clock: Callable[[], float] = time.monotonic,
        idle_timeout: float = IDLE_TIMEOUT_S,
        frame_interval: float = 1.0 / CAPTURE_FPS,
        watchdog_interval: float = WATCHDOG_INTERVAL_S,
        session_factory: Callable[..., IntrinsicsSession] = IntrinsicsSession,
        threaded: bool = True,
    ) -> None:
        self._frame_source_for = frame_source_for
        self._install = install
        self._clock = clock
        self.idle_timeout = idle_timeout
        self.frame_interval = frame_interval
        self._watchdog_interval = watchdog_interval
        self._session_factory = session_factory
        self._threaded = threaded
        self._lock = threading.Lock()
        self._active: Dict[str, Tuple[IntrinsicsSession, float]] = {}
        self._ended: Dict[str, IntrinsicsStatus] = {}
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()

    # ---- public ---------------------------------------------------------------

    def start(self, slug: str, board: Optional[str] = None) -> IntrinsicsStatus:
        """Begin a fresh session on *slug*; raise if one is already active."""
        spec = resolve_board_spec(board)  # ValueError on a malformed spec
        with self._lock:
            if slug in self._active:
                raise IntrinsicsSessionActive(slug)
            session = self._session_factory(
                slug, spec, self._frame_source_for(slug),
                frame_interval=self.frame_interval)
            self._active[slug] = (session, self._clock())
            self._ended.pop(slug, None)
        if self._threaded:
            session.start()
            self._ensure_watchdog()
        return session.status()

    def status(self, slug: str) -> IntrinsicsStatus:
        """Latest status (counts as a poll); the final one after a session ends."""
        with self._lock:
            entry = self._active.get(slug)
            if entry is not None:
                self._active[slug] = (entry[0], self._clock())
            elif slug in self._ended:
                return self._ended[slug]
            else:
                raise NoIntrinsicsSession(slug)
        return entry[0].status()

    def finish(self, slug: str, install: bool, force: bool = False) -> IntrinsicsFinishResult:
        session = self._session(slug)
        result = session.finish(
            install, force, install_cb=lambda model: self._install(slug, model))
        if session.ended:
            self._retire(slug, session, result.status)
        return result

    def abort(self, slug: str) -> IntrinsicsStatus:
        with self._lock:
            entry = self._active.get(slug)
        if entry is None:
            # Aborting nothing is a no-op that reports whatever last happened.
            return self.status(slug)
        status = entry[0].abort()
        self._retire(slug, entry[0], status)
        return status

    def abort_all(self) -> None:
        """Abort every live session and stop the watchdog (daemon shutdown)."""
        self._watchdog_stop.set()
        with self._lock:
            sessions = list(self._active.items())
        for slug, (session, _) in sessions:
            try:
                self._retire(slug, session, session.abort("aborted: daemon shutting down"))
            except Exception:  # pragma: no cover - defensive
                logger.exception("error aborting intrinsics session %s", slug)
        wd = self._watchdog
        if wd is not None and wd is not threading.current_thread():
            wd.join(timeout=2.0)
        self._watchdog = None

    def check_idle(self) -> list:
        """Abort sessions not polled within the timeout; returns their slugs."""
        now = self._clock()
        with self._lock:
            stale = [(slug, s) for slug, (s, last) in self._active.items()
                     if now - last >= self.idle_timeout]
        aborted = []
        for slug, session in stale:
            msg = IDLE_ABORT_MESSAGE.format(secs=self.idle_timeout)
            self._retire(slug, session, session.abort(msg))
            aborted.append(slug)
        return aborted

    def active_slugs(self) -> list:
        with self._lock:
            return sorted(self._active)

    # ---- internals ------------------------------------------------------------

    def _session(self, slug: str) -> IntrinsicsSession:
        with self._lock:
            entry = self._active.get(slug)
        if entry is None:
            raise NoIntrinsicsSession(slug)
        return entry[0]

    def _retire(self, slug: str, session: IntrinsicsSession,
                status: IntrinsicsStatus) -> None:
        with self._lock:
            entry = self._active.get(slug)
            if entry is not None and entry[0] is session:
                del self._active[slug]
                self._ended[slug] = status

    def _ensure_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="intrinsics-watchdog")
        self._watchdog.start()

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._watchdog_interval):
            try:
                self.check_idle()
            except Exception:  # pragma: no cover - defensive
                logger.exception("intrinsics watchdog error")
