"""``view/viewer.py`` -- the streaming live-view window core (ticket
027-007; supersedes the poll-driven design ticket 022-002 built, which
sprint 022's own module docstring explicitly deferred replacing until
"the streaming subsystem exists and the two can be compared" (Decision
3). That subsystem now exists (tickets 027-001 through 027-006) and
this module is the comparison's result.

**Streams; does not poll.** Both view commands subscribe once to
``daemon.stream_images(camera, deskewed=...)`` (``daemon/broadcast.py``'s
fan-out, ``daemon/streaming.py``'s producers, ``daemon/
stream_transport.py``'s socket transport, ``client/streams.py``'s
``SocketImageStream`` -- tickets 027-001 through 027-006) instead of
calling ``get_frame``/``get_tags`` on a fixed-rate timer.
:func:`run_raw_view` subscribes with ``deskewed=False``;
:func:`run_playfield_view` subscribes with ``deskewed=True`` **only** --
a deskewed ``ImageFrame`` already embeds its own ``TagFrame``
(``frame.tags``) and ``AnnotationSet`` (``frame.annotations``), so the
playfield view needs no second subscription and no ``get_tags``/
``stream_tags`` call at all (``types.ImageFrame``'s own "frames carry
tags" / "deskewed frames carry annotations" contract, unchanged by this
ticket). This is a real simplification over the old two-call
``_fetch_playfield`` (``get_tags`` then ``get_frame``), not merely a
speed-up: one subscription is everything the renderer needs, on every
frame it is handed.

**A background reader thread feeds a single-slot "latest frame" cell.**
Tk's main loop cannot block on a stream read without freezing the
window, so :class:`StreamLoop` runs a daemon-mode background thread
that iterates the ``ImageStream``, converts each yielded ``ImageFrame``
into a :class:`DisplayFrame`, and stores it in a lock-protected single
attribute -- replacing, never queuing, whatever was there before. The
calling thread (Tk's own) samples that cell non-blockingly on
:data:`_PUMP_INTERVAL_S`'s cadence -- the same interval that, under the
old :class:`!PollLoop`, only governed how promptly a mid-poll window
close was noticed. It now does double duty as both the responsiveness
cadence and the display refresh cadence, which is why this module has
only *one* timing constant instead of two (``poll_hz`` plus this
interval) -- ticket 027-007's own instruction: reuse
``Window.pump()``'s existing cadence rather than inventing a second
one. Older undisplayed frames are silently dropped rather than queued
for later display, carrying the streaming subsystem's own backpressure
model (``daemon/broadcast.py``: a slow consumer loses frames, it never
stalls the pipeline or another subscriber) one layer further, to the
one display slot a live-view window has room for anyway.

Closing the window (``Window.pump()`` reporting ``False``) and the
reader thread ending with an exception both stop :meth:`StreamLoop.run`
promptly: the stream is closed (unsubscribing -- a daemon-visible event
per ``daemon/interfaces.py``'s ``ImageStream`` docstring) and the reader
thread is joined, bounded by :data:`_READER_JOIN_TIMEOUT_S`, before
``run()`` returns or re-raises -- so no reader thread survives past a
``run_raw_view``/``run_playfield_view`` call returning, in ordinary
operation.

**No OpenCV here, ever.** The daemon serves already-encoded JPEG/PNG
bytes; this module decodes them with Pillow (``PIL.Image``) for display
and never imports ``cv2`` -- ``docs/design/adapters.md``'s "adapters do
no vision" rule, extended to the one adapter that touches pixels at
all (to put them on screen, not to process them). See
``tests/test_no_cv2_in_client_modules.py`` for the automated check.

**The reuse seam.** :class:`Window` (a display surface: encoded bytes
in, pixels on screen) and :class:`StreamLoop` (subscribe, read, show,
repeat, until the window closes or the stream fails) know nothing about
*what* the raw view vs. the deskewed+tags view subscribes to or how a
frame becomes displayed bytes -- only :func:`run_raw_view`/
:func:`run_playfield_view` and their private ``_convert_raw_frame``/
``_make_playfield_converter`` below are specific to a view kind. This
is the same reuse seam ticket 022-006 established for ``PollLoop``,
carried forward onto ``StreamLoop``: the loop, timing, and window
lifecycle stay identical between the two views; only the subscription's
``deskewed`` flag and the conversion callable differ.

**A deliberately narrow exception (sprint 032 ticket 002).**
``TkWindow``'s ``show_sidebar`` constructor flag and :meth:`Window.update_tags`
are view-kind-specific in a way nothing else in this seam is: the tag
sidebar is a playfield-view-only presentation feature. The raw view's
window is never built with ``show_sidebar=True`` and its
``_convert_raw_frame`` never populates :attr:`DisplayFrame.tags`, so
:meth:`StreamLoop.run` can still call ``update_tags`` unconditionally,
on every frame, for both view kinds -- :meth:`Window.update_tags`'s
concrete no-op default (not ``NotImplementedError``, unlike every other
``Window`` method -- see that method's own docstring) is what keeps
:class:`StreamLoop` itself agnostic even though the *feature* is not.

**``playfield view``'s "raw means raw" counterpart: never fall back.**
:func:`run_playfield_view` calls ``daemon.stream_images`` with
``deskewed=True`` and has no ``except`` around that call or around
anything the returned stream raises -- so
:class:`~aprilcam.errors.NotCalibrated` (raised for a deskewed request
against an uncalibrated camera, per ``Daemon.get_frame``'s and
``Daemon.stream_images``'s shared "fails explicitly, never silently
substitutes a raw frame" contract) propagates out of
:func:`run_playfield_view` untouched, and a raw-frame substitute is
structurally impossible on this path, not merely avoided by discipline
(UC-18.S-2's central rule, mirrored from UC-16's "raw means raw" the
other direction). Unchanged in substance from ticket 022-006 -- only
the call this propagates from moved, from ``get_frame`` to
``stream_images``.

**"Raw means raw" is enforced by omission, not by a switch.**
:func:`_convert_raw_frame` never reads ``ImageFrame.tags`` at all, even
though a raw frame may carry one (an active ring buffer from a recent
``camera tags``/``camera calibrate`` call on the same camera, or
detection simply running for an unrelated reason) -- UC-16's "raw means
raw" contract, taken literally as a *display-time* rule: nothing on
this path ever turns embedded tag data into a drawn boundary or hat.
This is what makes it safe to reuse ``_convert_raw_frame`` as written
even while detection happens to be active for an unrelated reason.

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
import math
import os
import io
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from PIL import Image, ImageTk

from aprilcam.client.interfaces import Renderer as RendererBase
from aprilcam.client.renderer import Renderer as PillowRenderer
from aprilcam.daemon.interfaces import Daemon, ImageStream
from aprilcam.types import ImageFrame, TagFamily, TagId, TagRecord

__all__ = [
    "DEFAULT_POLL_HZ",
    "NoDisplayAvailable",
    "FirstFrameTimeout",
    "DisplayFrame",
    "Window",
    "TkWindow",
    "StreamLoop",
    "run_raw_view",
    "run_playfield_view",
]

#: The poll rate this module used before ticket 027-007's migration to
#: streaming (sprint 022's Decision 3). Nothing in this module reads it
#: to drive a timer any more -- frames now arrive at whatever rate the
#: daemon's stream publishes, sampled for display at
#: :data:`_PUMP_INTERVAL_S`'s cadence instead. Kept only as the "old
#: way" comparison baseline this ticket's own acceptance criteria and
#: tests measure the new streaming throughput against (the "visibly
#: beats 2 Hz" success criterion).
DEFAULT_POLL_HZ = 2.0

#: How often :class:`StreamLoop`'s main-thread loop samples the
#: single-slot "latest frame" cell and pumps the window's toolkit event
#: loop -- both the display refresh cadence and the window-close
#: responsiveness cadence are now this one number (module docstring:
#: "do not invent a second timing constant"), where the old
#: :class:`!PollLoop` needed two (a poll interval, plus this same
#: sub-interval purely for noticing a close mid-wait).
_PUMP_INTERVAL_S = 0.05

#: How long :meth:`StreamLoop.run` waits for the background reader
#: thread to join once the window has closed or the stream has failed
#: -- mirrors ``daemon/stream_transport.py``'s own ``_JOIN_TIMEOUT_S``
#: bounded-join discipline, for the identical reason: never block
#: shutdown forever on a thread that might be stuck in a blocking
#: socket read.
_READER_JOIN_TIMEOUT_S = 2.0

#: Default bound (sprint 038) on how long :meth:`StreamLoop.run` waits
#: for the *first* frame before giving up and raising
#: :class:`FirstFrameTimeout` -- the production default; tests inject a
#: much shorter value via the constructor's ``first_frame_timeout_s``
#: parameter. Named and documented per the ticket's own requirement
#: ("a real default for production use, injectable short for tests"),
#: not a magic number inlined at the call site.
_DEFAULT_FIRST_FRAME_TIMEOUT_S = 10.0

#: The background reader thread's name -- distinctive enough that a
#: test can confirm via ``threading.enumerate()`` that no reader thread
#: survives past a :meth:`StreamLoop.run` call returning.
_READER_THREAD_NAME = "aprilcam-view-stream-reader"

#: Cap on the *initial* image-area width (a 4K webcam frame must not
#: open a huge window). After the first frame the window is
#: user-resizable and the image is scaled to fit it -- up or down --
#: purely as an on-screen convenience for :class:`TkWindow`, never sent
#: back to the daemon and never applied to anything but the on-screen
#: copy. Not "processing" in the raw-means-raw sense: no crop, no colour
#: change, no overlay, and the bytes handed to ``show()`` are untouched.
_MAX_DISPLAY_WIDTH = 960


def _fit_size(src_w: int, src_h: int, area_w: int, area_h: int) -> Tuple[int, int]:
    """Largest ``(w, h)`` with the aspect ratio of ``src_w x src_h`` that
    fits inside ``area_w x area_h`` (letterbox: never stretch or crop;
    scales up as well as down). Pure -- no Tk. Degenerate inputs (any
    dimension <= 0) return ``(1, 1)`` rather than raising."""
    if src_w <= 0 or src_h <= 0 or area_w <= 0 or area_h <= 0:
        return (1, 1)
    scale = min(area_w / src_w, area_h / src_h)
    return (max(int(src_w * scale), 1), max(int(src_h * scale), 1))


class NoDisplayAvailable(Exception):
    """No GUI display is available to open a viewer window (UC-16.S-3).

    See the module docstring for why this is not an
    ``aprilcam.errors.AprilCamError``.
    """


class FirstFrameTimeout(Exception):
    """No frame arrived within :meth:`StreamLoop.run`'s bounded
    first-frame timeout (sprint 038, SUC-004).

    Deliberately **not** an ``aprilcam.errors.AprilCamError`` -- like
    :class:`NoDisplayAvailable`, this condition is local to the client
    process (an elapsed-time observation about its own stream
    subscription), not a distinction the daemon-facing error taxonomy
    exists to carry. Carries *camera* so a caller's diagnostic (see
    ``cli/camera.py``'s ``cmd_view``) can name it without re-threading
    the value through separately.
    """

    def __init__(self, camera: str) -> None:
        self.camera = camera
        super().__init__(
            f"no frame received from {camera!r} within the first-frame timeout"
        )


@dataclass(frozen=True, kw_only=True)
class DisplayFrame:
    """One display update's payload -- an already-encoded image (JPEG
    or PNG) plus an optional advisory line for the window chrome, never
    drawn onto the image itself (see ``Window.set_status``).

    ``tags`` (sprint 032 ticket 002) is the current frame's tag list
    for :meth:`Window.update_tags` to refresh a side panel from --
    additive and default-``None``, so every construction site that
    predates this field (``_convert_raw_frame``,
    ``_convert_bare_playfield_frame``) keeps compiling and behaving
    identically without being touched. Only
    ``_make_playfield_converter``'s non-bare ``convert`` populates it,
    from the same ``frame.tags`` it already reads to render the overlay
    -- no second subscription, no new daemon call. As of sprint 033
    ticket 001, that population is AprilTag-family only -- ArUco
    records are filtered out before assignment, so this field never
    carries one on the real (post-filter) path.
    """

    data: bytes
    status: Optional[str] = None
    tags: Optional[Tuple[TagRecord, ...]] = None


class Window:
    """A live-view display surface: encoded bytes in, pixels on screen.

    A plain base class, not ``typing.Protocol`` (see
    ``daemon/interfaces.py``'s module docstring for the codebase-wide
    design rule): :class:`TkWindow` derives from this for real use;
    tests derive a fake so :class:`StreamLoop` is exercised with no real
    GUI toolkit instantiated.
    """

    def show(self, data: bytes) -> None:
        """Decode *data* (an encoded image -- JPEG or PNG) and display
        it, replacing whatever was shown before. Never draws anything
        beyond the pixels handed in -- any overlay is baked into *data*
        upstream (``client/renderer.py``) or, on the raw path, simply
        absent."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.show")

    def set_status(self, text: Optional[str]) -> None:
        """An advisory line shown in the window chrome, never drawn
        onto the image itself -- e.g. the ``frame_dark`` advisory.
        ``None`` clears it."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.set_status")

    def pump(self) -> bool:
        """Process one tick of the toolkit's event loop. Returns
        ``False`` once the user has closed the window -- the signal
        :class:`StreamLoop` uses to stop -- ``True`` otherwise."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.pump")

    def close(self) -> None:
        """Release the window and any toolkit resources. Idempotent."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Window.close")

    def update_tags(self, tags: Optional[Tuple[TagRecord, ...]]) -> None:
        """Refresh any side panel with *tags* -- the current frame's full
        tag list, or ``None``. Concrete no-op default (deliberate
        exception to this class's raise-``NotImplementedError``
        convention -- see sprint 032's sprint.md Design Rationale): a
        window with no side panel has no wrong way to handle a tag
        update."""
        return None


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


#: Tag-family abbreviations for the sidebar's ID column -- short enough
#: to keep the column narrow while still telling AprilTag 7 and ArUco 7
#: apart, since a single playfield can legitimately carry both
#: (``types.TagFamily``'s own docstring: family is part of a tag's
#: identity, never inferred from the number alone).
_TAG_FAMILY_ABBR = {
    "apriltag": "AT",
    "aruco": "AR",
}

#: The sidebar's header row -- column labels over the same fixed-width
#: fields :func:`_format_tag_row` produces, so headers and data line up
#: under a monospace font.
_SIDEBAR_HEADER = f"{'ID':<6}{'PxX':>7}{'PxY':>7}{'WldX':>7}{'WldY':>7}{'Ang':>6}{'Speed':>8}"


def _format_sidebar_number(value: Optional[float], *, width: int) -> str:
    """One numeric sidebar cell, right-aligned to *width* -- ``"--"``
    when *value* is ``None`` (an absent field, e.g. an uncalibrated
    observation's world position), a fixed one-decimal number
    otherwise, so every row's columns stay aligned under a monospace
    font regardless of which fields a given record carries."""
    if value is None:
        return f"{'--':>{width}}"
    return f"{value:{width}.1f}"


def _format_tag_row(record: TagRecord) -> str:
    """One sidebar row for *record*, ordered by :func:`_format_sidebar_text`
    by ``tag.number`` -- ID, pixel x/y, world x/y (cm), orientation
    (degrees, from ``yaw_rad``), and velocity, with ``"--"`` for any
    field *record* does not carry (sprint 032 ticket 002, Description's
    row-content table).

    **Velocity presentation choice**: shown as ``record.speed`` -- a
    single cm/s magnitude -- rather than ``world_velocity``'s separate
    x/y components. Either satisfies the driving issue; a single number
    reads more compactly in a narrow sidebar column, and this is the
    one column where this module picked one of two documented-equal
    options rather than the table dictating it.

    A pure function of *record* alone -- no ``tkinter`` import needed
    to exercise it, per the ticket's own testing note.
    """
    family = _TAG_FAMILY_ABBR.get(record.tag.family.value, "?")
    tag_id = f"{family}{record.tag.number}"
    pixel_x = _format_sidebar_number(record.pixel.x if record.pixel is not None else None, width=7)
    pixel_y = _format_sidebar_number(record.pixel.y if record.pixel is not None else None, width=7)
    world_x = _format_sidebar_number(record.world.x if record.world is not None else None, width=7)
    world_y = _format_sidebar_number(record.world.y if record.world is not None else None, width=7)
    angle = _format_sidebar_number(
        math.degrees(record.yaw_rad) if record.yaw_rad is not None else None, width=6
    )
    speed = _format_sidebar_number(record.speed, width=8)
    return f"{tag_id:<6}{pixel_x}{pixel_y}{world_x}{world_y}{angle}{speed}"


def _format_sidebar_text(tags: Optional[Tuple[TagRecord, ...]]) -> str:
    """The sidebar's full plain-text content for one :meth:`Window.update_tags`
    call: the header row plus one row per tag in *tags*, ordered by
    ``tag.tag.number`` (sprint 032 ticket 002/sprint.md Scope -- no
    mobile/stationary split, one single ordered list). ``None`` or an
    empty *tags* still produces the header alone, so the panel reads as
    "no tags currently detected" rather than a blank pane.

    Kept independent of any real ``tkinter`` widget -- per the ticket's
    own testing note, this is the piece that is unit-tested directly;
    :class:`TkWindow` only has to pour the string into a widget.
    """
    lines = [_SIDEBAR_HEADER]
    if tags:
        for record in sorted(tags, key=lambda r: r.tag.number):
            lines.append(_format_tag_row(record))
    return "\n".join(lines)


#: How long a tag may go completely unseen before its sidebar row is
#: dropped outright (sprint 033 ticket 002) -- the stakeholder-specified
#: retention window, verbatim from the driving issue
#: (`clasi/issues/sidebar-tag-list-sticky-30s-grey-ghosting.md`): "the
#: list should retain a tag in there for 30 seconds at least before it
#: drops out." A tag absent for less than this stays listed, greyed (see
#: `_TagRoster` below), so ordinary single-frame detection flicker never
#: makes a row disappear and reappear -- only a genuine >=30s absence
#: does.
_GHOST_HORIZON_S = 30.0

#: A ghosted sidebar row's Tk text-tag foreground color -- applied one
#: layer above the pure `_format_tag_row`/`_format_sidebar_text`
#: (sprint.md Design Rationale: color is a Tk-rendering concern, decided
#: by `TkWindow`, never folded into the row formatter itself).
_GHOST_ROW_COLOR = "grey50"

#: The Tk text-tag name `TkWindow.update_tags` applies to (and clears
#: from) a ghosted row's line range via `tag_add`/`tag_remove`.
_GHOST_TK_TAG = "ghost"


@dataclass(frozen=True)
class _RosterEntry:
    """One tracked tag id's sticky-roster bookkeeping (sprint 033
    ticket 002): its most recent :class:`~aprilcam.types.TagRecord` --
    refreshed only while live, retained unchanged while ghosted, per
    SUC-002's "last-seen values, not blanked" contract -- plus the
    monotonic timestamp of its last *live* sighting, and whether it was
    present in the most recently processed frame. Frozen and always
    replaced wholesale by :meth:`_TagRoster.update`, never mutated in
    place, matching this module's other value-shaped dataclasses (e.g.
    :class:`DisplayFrame`)."""

    record: TagRecord
    last_seen: float
    live: bool


class _TagRoster:
    """Sticky, per-tag-id bookkeeping for the playfield sidebar (sprint
    033 ticket 002, driving issue
    `clasi/issues/sidebar-tag-list-sticky-30s-grey-ghosting.md`).

    Given each frame's already AprilTag-filtered live tags (sprint 033
    ticket 001's own filter -- this class trusts it and never re-filters
    by family itself beyond the defense-in-depth guard in
    :meth:`update`), tracks each tag id's live/ghost/dropped lifecycle
    across frames, per sprint.md's state diagram:

    * A tag present this frame is (re)admitted live, values fully
      refreshed, whether or not it was tracked before.
    * A previously-tracked tag absent this frame is ghosted
      *immediately* -- no multi-frame grace period -- keeping its
      last-seen :class:`~aprilcam.types.TagRecord` untouched.
    * A tag (live or ghosted) unseen for :data:`_GHOST_HORIZON_S` or
      more, measured from its last *live* sighting, is dropped outright.

    Pure and Tk-free -- no ``tkinter`` import anywhere in this class --
    so its transitions are unit-testable with an injected fake clock and
    no real window (the ticket's own acceptance criterion).
    :class:`TkWindow` is this class's sole consumer (sprint.md Design
    Rationale: one consumer, no reason for a new module).

    *clock* defaults to :func:`time.monotonic` -- injectable so tests
    control elapsed time without sleeping, mirroring this module's other
    injectable-callable conventions (e.g. :class:`StreamLoop`'s
    *sleep*).
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: "dict[TagId, _RosterEntry]" = {}

    def update(self, tags: Optional[Tuple[TagRecord, ...]]) -> None:
        """Feed one frame's live tags into the roster.

        Refreshes any entry present this frame (fully replacing its
        record and last-seen timestamp), marks any previously-tracked
        entry absent this frame ghosted, and drops any entry whose last
        live sighting is :data:`_GHOST_HORIZON_S` or further in the
        past. Touches only this frame's tags plus the currently-tracked
        entries -- cost proportional to the number of tracked ids, never
        to elapsed wall time -- since :meth:`TkWindow.update_tags` calls
        this on every streamed frame at ``_PUMP_INTERVAL_S``'s ~20Hz
        cadence.

        A non-AprilTag record, if one is ever handed in despite ticket
        001's own upstream filter, is skipped outright -- defense in
        depth, per this ticket's own acceptance criterion that an ArUco
        record is never admitted to the roster.
        """
        now = self._clock()
        present_ids: "set[TagId]" = set()
        for record in tags or ():
            if record.tag.family != TagFamily.APRILTAG:
                continue
            present_ids.add(record.tag)
            self._entries[record.tag] = _RosterEntry(record=record, last_seen=now, live=True)

        for tag_id, entry in list(self._entries.items()):
            if tag_id in present_ids:
                continue
            if now - entry.last_seen >= _GHOST_HORIZON_S:
                del self._entries[tag_id]
            elif entry.live:
                self._entries[tag_id] = _RosterEntry(
                    record=entry.record, last_seen=entry.last_seen, live=False
                )

    def snapshot(self) -> Tuple[Tuple[TagRecord, bool], ...]:
        """The roster's current rows, ordered by tag number -- each a
        ``(record, ghosted)`` pair, live and ghosted rows interleaved in
        one list (sprint.md's "no separate stale section"). ``ghosted``
        is ``True`` exactly when the entry is not currently live."""
        entries = sorted(self._entries.values(), key=lambda entry: entry.record.tag.number)
        return tuple((entry.record, not entry.live) for entry in entries)


class TkWindow(Window):
    """The real, tkinter-backed :class:`Window`. See the module
    docstring for the headless-detection strategy.

    *show_sidebar* (sprint 032 ticket 002, default ``False`` -- exactly
    today's layout when omitted): when ``True``, builds a second
    ``tk.Frame`` packed beside the image, inside the same ``tk.Tk()``
    root (v1's ``left_frame``/``right_frame`` shape,
    ``archive/src/aprilcam/cli/view_cli.py:783-955`` for reference --
    the *layout idea* only, not its widget code), holding a monospace
    ``tk.Text`` refreshed by :meth:`update_tags`.

    **Sticky roster (sprint 033 ticket 002).** :meth:`update_tags` feeds
    every call into a private :class:`_TagRoster` and renders its
    current live+ghosted snapshot, applying a Tk text tag
    (:data:`_GHOST_TK_TAG`) to grey out ghosted rows' lines. The roster
    is constructed unconditionally (harmless when ``show_sidebar`` is
    ``False``, since :meth:`update_tags` returns before ever touching
    it in that case).
    """

    def __init__(self, title: str, *, show_sidebar: bool = False) -> None:
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
        self._roster = _TagRoster()

        image_parent: tk.Misc = self._root
        self._sidebar_text: Optional[tk.Text] = None
        if show_sidebar:
            # Sidebar is packed first so it keeps its fixed width; the
            # image frame takes whatever room is left and grows/shrinks.
            sidebar_frame = tk.Frame(self._root)
            sidebar_frame.pack(side=tk.RIGHT, fill=tk.Y)
            image_frame = tk.Frame(self._root)
            image_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            image_parent = image_frame

            sidebar_text = tk.Text(sidebar_frame, width=48, font=("Courier", 10))
            sidebar_text.pack(fill=tk.BOTH, expand=True)
            sidebar_text.tag_config(_GHOST_TK_TAG, foreground=_GHOST_ROW_COLOR)
            sidebar_text.insert("1.0", _format_sidebar_text(None))
            sidebar_text.configure(state=tk.DISABLED)
            self._sidebar_text = sidebar_text

        self._status_var = tk.StringVar(value="")
        tk.Label(
            image_parent, textvariable=self._status_var, fg="#cc8800", anchor="w"
        ).pack(side=tk.BOTTOM, fill=tk.X)

        # The image area is a frame with geometry propagation disabled
        # and the label *placed* (not packed) inside it, so the image's
        # size never feeds back into the window's size: the causal
        # direction is strictly window -> area -> image (no grow loop).
        self._image_area = tk.Frame(image_parent, bg="black", width=1, height=1)
        self._image_area.pack_propagate(False)
        self._image_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._image_label = tk.Label(self._image_area, bg="black", borderwidth=0)
        self._image_label.place(relx=0.5, rely=0.5, anchor="center")
        self._image_area.bind("<Configure>", self._on_area_configure)
        self._area_size: Tuple[int, int] = (1, 1)
        self._last_data: Optional[bytes] = None

        # tkinter drops a PhotoImage the instant nothing keeps a Python
        # reference to it, even while the Label is still displaying it
        # -- this attribute is that reference, replaced (not appended
        # to) on every show() call.
        self._photo: Optional[ImageTk.PhotoImage] = None

    def _on_close(self) -> None:
        self._closed = True

    def _on_area_configure(self, event: "tk.Event") -> None:
        # Re-render the cached frame at the new size right away rather
        # than waiting for the next streamed frame.
        size = (int(event.width), int(event.height))
        if size == self._area_size:
            return
        self._area_size = size
        if self._last_data is not None:
            self._render(self._last_data)

    def show(self, data: bytes) -> None:
        first = self._last_data is None
        self._last_data = data
        if first:
            # Seed the initial window size from the frame (capped at
            # _MAX_DISPLAY_WIDTH); afterwards the user owns the size.
            with Image.open(io.BytesIO(data)) as probe:
                src_w, src_h = probe.size
            init_w, init_h = _fit_size(
                src_w, src_h, min(src_w, _MAX_DISPLAY_WIDTH), max(src_h, 1)
            )
            self._area_size = (init_w, init_h)
            self._image_area.configure(width=init_w, height=init_h)
        self._render(data)

    def _render(self, data: bytes) -> None:
        image: Image.Image = Image.open(io.BytesIO(data))
        image.load()
        w, h = _fit_size(image.width, image.height, *self._area_size)
        if (w, h) != image.size:
            image = image.resize((w, h), Image.Resampling.LANCZOS)
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

    def update_tags(self, tags: Optional[Tuple[TagRecord, ...]]) -> None:
        """Refresh the side panel built when ``show_sidebar=True`` --
        a no-op (inherited behaviour, not overridden away) when this
        window has none, so :meth:`StreamLoop.run` can call this
        unconditionally regardless of which mode this window was built
        in.

        Sprint 033 ticket 002: *tags* is fed into this window's private
        :class:`_TagRoster` first, and the roster's current live+ghosted
        snapshot -- not *tags* directly -- is what gets rendered, so a
        tag absent this frame stays listed (greyed) rather than
        vanishing. ``_format_tag_row``/``_format_sidebar_text`` stay
        pure and unchanged; only the per-line grey :data:`_GHOST_TK_TAG`
        is applied here, one layer above them.
        """
        if self._sidebar_text is None:
            return None
        self._roster.update(tags)
        rows = self._roster.snapshot()
        text = _format_sidebar_text(tuple(record for record, _ in rows))

        self._sidebar_text.configure(state=tk.NORMAL)
        self._sidebar_text.delete("1.0", tk.END)
        self._sidebar_text.insert("1.0", text)
        self._sidebar_text.tag_remove(_GHOST_TK_TAG, "1.0", tk.END)
        for offset, (_, ghosted) in enumerate(rows):
            if not ghosted:
                continue
            line = offset + 2  # line 1 is the header; rows start at line 2
            self._sidebar_text.tag_add(_GHOST_TK_TAG, f"{line}.0", f"{line}.end")
        self._sidebar_text.configure(state=tk.DISABLED)
        return None


class StreamLoop:
    """Streaming display loop (ticket 027-007): a background thread
    iterates *stream*, converts each yielded ``ImageFrame`` via
    *convert* into a :class:`DisplayFrame`, and stores it in a
    lock-protected single-slot "latest frame" cell; the calling thread
    -- Tk's main thread, via :meth:`run` -- pumps *window* and samples
    that cell on its own cadence (*pump_interval*), never blocking on
    the stream itself. Older undisplayed frames are dropped (replace,
    not append) -- see the module docstring's backpressure note.

    This is the plumbing both :func:`run_raw_view` and
    :func:`run_playfield_view` share unchanged (module docstring's
    "reuse seam"): only *stream* (which ``deskewed`` value it was
    opened with) and *convert* differ between the raw and deskewed+tags
    views.

    Parameters
    ----------
    window:
        Where frames are displayed.
    stream:
        The subscription to read from -- closed by :meth:`run` in its
        own cleanup, regardless of how the loop ends.
    convert:
        Turns one streamed ``ImageFrame`` into a :class:`DisplayFrame`.
        Called on the background reader thread, not the caller's --
        must not touch *window* itself.
    pump_interval / sleep:
        Overridable so tests exercise the sampling cadence without a
        real clock cost (``_PUMP_INTERVAL_S``/``time.sleep`` by
        default).
    camera:
        The camera name -- carried only so a bounded first-frame
        timeout (below) can name it in :class:`FirstFrameTimeout`
        without threading the value through separately. Never used for
        anything else (no daemon call, no lookup).
    first_frame_timeout_s:
        How long (wall-clock, ``time.monotonic()``-measured) :meth:`run`
        waits for a first frame before giving up and raising
        :class:`FirstFrameTimeout` (sprint 038, SUC-004) --
        :data:`_DEFAULT_FIRST_FRAME_TIMEOUT_S` in production, overridden
        short in tests. Once any frame has been received, this bound
        never fires again for the rest of the stream's life -- it
        bounds only the *first* frame, not an ongoing liveness check
        (see the module docstring's "Open Questions" analogue in
        ``view-DESIGN.md`` §6 for why that is a deliberate scope limit).

    **Best-effort thread cleanup.** ``stream.close()`` closes the
    client-side socket (``client/streams.py``'s ``SocketImageStream``);
    if the reader thread happens to be blocked inside a socket read at
    that exact moment, how promptly that read unblocks is a property of
    the socket/threading layer this class does not own (out of this
    module's scope) -- :data:`_READER_JOIN_TIMEOUT_S` bounds how long
    :meth:`run` waits either way, so a wedged reader thread can never
    block window shutdown forever; it is left to exit as a daemon
    thread instead.
    """

    def __init__(
        self,
        window: Window,
        stream: ImageStream,
        convert: Callable[[ImageFrame], DisplayFrame],
        *,
        pump_interval: float = _PUMP_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
        camera: str = "",
        first_frame_timeout_s: float = _DEFAULT_FIRST_FRAME_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._stream = stream
        self._convert = convert
        self._pump_interval = pump_interval
        self._sleep = sleep
        self._camera = camera
        self._first_frame_timeout_s = first_frame_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: Optional[DisplayFrame] = None
        self._error: Optional[Exception] = None

    def _read_stream(self) -> None:
        """Runs on the background reader thread: iterate ``stream``,
        converting and replacing the single-slot cell on every frame.
        Any exception the stream raises while iterating (e.g. an
        ``aprilcam.errors.AprilCamError`` from a dropped connection) is
        caught here and relayed to :meth:`run`, which re-raises it on
        the caller's own thread once the loop has stopped -- a bare
        thread target that raised would otherwise just print a
        traceback and vanish silently."""
        try:
            for frame in self._stream:
                displayed = self._convert(frame)
                with self._lock:
                    self._latest = displayed
        except Exception as exc:  # noqa: BLE001 - relayed to run(), never swallowed
            with self._lock:
                self._error = exc

    def _take_latest(self) -> "tuple[Optional[DisplayFrame], Optional[Exception]]":
        with self._lock:
            frame, self._latest = self._latest, None
            return frame, self._error

    def run(self) -> None:
        """Start the reader thread, then pump *window* and display the
        latest available frame until the window closes, the reader
        thread reports a failure, or no first frame has arrived within
        ``first_frame_timeout_s`` -- whichever happens first.

        Returns normally once :meth:`Window.pump` reports the window
        closed -- never raises for that case, since a closed window is
        the expected way this loop ends. Re-raises whatever exception
        the stream raised, once the reader thread has stopped, if that
        is instead why the loop ended. Raises :class:`FirstFrameTimeout`
        (sprint 038, SUC-004) if ``first_frame_timeout_s`` elapses with
        no frame ever having been received -- checked on the caller's
        own thread (this loop), the same place "has anything ever been
        shown yet" is already tracked, so no new cross-thread signal is
        needed. Once a first frame has been received, this check never
        fires again for the rest of the loop's life -- it bounds only
        the *first* frame.
        """
        thread = threading.Thread(target=self._read_stream, name=_READER_THREAD_NAME, daemon=True)
        thread.start()
        start = self._clock()
        first_frame_received = False
        try:
            while self._window.pump():
                frame, error = self._take_latest()
                if error is not None:
                    raise error
                if frame is not None:
                    first_frame_received = True
                    self._window.show(frame.data)
                    self._window.update_tags(frame.tags)
                    self._window.set_status(frame.status)
                elif (
                    not first_frame_received
                    and (self._clock() - start) >= self._first_frame_timeout_s
                ):
                    raise FirstFrameTimeout(self._camera)
                self._sleep(self._pump_interval)
        finally:
            self._stream.close()
            thread.join(timeout=_READER_JOIN_TIMEOUT_S)


def _convert_raw_frame(frame: ImageFrame) -> DisplayFrame:
    """One streamed frame's raw-view display payload (UC-16, SUC-002).
    See the module docstring's "raw means raw is enforced by omission"
    note -- this never reads ``frame.tags``."""
    status = "frame dark -- image may be hard to see" if frame.frame_dark else None
    return DisplayFrame(data=frame.data, status=status)


def run_raw_view(
    camera: str,
    daemon: Daemon,
    *,
    window_factory: Callable[[str], Window] = TkWindow,
    first_frame_timeout_s: float = _DEFAULT_FIRST_FRAME_TIMEOUT_S,
) -> None:
    """``camera view <camera>`` (UC-16, SUC-002): open a window and
    subscribe to ``stream_images(camera, deskewed=False)``, displaying
    each streamed frame's raw bytes exactly as received -- no drawn
    overlays of any kind.

    Raises :class:`NoDisplayAvailable` if no display can be opened
    (propagated from *window_factory*, i.e. before any daemon call is
    made) -- ``cli/camera.py`` is the boundary that turns this into the
    UC-16.S-3 message pointing at ``camera image``. Any
    ``aprilcam.errors.AprilCamError`` raised mid-view (e.g. the camera
    disconnects) propagates the same way, after the window is closed.
    **Sprint 038, SUC-004**: :class:`FirstFrameTimeout` propagates the
    same way too -- if no frame arrives within *first_frame_timeout_s*,
    the window is closed in this function's existing ``finally`` and
    the exception propagates unchanged; ``cli/camera.py``'s ``cmd_view``
    is the boundary that turns it into a diagnostic naming the camera
    and the likely cause.

    The window title carries an explicit ``[RAW]`` marker for the whole
    session (sprint 031 ticket 004, SUC-004) -- UC-16's "raw means raw"
    guarantee is a deliberate diagnostic tool (how an operator tells a
    camera problem from a geometry problem apart from ``playfield
    view``), and the stakeholder hit exactly this confusion in practice
    expecting deskew from this command. This is signposting only: the
    stream subscription and every byte handed to the window are
    unchanged from before this ticket.
    """
    window = window_factory(f"aprilcam camera view [RAW] -- {camera}")
    try:
        stream = daemon.stream_images(camera, deskewed=False)
        StreamLoop(
            window,
            stream,
            _convert_raw_frame,
            camera=camera,
            first_frame_timeout_s=first_frame_timeout_s,
        ).run()
    finally:
        window.close()


def _make_playfield_converter(renderer: RendererBase) -> Callable[[ImageFrame], DisplayFrame]:
    """Builds the deskewed+tags view's per-frame converter (UC-18,
    ticket 022-006, adapted for streaming by ticket 027-007).

    Hands each streamed, already-deskewed ``ImageFrame`` straight to
    *renderer* (``client/renderer.py``'s ``Renderer.render()``), which
    draws tag boundaries/hats from the frame's own embedded ``tags``
    and any embedded ``annotations`` -- no second query, since a
    deskewed frame already carries both (module docstring). ``
    calibration_stale`` (UC-18's stale-mid-view failure mode) and
    ``frame_dark`` both surface as the window's advisory status line --
    never drawn onto the image itself, same as :func:`_convert_raw_frame`'s
    ``frame_dark`` handling.

    AprilTag-family only for the sidebar (sprint 033 ticket 001):
    ``DisplayFrame.tags`` is filtered to ``TagFamily.APRILTAG`` records
    before assignment, so the field's ArUco calibration corner markers
    are never listed. This is independent of the equivalent guard in
    ``client/renderer.py``'s ``Renderer.render()`` (sprint 033
    sprint.md Design Rationale: two inline checks at the two points
    that each read a frame's tag list, not a shared predicate) --
    *renderer* above still receives the frame's unfiltered ``tags`` and
    applies its own guard when drawing the overlay.
    """

    def convert(frame: ImageFrame) -> DisplayFrame:
        data = renderer.render(frame)

        status_parts = []
        if frame.calibration_stale:
            status_parts.append(
                "calibration stale -- re-run 'aprilcam camera calibrate' (UC-18)"
            )
        if frame.frame_dark:
            status_parts.append("frame dark -- image may be hard to see")
        status = "; ".join(status_parts) if status_parts else None

        if frame.tags is None:
            tags = None
        else:
            tags = tuple(
                record for record in frame.tags.tags if record.tag.family == TagFamily.APRILTAG
            )
        return DisplayFrame(data=data, status=status, tags=tags)

    return convert


def _convert_bare_playfield_frame(frame: ImageFrame) -> DisplayFrame:
    """``playfield view --bare``'s per-frame converter (sprint 032
    ticket 002): mirrors :func:`_convert_raw_frame`'s zero-overhead
    shape, but for the deskewed stream -- ``data=frame.data`` unmodified
    (no ``Renderer.render()`` call at all), and ``tags`` left at its
    ``None`` default (never reads ``frame.tags``), so a bare window's
    (sidebar-less, per ``show_sidebar=False``) ``update_tags`` call
    stays the inherited no-op. Only ``calibration_stale``/``frame_dark``
    still surface as the window's advisory status line, exactly like
    :func:`_make_playfield_converter`'s ``convert``."""
    status_parts = []
    if frame.calibration_stale:
        status_parts.append(
            "calibration stale -- re-run 'aprilcam camera calibrate' (UC-18)"
        )
    if frame.frame_dark:
        status_parts.append("frame dark -- image may be hard to see")
    status = "; ".join(status_parts) if status_parts else None

    return DisplayFrame(data=frame.data, status=status)


def run_playfield_view(
    camera: str,
    daemon: Daemon,
    *,
    renderer_factory: Callable[[], RendererBase] = PillowRenderer,
    window_factory: Callable[..., Window] = TkWindow,
    bare: bool = False,
    first_frame_timeout_s: float = _DEFAULT_FIRST_FRAME_TIMEOUT_S,
) -> None:
    """``playfield view`` (UC-18, ticket 022-006, streaming per ticket
    027-007): open a window and subscribe to ``stream_images(camera,
    deskewed=True)`` **only** -- no ``get_tags``/``stream_tags`` call --
    rendering each streamed frame's embedded tag boundaries and "hats"
    through *renderer_factory* (``client/renderer.py`` by default)
    before display, reusing :class:`StreamLoop`/:class:`Window`
    unchanged (module docstring's "reuse seam").

    *bare* (sprint 032 ticket 002, default ``False``): when ``False``
    (the annotated default), the window is built with
    ``show_sidebar=True`` and each frame is rendered through
    *renderer_factory* with :class:`DisplayFrame`'s ``tags`` populated
    from the frame's own embedded ``TagFrame``, feeding the sidebar
    :meth:`StreamLoop.run` refreshes on every frame. When ``True``, the
    window is built with ``show_sidebar=False`` and frames are shown via
    :func:`_convert_bare_playfield_frame` -- *renderer_factory* is never
    even called, and ``DisplayFrame.tags`` stays ``None`` -- reproducing
    today's plain image-only behaviour (this ticket's own regression
    coverage for that path).

    As a side effect, opening this view is what makes the target
    camera's ring buffer start filling for as long as the window stays
    open and the daemon keeps at least one subscriber -- **not** simply
    because ``ImageStreamProducer`` publishes, but because
    ``daemon.service.DaemonService``'s deskewed-producer wiring
    (``DaemonService._build_deskewed_image_producer``) piggybacks a
    keep-alive subscription onto the camera's ``TagStreamProducer`` for
    the life of this view's stream. ``ImageStreamProducer`` itself never
    calls ``DetectionEngine.run_burst`` -- it only reads whatever the
    tag stream (this keep-alive, a real ``stream_tags`` caller, or nothing)
    has already put in the buffer (``daemon/streaming.py``'s own
    docstring) -- so a fix confirmed against real hardware (a `playfield
    view` session that showed a newly-appeared, already-confirmed tag
    only via a separate `get_tags` poll, never via the view itself)
    landed the keep-alive wiring specifically because subscribing to
    the image stream alone used to *not* do this, contrary to what an
    earlier draft of this note claimed.

    Raises :class:`NoDisplayAvailable` if no display can be opened
    (propagated from *window_factory*, i.e. before any daemon call is
    made). Raises :class:`~aprilcam.errors.NotCalibrated` if *camera* is
    not calibrated -- propagated from the ``stream_images`` call itself,
    **never** caught here to substitute a raw stream (see the module
    docstring's "never fall back" note). ``cli/playfield.py`` is the
    boundary that turns either exception into a UC-18-worded message.
    Any other ``aprilcam.errors.AprilCamError`` raised mid-view
    propagates the same way, after the window is closed.
    """
    window = window_factory(
        f"aprilcam playfield view -- {camera}", show_sidebar=not bare
    )
    try:
        stream = daemon.stream_images(camera, deskewed=True)
        convert: Callable[[ImageFrame], DisplayFrame]
        if bare:
            convert = _convert_bare_playfield_frame
        else:
            convert = _make_playfield_converter(renderer_factory())
        StreamLoop(
            window,
            stream,
            convert,
            camera=camera,
            first_frame_timeout_s=first_frame_timeout_s,
        ).run()
    finally:
        window.close()
