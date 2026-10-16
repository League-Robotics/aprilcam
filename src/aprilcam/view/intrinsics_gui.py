"""Fullscreen Tk client for the ChArUco intrinsics workflow (sprint 044-008).

A thin shell: everything it draws comes from ``intrinsics_model`` (view
model, colours, font sizes, key decisions). Two pieces:

* :class:`IntrinsicsController` -- no Tk. A poller thread calls
  ``get_intrinsics_status`` and runs finish/abort RPCs, publishing a
  single-slot snapshot the UI thread samples (the ``StreamLoop`` pattern), so
  a slow or dead daemon never blocks the window.
* :class:`_TkShell` / :func:`run_intrinsics_gui` -- the fullscreen window.

Never auto-quits on green: only Enter (once nothing is left to install) or
Esc / window close ends the run. No cv2 here.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from aprilcam.intrinsics.interfaces import IntrinsicsStatus
from aprilcam.view.intrinsics_model import (
    build_view_model,
    colour_for,
    connection_banner,
    decide_enter,
    exit_code_for,
    finish_text_lines,
    font_sizes,
    fit_rect,
    key_action,
    tile_colour,
)
from aprilcam.view.intrinsics_overlay import compose_camera_image
from aprilcam.view.viewer import NoDisplayAvailable, _ensure_tk_library_env, _headless_reason

__all__ = ["IntrinsicsController", "Snapshot", "run_intrinsics_gui"]

_POLL_INTERVAL_S = 0.22
_FRAME_INTERVAL_S = 0.22
_DRAW_INTERVAL_MS = 150
_BG = "#101418"


class Snapshot:
    """Immutable-by-convention view of the controller state for one redraw."""

    def __init__(self, statuses, notice_lines, error, finished, frames=None, frame_stale=None):
        self.frames = frames if frames is not None else {}
        self.frame_stale = frame_stale if frame_stale is not None else {}
        self.statuses = statuses
        self.notice_lines = notice_lines
        self.error = error
        self.finished = finished


class IntrinsicsController:
    """Session bookkeeping and RPCs, independent of Tk (testable directly)."""

    def __init__(self, daemon, cameras: list, *, install: bool,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._daemon = daemon
        self._clock = clock
        self._frames: dict = {}
        self._frame_stale: dict = {}
        self._frame_errors: dict = {}
        self._last_frame_try: dict = {}
        self.cameras = list(cameras)
        self._install = install
        self.owned: set = set(cameras)
        self.installed: set = set()
        self._auto_tried: set = set()
        self._lock = threading.Lock()
        self._commands: list = []
        self._statuses: dict = {}
        self._notice: list = []
        self._error: Optional[str] = None
        self.finished = False
        self._stop = threading.Event()

    # -- UI-thread entry points (cheap, never touch the daemon) ------------
    def request(self, action: str) -> None:
        """Queue ``enter`` / ``escape`` / ``close`` for the poller thread."""
        with self._lock:
            self._commands.append(action)

    def snapshot(self) -> Snapshot:
        with self._lock:
            ordered = [self._statuses[c] for c in self.cameras if c in self._statuses]
            error = self._error
            if error is None and self._frame_errors:
                error = next(iter(self._frame_errors.values()))
            return Snapshot(ordered, list(self._notice), error, self.finished,
                            dict(self._frames), dict(self._frame_stale))

    def exit_code(self) -> int:
        return exit_code_for(sorted(self.installed), self.cameras)

    # -- poller-thread work -------------------------------------------------
    def step(self) -> None:
        """One poller iteration: run queued commands, poll, auto-install."""
        with self._lock:
            cmds, self._commands = self._commands, []
        for cmd in cmds:
            self._run_command(cmd)
            if self.finished:
                return
        error = None
        for cam in list(self.owned):
            try:
                st = self._daemon.get_intrinsics_status(cam)
            except Exception as exc:  # noqa: BLE001 - shown as a banner, retried
                error = str(exc) or type(exc).__name__
                continue
            with self._lock:
                self._statuses[cam] = st
        with self._lock:
            self._error = error
        self._poll_frames()
        if self._install:
            for cam in list(self.owned):
                st = self._statuses.get(cam)
                if st is not None and st.good and cam not in self._auto_tried:
                    self._auto_tried.add(cam)
                    self._finish(cam)

    def _poll_frames(self) -> None:
        """Fetch one JPEG per owned camera, at most every ``_FRAME_INTERVAL_S``.
        A failure keeps the previous bytes (marked stale) and never touches the
        status result."""
        for cam in list(self.owned):
            now = self._clock()
            last = self._last_frame_try.get(cam)
            if last is not None and now - last < _FRAME_INTERVAL_S:
                continue
            self._last_frame_try[cam] = now
            # Frames are keyed by the camera name the *status* reports (the
            # daemon's slug), because the renderer looks them up from the
            # status it draws. The operator may have typed a persistent
            # number ("4"); keying by that token would never match the slug
            # and the image would never replace the fallback tiles.
            with self._lock:
                st = self._statuses.get(cam)
            key = st.camera if st is not None and st.camera else cam
            try:
                frame = self._daemon.get_frame(cam)
                data = frame.data
                if not data:
                    raise ValueError("empty frame")
            except Exception as exc:  # noqa: BLE001 - dim the old image, show banner
                with self._lock:
                    self._frame_errors[cam] = f"[{cam}] frame: {str(exc) or type(exc).__name__}"
                    if key in self._frames:
                        self._frame_stale[key] = True
                continue
            with self._lock:
                self._frames[key] = data
                self._frame_stale[key] = False
                self._frame_errors.pop(cam, None)

    def _run_command(self, cmd: str) -> None:
        if cmd == "enter":
            with self._lock:
                good = {c: bool(self._statuses[c].good) if c in self._statuses else False
                        for c in self.cameras if c in self.owned}
            decision = decide_enter(good)
            if decision.action == "quit":
                self.finished = True
            elif decision.action == "notice":
                with self._lock:
                    self._notice = [decision.notice]
            else:
                for cam in decision.cameras:
                    self._finish(cam)
        else:  # escape / close
            self.abort_all()
            self.finished = True

    def _finish(self, cam: str) -> None:
        try:
            result = self._daemon.finish_intrinsics(cam, True)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._notice = self._notice + [f"[{cam}] finish failed: {exc}"]
            return
        with self._lock:
            self._notice = self._notice + finish_text_lines(result)
            self._statuses[cam] = result.status
        if result.installed:
            self.owned.discard(cam)
            self.installed.add(cam)

    def abort_all(self) -> None:
        """Abort every session still owned (best effort)."""
        for cam in list(self.owned):
            try:
                self._daemon.abort_intrinsics(cam)
            except Exception:  # noqa: BLE001 - keep releasing the others
                pass
            self.owned.discard(cam)

    def run_poller(self, interval: float = _POLL_INTERVAL_S) -> None:
        while not self._stop.is_set() and not self.finished:
            self.step()
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()


class _TkShell:
    def __init__(self, controller: IntrinsicsController) -> None:
        import tkinter as tk

        self._tk = tk
        self._ctl = controller
        _ensure_tk_library_env()
        try:
            self._root = tk.Tk()
        except tk.TclError as exc:
            raise NoDisplayAvailable(str(exc)) from exc
        self._root.title("aprilcam intrinsics")
        self._root.configure(bg=_BG)
        try:
            self._root.attributes("-fullscreen", True)
        except tk.TclError:
            pass
        self._canvas = tk.Canvas(self._root, bg=_BG, highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._photos: dict = {}  # camera -> (cache key, jpeg, PhotoImage); keeps refs alive
        self._last_good: dict = {}  # camera -> last composed (undimmed) PIL image
        self._root.bind("<Key>", self._on_key)
        self._root.protocol("WM_DELETE_WINDOW", lambda: controller.request("close"))

    def _on_key(self, event) -> None:
        action = key_action(event.keysym)
        if action is not None:
            self._ctl.request(action)

    def run(self) -> None:
        poller = threading.Thread(target=self._ctl.run_poller, name="aprilcam-intrinsics-poller",
                                  daemon=True)
        poller.start()
        try:
            self._tick()
            self._root.mainloop()
        finally:
            self._ctl.stop()
            self._ctl.abort_all()
            poller.join(timeout=2.0)
            try:
                self._root.destroy()
            except self._tk.TclError:
                pass

    def _tick(self) -> None:
        snap = self._ctl.snapshot()
        if snap.finished:
            self._root.quit()
            return
        self._draw(snap)
        self._root.after(_DRAW_INTERVAL_MS, self._tick)

    # -- drawing: layout comes from the model, this only paints -----------
    def _draw(self, snap: Snapshot) -> None:
        c = self._canvas
        c.delete("all")
        w, h = self._root.winfo_width(), self._root.winfo_height()
        if w < 50 or h < 50:
            w, h = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        n = max(len(self._ctl.cameras), 1)
        fs = font_sizes(self._root.winfo_screenheight(), n)
        vm = build_view_model(snap.statuses)
        err = connection_banner(snap.error)
        if err:
            c.create_text(w // 2, fs.banner // 2 + 4, text=err, fill=colour_for("red"),
                          font=("Helvetica", -fs.banner // 2, "bold"))
            y = fs.banner
        else:
            c.create_text(w // 2, fs.banner // 2 + 4, text=vm.banner_text,
                          fill=colour_for(vm.banner_class),
                          font=("Helvetica", -fs.banner, "bold"))
            y = fs.banner + 20
        col_w = w // n
        bottom = h - 10
        lines = list(snap.notice_lines)
        bottom -= len(lines) * (fs.body + 8)
        limit = bottom - fs.next_line * 3 if vm.next_line else bottom - 10
        for i, cam in enumerate(vm.cameras):
            self._draw_camera(cam, i * col_w, y, col_w, fs, limit, snap)
        for k, line in enumerate(lines):
            c.create_text(10, bottom + k * (fs.body + 8), text=line, anchor="nw",
                          fill=colour_for("plain"), font=("Courier", -fs.body))
        if vm.next_line:
            c.create_text(w // 2, bottom - fs.next_line * 2, text="NEXT: " + vm.next_line,
                          fill=colour_for("yellow" if not vm.all_good else "green"),
                          font=("Helvetica", -fs.next_line, "bold"), justify="center",
                          width=w - 40)

    def _compose(self, cam, jpeg, status, model, dest, dimmed, fs):
        """Composed PhotoImage for one camera, cached so the 150 ms redraw
        tick does not re-decode an unchanged frame."""
        from PIL import ImageTk

        key = (id(jpeg), dest, dimmed, tuple(t.level for t in model.cells),
               tuple(t.count for t in model.cells), status.board_outline)
        hit = self._photos.get(cam)
        if hit is not None and hit[0] == key:
            return hit[2]
        img = compose_camera_image(
            jpeg, model, status.board_outline, status.image_size, dest, dimmed=dimmed,
            fallback=self._last_good.get(cam), label_px=fs.metric)
        if not dimmed:
            self._last_good[cam] = img
        photo = ImageTk.PhotoImage(img)
        self._photos[cam] = (key, jpeg, photo)
        return photo

    def _draw_camera(self, cam, x0: int, y: int, col_w: int, fs, limit: int, snap: Snapshot) -> None:
        c = self._canvas
        pad = 12
        cx = x0 + col_w // 2
        c.create_text(cx, y, text=f"{cam.camera}   {cam.views_line}", anchor="n",
                      fill=colour_for("dim"), font=("Helvetica", -fs.body))
        y += fs.body + 14
        c.create_text(cx, y, text=cam.error_now, anchor="n",
                      fill=colour_for(cam.error_class), font=("Helvetica", -fs.metric, "bold"),
                      width=col_w - 2 * pad)
        y += int(fs.metric * 1.6)
        c.create_text(cx, y, text=cam.could_change, anchor="n",
                      fill=colour_for(cam.could_change_class),
                      font=("Helvetica", -fs.metric * 2 // 3, "bold"), width=col_w - 2 * pad)
        y += int(fs.metric * 2.2)
        notes = ([cam.reject_note] if cam.reject_note else []) + list(cam.errors)
        tside = max(min((col_w - 2 * pad) // 3, fs.metric * 3) * 2 // 3, 18)
        jpeg = snap.frames.get(cam.camera)
        status = next((s for s in snap.statuses if s.camera == cam.camera), None)
        if jpeg is not None and status is not None:
            tilt_h = tside + 10
            notes_h = len(notes) * (fs.body + 6)
            area_h = limit - y - tilt_h - notes_h
            if area_h >= 60:
                dest = (col_w - 2 * pad, area_h)
                photo = self._compose(cam.camera, jpeg, status, cam, dest,
                                      bool(snap.frame_stale.get(cam.camera)), fs)
                fx0, fy0, fx1, fy1 = fit_rect((photo.width(), photo.height()),
                                              (x0 + pad, y, x0 + col_w - pad, y + area_h))
                c.create_image(fx0, fy0, image=photo, anchor="nw")
                y = fy1 + 8
                self._draw_tilts(cam, cx, y, tside, fs)
                y += tilt_h
                self._draw_notes(notes, cx, y, col_w - 2 * pad, fs)
                return
        side = max(min((col_w - 2 * pad) // 3, fs.metric * 3), 20)
        left = cx - side * 3 // 2
        for idx, tile in enumerate(cam.cells):
            r, k = divmod(idx, 3)
            self._tile(left + k * side, y + r * side, side, tile, fs)
        y += side * 3 + 14
        tside = max(side * 2 // 3, 18)
        self._draw_tilts(cam, cx, y, tside, fs)
        y += tside + 10
        self._draw_notes(notes, cx, y, col_w - 2 * pad, fs)

    def _draw_tilts(self, cam, cx: int, y: int, tside: int, fs) -> None:
        tleft = cx - tside * len(cam.tilts) // 2
        for k, tile in enumerate(cam.tilts):
            self._tile(tleft + k * tside, y, tside, tile, fs)

    def _draw_notes(self, notes, cx: int, y: int, width: int, fs) -> None:
        for note in notes:
            self._canvas.create_text(cx, y, text=note, anchor="n", fill=colour_for("red"),
                                     font=("Helvetica", -fs.body), width=width)
            y += fs.body + 6

    def _tile(self, x: int, y: int, side: int, tile, fs) -> None:
        c = self._canvas
        c.create_rectangle(x + 2, y + 2, x + side - 2, y + side - 2,
                           fill=tile_colour(tile.level), outline="#000000")
        c.create_text(x + side // 2, y + side // 2,
                      text=f"{tile.name}\n{tile.count if tile.count else '.'}",
                      fill="#ffffff", font=("Helvetica", -fs.tile, "bold"), justify="center")


def run_intrinsics_gui(
    daemon,
    cameras: list,
    *,
    install: bool,
    shell_factory: Optional[Callable[[IntrinsicsController], object]] = None,
) -> int:
    """Run the fullscreen window; returns 0 only if every camera ended
    installed, else 1. Raises :class:`NoDisplayAvailable` on a display-less
    host so the CLI can fall back to text mode. Always aborts sessions that
    are still owned on the way out."""
    reason = _headless_reason()
    if reason is not None and shell_factory is None:
        raise NoDisplayAvailable(reason)
    controller = IntrinsicsController(daemon, cameras, install=install)
    shell: Any
    try:
        if shell_factory is None:
            try:
                shell = _TkShell(controller)
            except ImportError as exc:  # no tkinter in this Python build
                raise NoDisplayAvailable(str(exc)) from exc
        else:
            shell = shell_factory(controller)
        shell.run()
    finally:
        controller.stop()
        controller.abort_all()
    return controller.exit_code()
