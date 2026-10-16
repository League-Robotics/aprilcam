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
from typing import Callable, Optional

from aprilcam.intrinsics.interfaces import IntrinsicsStatus
from aprilcam.view.intrinsics_model import (
    build_view_model,
    colour_for,
    connection_banner,
    decide_enter,
    exit_code_for,
    finish_text_lines,
    font_sizes,
    key_action,
    tile_colour,
)
from aprilcam.view.viewer import NoDisplayAvailable, _ensure_tk_library_env, _headless_reason

__all__ = ["IntrinsicsController", "Snapshot", "run_intrinsics_gui"]

_POLL_INTERVAL_S = 0.33
_DRAW_INTERVAL_MS = 150
_BG = "#101418"


class Snapshot:
    """Immutable-by-convention view of the controller state for one redraw."""

    def __init__(self, statuses, notice_lines, error, finished):
        self.statuses = statuses
        self.notice_lines = notice_lines
        self.error = error
        self.finished = finished


class IntrinsicsController:
    """Session bookkeeping and RPCs, independent of Tk (testable directly)."""

    def __init__(self, daemon, cameras: list, *, install: bool) -> None:
        self._daemon = daemon
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
            return Snapshot(ordered, list(self._notice), self._error, self.finished)

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
        if self._install:
            for cam in list(self.owned):
                st = self._statuses.get(cam)
                if st is not None and st.good and cam not in self._auto_tried:
                    self._auto_tried.add(cam)
                    self._finish(cam)

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
        for i, cam in enumerate(vm.cameras):
            self._draw_camera(cam, i * col_w, y, col_w, fs)
        bottom = h - 10
        lines = list(snap.notice_lines)
        if lines:
            bottom -= len(lines) * (fs.body + 8)
            for k, line in enumerate(lines):
                c.create_text(10, bottom + k * (fs.body + 8), text=line, anchor="nw",
                              fill=colour_for("plain"), font=("Courier", -fs.body))
        if vm.next_line:
            c.create_text(w // 2, bottom - fs.next_line * 2, text="NEXT: " + vm.next_line,
                          fill=colour_for("yellow" if not vm.all_good else "green"),
                          font=("Helvetica", -fs.next_line, "bold"), justify="center",
                          width=w - 40)

    def _draw_camera(self, cam, x0: int, y: int, col_w: int, fs) -> None:
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
        side = max(min((col_w - 2 * pad) // 3, fs.metric * 3), 20)
        left = cx - side * 3 // 2
        for idx, tile in enumerate(cam.cells):
            r, k = divmod(idx, 3)
            self._tile(left + k * side, y + r * side, side, tile, fs)
        y += side * 3 + 14
        tside = max(side * 2 // 3, 18)
        tleft = cx - tside * len(cam.tilts) // 2
        for k, tile in enumerate(cam.tilts):
            self._tile(tleft + k * tside, y, tside, tile, fs)
        y += tside + 10
        for note in ([cam.reject_note] if cam.reject_note else []) + list(cam.errors):
            c.create_text(cx, y, text=note, anchor="n", fill=colour_for("red"),
                          font=("Helvetica", -fs.body), width=col_w - 2 * pad)
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
