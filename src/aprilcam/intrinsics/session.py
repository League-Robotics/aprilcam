"""One live ChArUco intrinsics session on one camera (daemon-side).

A session wires the detector, the view collector and the lens solver
behind two threads (sprint 044 Decision 5):

* the **capture thread** reads frames from the injected *frame source*
  (the camera's ``Camera.read()``) at a capped rate, detects boards, and
  offers them to the :class:`ViewCollector`;
* the **solver thread** re-solves every :data:`SOLVE_EVERY` new views so a
  slow solve never stalls capture.

Both publish into one immutable :class:`IntrinsicsStatus` snapshot swapped
under a lock, so :meth:`status` never waits on a solve. A session holds
corner arrays only, never frames (Decision 4).

Everything time- and thread-dependent is injectable: :meth:`step` and
:meth:`maybe_solve` are the two halves of the loops and can be driven
synchronously, so tests need no thread timing.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Callable, Optional, Sequence

import cv2
import numpy as np

from aprilcam.calibration.lens import LENS_SOURCE_MEASURED, LensModel
from aprilcam.intrinsics.collector import MIN_VIEWS, REGION_NAMES, ViewCollector
from aprilcam.intrinsics.detector import BoardDetector
from aprilcam.intrinsics.interfaces import (
    STATE_ABORTED,
    STATE_COLLECTING,
    STATE_FAILED,
    STATE_FINISHED,
    BoardSpec,
    IntrinsicsFinishResult,
    IntrinsicsStatus,
    LensEvaluation,
    derive_banner,
)
from aprilcam.intrinsics.solver import MIN_SOLVE_VIEWS, LensSolver, next_hint

logger = logging.getLogger(__name__)

#: Capture rate cap (frames per second).
CAPTURE_FPS = 8.0
#: Re-solve after this many new kept views.
SOLVE_EVERY = 3
#: Consecutive failed reads before the camera is declared unreadable.
MAX_READ_FAILURES = 24
#: How long a finished solver wait blocks before re-checking the stop flag.
_SOLVER_POLL_S = 0.25
_JOIN_TIMEOUT_S = 10.0

RUN_CALIBRATE_NOTE = "installed; run calibrate to apply it"

FrameSource = Callable[[], Optional[np.ndarray]]


class IntrinsicsSession:
    """Collect views, solve incrementally, and finish with an optional install."""

    def __init__(
        self,
        camera: str,
        spec: BoardSpec,
        frame_source: FrameSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        frame_interval: float = 1.0 / CAPTURE_FPS,
        detector=None,
        collector: Optional[ViewCollector] = None,
    ) -> None:
        self.camera = camera
        self._spec = spec
        self._frame_source = frame_source
        self._clock = clock
        self._wall_clock = wall_clock
        self.frame_interval = frame_interval
        self._detector = detector if detector is not None else BoardDetector(spec)
        self._collector = collector if collector is not None else ViewCollector(spec)
        self._lock = threading.Lock()
        self._status = IntrinsicsStatus(
            camera=camera, board_spec=str(spec), started_at=clock())
        self._image_size: Optional[tuple[int, int]] = None
        self._read_failures = 0
        self._solved_count = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ended = False

    # ---- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Spawn the capture and solver threads."""
        self._spawn()

    def _spawn(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._capture_loop, daemon=True,
                             name=f"intrinsics-capture-{self.camera}"),
            threading.Thread(target=self._solver_loop, daemon=True,
                             name=f"intrinsics-solver-{self.camera}"),
        ]
        for t in self._threads:
            t.start()

    def _halt(self) -> bool:
        """Stop and join the threads; ``True`` if any were running."""
        was_running = bool(self._threads)
        self._stop.set()
        self._wake.set()
        me = threading.current_thread()
        for t in self._threads:
            if t is not me:
                t.join(timeout=_JOIN_TIMEOUT_S)
        self._threads = []
        self._wake.clear()
        return was_running

    @property
    def ended(self) -> bool:
        return self._ended

    def status(self) -> IntrinsicsStatus:
        """The latest immutable snapshot; never waits on a solve."""
        with self._lock:
            return self._status

    # ---- capture half ---------------------------------------------------------

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            if not self.step():
                return
            remaining = self.frame_interval - (time.monotonic() - t0)
            self._stop.wait(max(0.0, remaining))

    def step(self) -> bool:
        """Read and process one frame; ``False`` once the session has failed."""
        if self.status().state != STATE_COLLECTING:
            return False
        try:
            frame = self._frame_source()
        except Exception as exc:  # a frame source must not take the daemon down
            frame = None
            logger.warning("intrinsics frame source raised: %s", exc)
        if frame is None:
            self._read_failures += 1
            if self._read_failures >= MAX_READ_FAILURES:
                self._fail(f"camera {self.camera!r} is not delivering frames "
                           f"({self._read_failures} consecutive failed reads)")
                return False
            return True
        self._read_failures = 0
        h, w = frame.shape[:2]
        if self._image_size is None:
            self._image_size = (w, h)
            self._update(image_size=(w, h))
        elif self._image_size != (w, h):
            ow, oh = self._image_size
            self._fail(f"camera resolution changed mid-session ({ow}x{oh} -> {w}x{h}); "
                       "abort and start a new session at the new resolution")
            return False
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        try:
            boards = self._detector.detect(gray)
            verdicts = self._collector.offer(gray, boards, self._wall_clock())
        except Exception as exc:
            logger.exception("intrinsics detection failed")
            self._fail(f"board detection failed: {exc}")
            return False
        reject = None
        if verdicts and not any(v.accepted for v in verdicts):
            first = verdicts[0].reason
            reject = first.value if first is not None else None
        cov = self._collector.coverage()
        cells = dict(zip(REGION_NAMES, cov.region_counts))
        self._update(
            board_visible=bool(boards),
            layout=self._detector.layout,
            view_count=cov.view_count,
            tilted_count=cov.tilted_count,
            cell_coverage=cells,
            tilt_coverage=dict(cov.tilt_directions),
            last_reject=reject,
            next_hint=next_hint(cells, cov.tilt_directions, cov.tilted_count,
                                cov.view_count),
        )
        if cov.view_count - self._solved_count >= SOLVE_EVERY:
            self._wake.set()
        return True

    # ---- solver half ----------------------------------------------------------

    def _solver_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=_SOLVER_POLL_S)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.maybe_solve()
            except Exception:
                logger.exception("intrinsics background solve failed")

    def maybe_solve(self) -> bool:
        """Re-solve if >= SOLVE_EVERY new views arrived; ``True`` if it solved."""
        views = self._collector.views
        n = len(views)
        if (self._image_size is None or n < MIN_SOLVE_VIEWS
                or n - self._solved_count < SOLVE_EVERY):
            return False
        self._solved_count = n
        ev = LensSolver(self._spec, self._image_size).evaluate(views)
        if ev is not None:
            self._publish_eval(ev)
        return ev is not None

    def _publish_eval(self, ev: LensEvaluation) -> None:
        sol = ev.solution
        self._update(
            rms_px=ev.rms_px, residual_px=ev.residual_px,
            correction_px=ev.correction_px, fx=sol.fx, hfov_deg=sol.hfov_deg,
            good=ev.good, solved_at=self._wall_clock())

    # ---- finishing ------------------------------------------------------------

    def finish(
        self,
        install: bool,
        force: bool = False,
        install_cb: Optional[Callable[[LensModel], None]] = None,
    ) -> IntrinsicsFinishResult:
        """Stop capture, solve everything once more, and optionally install.

        A refused install (not good / implausible / too few views) leaves
        the session collecting so the person can keep going. Otherwise the
        session ends.
        """
        was_running = self._halt()
        done = False
        try:
            st = self.status()
            if st.state == STATE_FAILED:
                done = True
                return IntrinsicsFinishResult(
                    installed=False, status=st,
                    reason="session failed: " + "; ".join(st.errors))
            if st.state != STATE_COLLECTING:
                done = True
                return IntrinsicsFinishResult(
                    installed=False, status=st, reason=f"session already {st.state}")
            views = self._collector.views
            ev = (LensSolver(self._spec, self._image_size).evaluate(views)
                  if self._image_size is not None else None)
            if ev is not None:
                self._publish_eval(ev)
            lens = _lens_summary(ev) if ev is not None else None
            if not install:
                done = True
                self._update(state=STATE_FINISHED)
                return IntrinsicsFinishResult(
                    installed=False, status=self.status(), lens=lens,
                    reason="install not requested")
            refusal = None
            if ev is None:
                refusal = (f"not enough views to solve ({len(views)} usable; "
                           f"need {MIN_SOLVE_VIEWS}, ideally {MIN_VIEWS}+)")
            elif not force:
                if ev.solution.plausibility_error:
                    refusal = f"implausible lens: {ev.solution.plausibility_error}"
                elif not ev.good:
                    refusal = ("result is not good enough yet; keep collecting "
                               "views or pass force to install anyway")
            if refusal is not None:
                return IntrinsicsFinishResult(
                    installed=False, status=self.status(), lens=lens, reason=refusal)
            assert ev is not None
            if install_cb is None:
                raise RuntimeError("finish(install=True) needs an install callback")
            install_cb(self._model_from(ev))
            done = True
            self._update(state=STATE_FINISHED)
            return IntrinsicsFinishResult(
                installed=True, status=self.status(), lens=lens,
                reason=RUN_CALIBRATE_NOTE)
        finally:
            if done:
                self._ended = True
            elif was_running:
                self._spawn()

    def abort(self, reason: Optional[str] = None) -> IntrinsicsStatus:
        """End the session without installing anything. Idempotent."""
        self._halt()
        self._ended = True
        st = self.status()
        if st.state == STATE_COLLECTING:
            kw: dict = {"state": STATE_ABORTED}
            if reason:
                kw["errors"] = st.errors + (reason,)
            self._update(**kw)
        return self.status()

    # ---- internals ------------------------------------------------------------

    def _model_from(self, ev: LensEvaluation) -> LensModel:
        sol = ev.solution
        spec = self._spec
        return LensModel(
            camera_matrix=_as_matrix3(sol.camera_matrix),
            dist_coeffs=tuple(sol.dist_coeffs),
            image_size=(int(sol.image_size[0]), int(sol.image_size[1])),
            source=LENS_SOURCE_MEASURED,
            board_spec={"cols": spec.cols, "rows": spec.rows,
                        "square_mm": spec.square_mm, "marker_mm": spec.marker_mm,
                        "dict": spec.dict_name, "spec": str(spec)},
            views=sol.view_count,
            rms_px=ev.rms_px,
            residual_px=ev.residual_px,
            correction_px=ev.correction_px,
            installed_at=self._wall_clock(),
        )

    def _fail(self, message: str) -> None:
        st = self.status()
        self._update(state=STATE_FAILED, errors=st.errors + (message,))
        self._stop.set()

    def _update(self, **changes) -> None:
        with self._lock:
            s = dataclasses.replace(self._status, **changes)
            self._status = dataclasses.replace(
                s, banner=derive_banner(board_visible=s.board_visible,
                                        view_count=s.view_count, good=s.good))


def _as_matrix3(m: Sequence[Sequence[float]] | np.ndarray) -> tuple[
        tuple[float, float, float], tuple[float, float, float],
        tuple[float, float, float]]:
    """A 3x3 array as the tuple-of-rows shape :class:`LensModel` stores."""
    a = np.asarray(m, dtype=np.float64).reshape(3, 3)
    return (
        (float(a[0, 0]), float(a[0, 1]), float(a[0, 2])),
        (float(a[1, 0]), float(a[1, 1]), float(a[1, 2])),
        (float(a[2, 0]), float(a[2, 1]), float(a[2, 2])),
    )


def _lens_summary(ev: LensEvaluation) -> dict:
    sol = ev.solution
    return {
        "camera_matrix": [list(r) for r in sol.camera_matrix],
        "dist_coeffs": list(sol.dist_coeffs),
        "image_size": list(sol.image_size),
        "fx": sol.fx, "hfov_deg": sol.hfov_deg, "views": sol.view_count,
        "rms_px": ev.rms_px, "residual_px": ev.residual_px,
        "correction_px": ev.correction_px, "good": ev.good,
        "plausibility_error": sol.plausibility_error,
    }
