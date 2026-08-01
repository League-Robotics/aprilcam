"""``view/viewer.py`` — the poll-driven live-view window core (ticket
022-002; sprint.md Architecture Decisions 2 and 3).

**Polls; does not stream (Decision 3).** Both view commands need frames
continuously, but ``ImageStream``/``TagStream`` are undeclared-but-
unimplemented Stage 8/12 territory this sprint does not otherwise
touch -- streaming needs backpressure (bounded queues, silent drop), a
stream-endpoint allocation RPC, and per-subscriber lifecycle, a real
subsystem in its own right. Building a partial version of it just to
feed one viewer window would be built twice, so :class:`PollLoop` below
instead calls a plain ``fetch`` callable at a modest fixed rate
(:data:`DEFAULT_POLL_HZ`, ~2 Hz -- a starting guess, not a tuned
constant per sprint.md's Open Question 3; revisit once the streaming
subsystem exists and the two can be compared).

**No OpenCV here, ever.** The daemon serves already-encoded JPEG/PNG
bytes; this module decodes them with Pillow (``PIL.Image``) for display
and never imports ``cv2`` -- ``docs/design/adapters.md``'s "adapters do
no vision" rule, extended to the one adapter that touches pixels at
all (to put them on screen, not to process them). See
``tests/test_no_cv2_in_client_modules.py`` for the automated check.

**The ticket-006 reuse seam.** :class:`Window` (a display surface:
encoded bytes in, pixels on screen) and :class:`PollLoop` (fetch, show,
wait, repeat, until the window closes) know nothing about *what* the
raw view vs. the deskewed+tags view fetch or how it was produced --
only :func:`run_raw_view` and its private ``_fetch_raw`` below are
specific to this ticket. Ticket 006's ``playfield view`` reuses
``Window``/``PollLoop`` unchanged and supplies a different ``fetch``
callable (``get_tags`` then ``get_frame(deskewed=True)``, rendered
through ``client/renderer.py`` into PNG bytes) -- the loop, timing, and
window lifecycle stay identical, exactly as sprint.md's Step 3 table
describes the "Live viewing" responsibility.

**"Raw means raw" is enforced by omission, not by a switch.**
``_fetch_raw`` never reads ``ImageFrame.tags`` at all, even though a
frame may carry one (an active ring buffer from a recent ``camera
tags``/``camera calibrate`` call on the same camera) -- UC-16's
"raw means raw" contract, taken literally as a *display-time* rule per
the ticket brief: the daemon's query is unconditional (``get_frame``
always embeds the latest ring-buffer entry when one exists), but
nothing on this path ever turns that embedded data into a drawn
boundary or hat. This is what makes it safe to reuse ``_fetch_raw`` as
written even while detection happens to be running for an unrelated
reason (the ticket's first acceptance criterion).

**Headless detection (UC-16.S-3).** :class:`TkWindow` raises
:class:`NoDisplayAvailable` instead of letting a GUI-toolkit failure
reach the caller as an unexplained backtrace: a cheap, toolkit-free
env-var check (:func:`_headless_reason`) catches the common
field-Pi-over-SSH case (no ``$DISPLAY``/``$WAYLAND_DISPLAY``) before
tkinter is even asked to try, and a ``tkinter.TclError`` catch around
``tk.Tk()`` itself is the second line of defence for every other
headless shape, including a headless macOS session (which does not use
``$DISPLAY`` at all). ``NoDisplayAvailable`` is deliberately **not** an
``aprilcam.errors.AprilCamError`` -- see that class's own docstring for
why: this condition is local to the client process's environment, not
a distinction the daemon-facing error taxonomy exists to carry.
``cli/camera.py`` is the boundary that catches it and prints the
UC-16.S-3 message pointing at ``camera image``.
"""

from __future__ import annotations

import glob
import io
import os
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image, ImageTk

from aprilcam.daemon.interfaces import Daemon

__all__ = [
    "DEFAULT_POLL_HZ",
    "NoDisplayAvailable",
    "PolledFrame",
    "Window",
    "TkWindow",
    "PollLoop",
    "run_raw_view",
]

#: Decision 3 (sprint.md): a starting guess, not a tuned constant --
#: see the module docstring.
DEFAULT_POLL_HZ = 2.0

#: How often ``PollLoop`` pumps the window's toolkit event loop while
#: waiting for the next poll tick, so a close click is noticed within
#: this long even at a slow poll rate, rather than only at the next
#: fetch.
_PUMP_INTERVAL_S = 0.05

#: Large camera frames (a 4K webcam) are scaled down for the window --
#: purely a screen-real-estate convenience for :class:`TkWindow`, never
#: sent back to the daemon and never applied to anything but the
#: on-screen copy. Not "processing" in the raw-means-raw sense: no
#: crop, no colour change, no overlay, and the underlying bytes handed
#: to ``show()`` are untouched.
_MAX_DISPLAY_WIDTH = 960


class NoDisplayAvailable(Exception):
    """No GUI display is available to open a viewer window (UC-16.S-3).

    See the module docstring for why this is not an
    ``aprilcam.errors.AprilCamError``.
    """


@dataclass(frozen=True, kw_only=True)
class PolledFrame:
    """One poll tick's display payload -- an already-encoded image
    (JPEG or PNG) plus an optional advisory line for the window
    chrome, never drawn onto the image itself (see
    ``Window.set_status``)."""

    data: bytes
    status: Optional[str] = None


class Window:
    """A live-view display surface: encoded bytes in, pixels on screen.

    A plain base class, not ``typing.Protocol`` (see
    ``daemon/interfaces.py``'s module docstring for the codebase-wide
    design rule): :class:`TkWindow` derives from this for real use;
    tests derive a fake so :class:`PollLoop` is exercised with no real
    GUI toolkit instantiated.
    """

    def show(self, data: bytes) -> None:
        """Decode *data* (an encoded image -- JPEG or PNG) and display
        it, replacing whatever was shown before. Never draws anything
        beyond the pixels handed in -- any overlay is baked into *data*
        upstream (``client/renderer.py``, ticket 006) or, on this
        ticket's raw path, simply absent."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.show")

    def set_status(self, text: Optional[str]) -> None:
        """An advisory line shown in the window chrome, never drawn
        onto the image itself -- e.g. the ``frame_dark`` advisory
        (this ticket's last acceptance criterion). ``None`` clears
        it."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.set_status")

    def pump(self) -> bool:
        """Process one tick of the toolkit's event loop. Returns
        ``False`` once the user has closed the window -- the signal
        :class:`PollLoop` uses to stop polling -- ``True``
        otherwise."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.pump")

    def close(self) -> None:
        """Release the window and any toolkit resources. Idempotent."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.close")


def _headless_reason() -> Optional[str]:
    """A cheap, toolkit-free headless check for the common Linux/X11
    (or Wayland) case -- the field-Pi-over-SSH scenario UC-16.S-3 names
    explicitly. Returns why no display is likely available, or
    ``None`` if this quick check finds nothing wrong -- which is not a
    guarantee a display actually works, only that this fast check did
    not rule one out; the real check on every platform is the
    toolkit's own open attempt in :class:`TkWindow`. macOS and Windows
    do not use ``$DISPLAY`` for their native GUI backends, so this
    always returns ``None`` there -- a headless macOS session is
    instead caught by ``tk.Tk()`` raising ``TclError``."""
    if not sys.platform.startswith("linux"):
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    return "no $DISPLAY or $WAYLAND_DISPLAY set"


def _ensure_tk_library_env() -> None:
    """Some standalone Python builds (a uv-managed CPython in
    particular) ship Tcl/Tk but record a build-time ``TCL_LIBRARY``
    path that no longer exists on this machine, so ``tk.Tk()`` fails
    with "Can't find a usable init.tcl" even though a display genuinely
    is available -- ported from ``archive/src/aprilcam/cli/
    view_cli.py``'s ``_ensure_tcl_tk``. A no-op once ``TCL_LIBRARY`` is
    already set, or when nothing matching is found (in which case
    ``tk.Tk()`` fails on its own terms and :class:`NoDisplayAvailable`
    still carries a real message)."""
    if os.environ.get("TCL_LIBRARY"):
        return
    for base in (sys.base_prefix, sys.prefix):
        for tcl_dir in sorted(glob.glob(os.path.join(base, "lib", "tcl8.*")), reverse=True):
            if os.path.exists(os.path.join(tcl_dir, "init.tcl")):
                os.environ["TCL_LIBRARY"] = tcl_dir
                tk_dir = os.path.join(
                    os.path.dirname(tcl_dir),
                    os.path.basename(tcl_dir).replace("tcl", "tk"),
                )
                if os.path.isdir(tk_dir):
                    os.environ["TK_LIBRARY"] = tk_dir
                return


class TkWindow(Window):
    """The real, tkinter-backed :class:`Window`. See the module
    docstring for the headless-detection strategy."""

    def __init__(self, title: str) -> None:
        reason = _headless_reason()
        if reason is not None:
            raise NoDisplayAvailable(reason)
        _ensure_tk_library_env()
        try:
            self._root = tk.Tk()
        except tk.TclError as exc:
            raise NoDisplayAvailable(str(exc)) from exc

        self._root.title(title)
        self._closed = False
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._image_label = tk.Label(self._root, bg="black")
        self._image_label.pack()
        self._status_var = tk.StringVar(value="")
        tk.Label(
            self._root, textvariable=self._status_var, fg="#cc8800", anchor="w"
        ).pack(fill=tk.X)

        # tkinter drops a PhotoImage the instant nothing keeps a Python
        # reference to it, even while the Label is still displaying it
        # -- this attribute is that reference, replaced (not appended
        # to) on every show() call.
        self._photo: Optional[ImageTk.PhotoImage] = None

    def _on_close(self) -> None:
        self._closed = True

    def show(self, data: bytes) -> None:
        image: Image.Image = Image.open(io.BytesIO(data))
        image.load()
        if image.width > _MAX_DISPLAY_WIDTH:
            scale = _MAX_DISPLAY_WIDTH / image.width
            image = image.resize(
                (int(image.width * scale), max(int(image.height * scale), 1)),
                Image.Resampling.LANCZOS,
            )
        photo = ImageTk.PhotoImage(image)
        self._image_label.configure(image=photo)
        self._photo = photo

    def set_status(self, text: Optional[str]) -> None:
        self._status_var.set(text or "")

    def pump(self) -> bool:
        if self._closed:
            return False
        try:
            self._root.update()
        except tk.TclError:
            # The window was torn down out from under us (e.g. the
            # process is shutting down) -- treat it exactly like a
            # normal close rather than propagating a toolkit error
            # this late.
            self._closed = True
            return False
        return not self._closed

    def close(self) -> None:
        self._closed = True
        try:
            self._root.destroy()
        except tk.TclError:
            pass


class PollLoop:
    """Poll-driven display loop: fetch a frame, hand it to a
    :class:`Window`, pump the toolkit's event loop, wait for the next
    tick, repeat -- until the window closes.

    This is the plumbing ticket 006's ``playfield view`` reuses
    unchanged (see the module docstring): only *what* ``fetch``
    returns differs between the raw and deskewed+tags views.

    Parameters
    ----------
    window:
        Where frames are displayed.
    fetch:
        Produces the next :class:`PolledFrame`. Called once per tick,
        synchronously -- any exception it raises (e.g. an
        ``aprilcam.errors.AprilCamError`` from a dropped connection)
        propagates out of :meth:`run`, ending the loop.
    poll_hz:
        Ticks per second (default :data:`DEFAULT_POLL_HZ`).
    sleep / clock:
        Overridable time sources so tests exercise real cadence logic
        without a real clock (``time.sleep``/``time.monotonic`` by
        default).
    """

    def __init__(
        self,
        window: Window,
        fetch: Callable[[], PolledFrame],
        *,
        poll_hz: float = DEFAULT_POLL_HZ,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._fetch = fetch
        self._interval = 1.0 / poll_hz
        self._sleep = sleep
        self._clock = clock

    def run(self) -> None:
        """Poll until the window closes. Returns normally once
        :meth:`Window.pump` reports the window closed -- never raises
        for that case, since a closed window is the expected way this
        loop ends (the ticket's second acceptance criterion: closing
        the window stops polling)."""
        while self._window.pump():
            frame = self._fetch()
            self._window.show(frame.data)
            self._window.set_status(frame.status)
            if not self._wait_for_next_tick():
                break

    def _wait_for_next_tick(self) -> bool:
        """Sleep until *interval* has elapsed since this tick started,
        pumping the window periodically so a close during the wait is
        noticed promptly. Returns ``False`` the moment the window
        reports closed."""
        deadline = self._clock() + self._interval
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            if not self._window.pump():
                return False
            self._sleep(min(remaining, _PUMP_INTERVAL_S))


def _fetch_raw(daemon: Daemon, camera: str) -> PolledFrame:
    """One poll tick's raw-view payload (UC-16, SUC-002). See the
    module docstring's "raw means raw is enforced by omission" note --
    this never reads ``frame.tags``."""
    frame = daemon.get_frame(camera, deskewed=False)
    status = "frame dark -- image may be hard to see" if frame.frame_dark else None
    return PolledFrame(data=frame.data, status=status)


def run_raw_view(
    camera: str,
    daemon: Daemon,
    *,
    window_factory: Callable[[str], Window] = TkWindow,
    poll_hz: float = DEFAULT_POLL_HZ,
) -> None:
    """``camera view <camera>`` (UC-16, SUC-002): open a window and
    poll ``get_frame(camera, deskewed=False)`` at *poll_hz*, displaying
    each frame's raw bytes exactly as received -- no drawn overlays of
    any kind.

    Raises :class:`NoDisplayAvailable` if no display can be opened
    (propagated from *window_factory*, i.e. before any daemon call is
    made) -- ``cli/camera.py`` is the boundary that turns this into the
    UC-16.S-3 message pointing at ``camera image``. Any
    ``aprilcam.errors.AprilCamError`` raised mid-view (e.g. the camera
    disconnects) propagates the same way, after the window is closed.
    """
    window = window_factory(f"aprilcam camera view -- {camera}")
    try:
        PollLoop(window, lambda: _fetch_raw(daemon, camera), poll_hz=poll_hz).run()
    finally:
        window.close()
