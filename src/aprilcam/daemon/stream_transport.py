"""Bridges an in-process ``ImageStream``/``TagStream`` subscription
(``daemon.service.DaemonService.stream_images``/``stream_tags``,
tickets 027-003/004) to a real, remote socket client -- ``design.md``
section 5's "bulk streams: separate sockets... framing
``[uint32 BE length][protobuf]``" contract, using ``aprilcam.framing``
for the actual byte-level framing.

**Pure transport, no vision, no proto.** This module never imports
``aprilcam.proto`` -- ``daemon/rpc.py`` is the sole owner of the
Python-dataclass-to-proto-message boundary (its own module docstring:
"nothing outside it imports ``aprilcam.proto.aprilcam_v2_pb2``
directly"), so :class:`StreamTransportManager` takes *already-wired*
``image_encoder``/``tag_encoder`` callables (``aprilcam.types.ImageFrame``/
``TagFrame`` -> already-serialized protobuf ``bytes``) rather than
constructing proto messages itself. ``daemon/rpc.py``'s
:func:`~aprilcam.daemon.rpc.build_stream_transport` is what wires those
callables in for a real daemon.

**Subscribe errors are sent, not swallowed (sprint 043 ticket 003).**
Preconditions (``UnknownCamera``, ``CameraNotPresent``, ``NotCalibrated``)
surface inside the sender thread's ``subscribe`` call. When it raises, the
exception is still logged, then -- best effort, send failures ignored --
the connection receives exactly one *error frame* before it closes. An
error frame is an ordinary length-prefixed frame whose payload is
``framing.ERROR_FRAME_MARKER`` (the single byte ``0x00``) followed by a
serialized ``StreamError`` (kind + message, built by the injected
*error_encoder*). ``0x00`` cannot begin a valid protobuf message, so it is
unambiguous against ``GetFrameResponse``/``TagFrame`` data frames (even an
empty all-defaults ``TagFrame``), and successful streams never carry it --
they stay byte-identical. ``client/streams.py`` decodes it and raises the
matching typed error.

**Endpoint reuse, not one-shot allocation** (sprint.md Open Question
3's recommended default). :meth:`StreamTransportManager.
get_or_create_image_endpoint`/:meth:`get_or_create_tag_endpoint` bind a
listener the first time a given key (``(camera, deskewed)`` for images,
``camera`` for tags) is requested and hand back the *same* endpoint on
every later call for that key -- a second client asking for the same
product connects to the same socket(s) as the first, rather than
causing a second bind. This is safe against "frame geometry stable for
the life of a stream" (UC-07) because the key this manager reuses on
(``camera``, ``deskewed``) is *exactly* the key
``DaemonService.stream_images``/``stream_tags`` already dedupe their
own producer on (``daemon/service.py``'s ``_image_stream_producers``/
``_tag_stream_producers``, tickets 003/004) -- there is no additional,
independently-varying parameter two calls with the same key could
disagree about, so two sockets for the same key can only ever resolve
to the *same* producer, whose deskew geometry
(``ImageStreamProducer``'s ``_DeskewContext``) is snapshotted once, at
that producer's own first-subscriber transition, and never touched
again for its life. Nothing at this layer needed to add its own
conflict detection on top of that -- there is nothing left for it to
conflict about.

**Idle gating holds at this layer too.** Binding a listener and running
its accept loop touches no camera or vision code and starts no
``Daemon`` method -- "listening for a connection costs nothing"
(sprint.md). Only an *accepted* connection's own sender thread calls
``DaemonService.stream_images``/``stream_tags`` (via the ``subscribe``
callable :class:`StreamTransportManager` builds), which is the exact
moment ticket 003/004's ``Broadcaster.subscribe()`` fires its
``on_active`` callback and a producer thread actually starts. One
socket connection is one ``Broadcaster`` subscriber -- ticket 001's
per-subscriber-queue model, extended one-to-one through this transport
layer (never a second buffering layer on top of ``Broadcaster``'s own
bounded queue: a sender thread just drains its subscription onto its
socket as fast as it can, per this ticket's own Backpressure note).

**No eager camera/calibration validation in ``get_or_create_*``.**
Validating would mean calling ``DaemonService.stream_images``/
``stream_tags`` from the RPC handler itself, which would either
(a) subscribe successfully, immediately starting a producer with zero
real socket clients -- violating "allocating an endpoint must not
start capture/detection" -- or (b) require a second, independent
implementation of ``_camera_for``'s slug-or-number resolution here,
duplicating logic ``daemon/rpc.py``'s own module docstring already
warns against. Instead, resolution and every precondition
(``UnknownCamera``, ``CameraNotPresent``, ``NotCalibrated``) run inside
the first accepted connection's sender thread, at the moment it calls
``subscribe()`` -- if that raises, the connection is closed without
ever sending a byte, logged, and no producer is left running. A caller
that mistypes a camera name gets a socket that connects then
immediately closes, not a gRPC error at allocation time; ticket 006
(the client side) is expected to treat "connected then closed with no
frame" as a real failure to surface, not a transient hiccup.

**Adapted from -- not copied wholesale from -- v1's ``_BaseProducer``**
(``archive/src/aprilcam/daemon/stream.py``): the bind-unix/bind-tcp/
accept-loop/per-connection-sender-thread mechanics are the earned
starting point, but v1 coupled production (capture/encode/publish) and
transport (sockets) in one class hierarchy and allocated a fresh,
uniquely-named socket pair on every ``start()`` call. This module only
ever touches sockets and threads -- production lives entirely in
``daemon/streaming.py`` (reached only via the ``subscribe`` callable) --
and reuses one listener per key rather than minting a new one per
allocation call (see "Endpoint reuse" above).
"""

from __future__ import annotations

import errno
import logging
import re
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from aprilcam import framing
from aprilcam.daemon.interfaces import Daemon
from aprilcam.types import ImageFrame, TagFrame

__all__ = [
    "MAX_UNIX_SOCKET_PATH_BYTES",
    "unix_socket_path","StreamEndpoint", "StreamTransportManager"]

logger = logging.getLogger(__name__)

#: How often an accept loop wakes to re-check its stop event -- bounds
#: how long ``StreamTransportManager.stop``'s accept-thread join can
#: take when ``server_sock.close()`` alone does not unblock a pending
#: ``accept()`` (platform-dependent).
_ACCEPT_POLL_TIMEOUT_S = 1.0

#: How long ``_Listener.stop`` waits for each accept/sender thread to
#: join before giving up -- mirrors ``daemon/rpc.py``'s
#: ``_GrpcListener``/``daemon/streaming.py`` producers' own bounded-join
#: shutdown discipline (never blocks forever on a wedged thread).
_JOIN_TIMEOUT_S = 2.0

#: Backlog for both unix and TCP listeners -- matches v1's default
#: (``archive/src/aprilcam/daemon/stream.py``'s ``backlog: int = 5``);
#: this is a local daemon serving a small, bounded set of clients, not
#: a public-facing service.
_LISTEN_BACKLOG = 5

_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_name(name: str) -> str:
    """Filesystem-safe rendering of a listener's key, for its unix
    socket filename. A camera slug is already safe (registry-derived,
    ``camera/identity.py``), but this layer deliberately does not
    resolve/validate ``request.camera`` before using it here (see the
    module docstring's "No eager camera/calibration validation")  --
    so an arbitrary caller-supplied string is treated defensively
    rather than trusted to already be a valid path component."""
    return _UNSAFE_NAME_CHARS.sub("_", name) or "_"


#: Longest unix-socket path this module will try to bind. ``sun_path`` is
#: 104 bytes on macOS (108 on Linux) *including* the trailing NUL; macOS's
#: ``$TMPDIR`` socket dir alone is ~60 bytes, so a stream named after a
#: full camera slug (``stream-tags-arducam-ov9782-usb-camera.sock``)
#: overflowed it on the rig (2026-09-04: "AF_UNIX path too long", the tag
#: stream for camera 3 could not bind while the same stream requested by
#: camera *number* bound fine). Clients learn the path from the
#: ``StreamEndpoint`` the RPC returns, so the on-disk name is free to be
#: shortened.
MAX_UNIX_SOCKET_PATH_BYTES = 100


def unix_socket_path(socket_dir: Path, name: str) -> Path:
    """``socket_dir/stream-<name>.sock``, or -- when that would exceed
    :data:`MAX_UNIX_SOCKET_PATH_BYTES` -- a stable shortened form
    ``stream-<name prefix>-<8 hex of sha1(name)>.sock`` that still fits.
    Deterministic for a given (dir, name), so the same stream always
    gets the same path within a daemon."""
    import hashlib

    safe = _safe_name(name)
    path = socket_dir / f"stream-{safe}.sock"
    if len(str(path).encode()) <= MAX_UNIX_SOCKET_PATH_BYTES:
        return path
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    budget = MAX_UNIX_SOCKET_PATH_BYTES - len(str(socket_dir / f"stream--{digest}.sock").encode())
    prefix = safe[: max(budget, 1)]
    return socket_dir / f"stream-{prefix}-{digest}.sock"


def _bind_unix_socket(socket_dir: Path, name: str) -> "Tuple[Optional[socket.socket], Optional[Path]]":
    """Bind a unix stream socket at ``socket_dir/stream-<name>.sock``,
    retrying once after removing a stale file left by a crashed prior
    run -- same retry shape as v1's ``_bind_unix_socket``. Safe to
    unlink-and-retry here for the same reason ``daemon/rpc.py``'s
    ``build_server`` gives for the control-plane socket: every call
    into this function happens after the daemon's singleton lock is
    already held, so no live daemon can own the path being replaced.
    """
    path = unix_socket_path(socket_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            sock.listen(_LISTEN_BACKLOG)
            return sock, path
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE and attempt == 0:
                logger.warning("stream_transport: stale socket %s -- removing and retrying", path)
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            logger.error("stream_transport: cannot bind unix socket %s: %s", path, exc)
            return None, None
    return None, None  # pragma: no cover - unreachable; satisfies type checker


def _bind_tcp_socket() -> Optional[socket.socket]:
    """Bind a TCP stream socket on all interfaces, OS-assigned ephemeral
    port -- same "bind wide, let the daemon publish which port" choice
    v1 made (``archive/src/aprilcam/daemon/stream.py``'s
    ``_bind_tcp_socket`` docstring), so a remote client on the LAN can
    connect when the daemon runs on a Pi or other remote host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 0))
        sock.listen(_LISTEN_BACKLOG)
        return sock
    except OSError as exc:
        sock.close()
        logger.error("stream_transport: cannot bind tcp socket: %s", exc)
        return None


@dataclass(frozen=True)
class StreamEndpoint:
    """Where a client connects for one stream's raw framed socket.
    Mirrors ``proto/aprilcam_v2.proto``'s ``StreamEndpoint`` message
    field-for-field; ``daemon/rpc.py`` (the sole owner of the proto
    boundary) converts this into that wire message -- this module never
    imports ``aprilcam.proto`` itself (module docstring).

    Either field may be ``None`` (that transport was not offered or
    failed to bind); a caller offering neither has nothing to publish
    and logs the bind failure at construction time.
    """

    socket_path: Optional[str] = None
    tcp_port: Optional[int] = None


#: What a listener's ``subscribe`` callable returns: a lazy iterator of
#: already-framed-payload-ready ``bytes`` (one item per published
#: frame, not yet length-prefixed -- :class:`_Listener` applies
#: ``aprilcam.framing.pack_frame`` itself) plus the zero-argument
#: callable that unsubscribes (``ImageStream.close``/``TagStream.close``).
#: Expressed this way -- concrete ``bytes``/``None`` types, no
#: generic/``Union`` coupling between the stream's item type and its
#: encoder -- specifically so :class:`_Listener` itself needs no
#: knowledge of ``ImageFrame``/``TagFrame`` at all; the item-type-to-
#: encoder pairing is resolved once, inside
#: :class:`StreamTransportManager`'s ``_subscribe_images``/
#: ``_subscribe_tags``, before ``_Listener`` ever sees it.
_Subscription = Tuple[Iterator[bytes], Callable[[], None]]


class _Listener:
    """One reused (unix[, TCP]) listening endpoint for one (camera,
    product) key.

    Binds and starts accepting immediately, in ``__init__`` -- binding
    costs nothing (module docstring's "Idle gating holds at this layer
    too"). Each *accepted* connection gets its own sender thread that
    calls ``subscribe`` exactly once, iterates the ``bytes`` it yields,
    frames each with :func:`aprilcam.framing.pack_frame`, and writes it
    to that connection's socket -- one socket connection is one
    ``Broadcaster`` subscriber (module docstring).
    """

    def __init__(
        self,
        name: str,
        socket_dir: Path,
        *,
        subscribe: Callable[[], _Subscription],
        enable_unix: bool,
        enable_tcp: bool,
        error_encoder: Optional[Callable[[Exception], bytes]] = None,
    ) -> None:
        self._name = name
        self._subscribe = subscribe
        self._error_encoder = error_encoder
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []

        self._lock = threading.Lock()
        self._connections: "Set[socket.socket]" = set()
        self._closers: Dict[socket.socket, Callable[[], None]] = {}

        self._unix_sock: Optional[socket.socket] = None
        self._unix_path: Optional[Path] = None
        self._tcp_sock: Optional[socket.socket] = None
        self._tcp_port: Optional[int] = None

        if enable_unix:
            sock, path = _bind_unix_socket(socket_dir, name)
            if sock is not None:
                self._unix_sock = sock
                self._unix_path = path
                self._start_accept_thread(sock, name=f"stream-accept-unix-{name}")

        if enable_tcp:
            tcp_sock = _bind_tcp_socket()
            if tcp_sock is not None:
                self._tcp_sock = tcp_sock
                self._tcp_port = tcp_sock.getsockname()[1]
                self._start_accept_thread(tcp_sock, name=f"stream-accept-tcp-{name}")

    @property
    def endpoint(self) -> StreamEndpoint:
        return StreamEndpoint(
            socket_path=str(self._unix_path) if self._unix_path is not None else None,
            tcp_port=self._tcp_port,
        )

    # -- accept loop ----------------------------------------------------

    def _start_accept_thread(self, server_sock: socket.socket, *, name: str) -> None:
        t = threading.Thread(target=self._accept_loop, args=(server_sock,), name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _accept_loop(self, server_sock: socket.socket) -> None:
        server_sock.settimeout(_ACCEPT_POLL_TIMEOUT_S)
        while not self._stop_event.is_set():
            try:
                conn, _addr = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                # Either a genuine accept failure or (the common case
                # during shutdown) this listener's own socket was just
                # closed out from under this call by stop() -- either
                # way, this loop's only job is done.
                break
            with self._lock:
                self._connections.add(conn)
            t = threading.Thread(
                target=self._sender_loop,
                args=(conn,),
                name=f"stream-sender-{self._name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    # -- per-connection sender -------------------------------------------

    def _sender_loop(self, conn: socket.socket) -> None:
        try:
            frames, close = self._subscribe()
        except Exception as exc:  # noqa: BLE001 - boundary: a bad subscribe must not kill this thread
            logger.exception("stream_transport(%s): subscribe failed -- closing connection", self._name)
            self._send_error_frame(conn, exc)
            self._drop(conn)
            return

        if self._stop_event.is_set():
            # stop() ran between accept() and here -- never start
            # delivering; unsubscribe immediately so this fleeting
            # subscription never keeps a producer alive.
            close()
            self._drop(conn)
            return

        with self._lock:
            self._closers[conn] = close
        try:
            for payload in frames:
                try:
                    conn.sendall(framing.pack_frame(payload))
                except OSError:
                    break
                if self._stop_event.is_set():
                    break
        finally:
            close()
            self._drop(conn)

    def _send_error_frame(self, conn: socket.socket, exc: Exception) -> None:
        """Best effort: tell the client *why* the subscribe failed, as
        one error frame -- ``framing.ERROR_FRAME_MARKER`` + the
        serialized ``StreamError`` the injected *error_encoder* builds
        (module docstring, "Subscribe errors are sent, not swallowed").
        Any failure here (no encoder wired, encoder bug, client already
        gone) is ignored: the connection is closing either way."""
        encoder = self._error_encoder
        if encoder is None:
            return
        try:
            payload = framing.ERROR_FRAME_MARKER + encoder(exc)
            conn.sendall(framing.pack_frame(payload))
        except Exception:  # noqa: BLE001 - best effort, never mask the close
            logger.debug("stream_transport(%s): could not send error frame", self._name, exc_info=True)

    def _drop(self, conn: socket.socket) -> None:
        with self._lock:
            self._connections.discard(conn)
            self._closers.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass

    # -- shutdown ----------------------------------------------------------

    def stop(self) -> None:
        """Close this listener's socket(s), unsubscribe and close every
        live connection, and join every accept/sender thread with a
        bounded timeout. Idempotent -- a second call finds empty
        connection/closer sets and already-closed sockets, and joining
        an already-finished thread is a harmless no-op.
        """
        self._stop_event.set()
        for sock in (self._unix_sock, self._tcp_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        with self._lock:
            closers = list(self._closers.values())
            conns = list(self._connections)
        for close in closers:
            try:
                close()
            except Exception:  # noqa: BLE001 - shutdown must not abort on one bad unsubscribe
                logger.exception("stream_transport(%s): error closing a subscription", self._name)
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        if self._unix_path is not None:
            try:
                self._unix_path.unlink()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=_JOIN_TIMEOUT_S)
        self._threads.clear()


class StreamTransportManager:
    """Owns every stream socket listener the daemon offers.

    One listener per (camera, product) key, bound the first time that
    key is requested and reused on every later request for the same key
    (module docstring's "Endpoint reuse"). Construction itself touches
    no socket, thread, or ``Daemon`` method -- a freshly-built manager
    with no ``get_or_create_*`` call yet made is fully inert, matching
    an idle daemon's "zero background work until asked" contract at
    every other layer.
    """

    def __init__(
        self,
        service: Daemon,
        socket_dir: Path,
        *,
        image_encoder: Callable[[ImageFrame], bytes],
        tag_encoder: Callable[[TagFrame], bytes],
        error_encoder: Optional[Callable[[Exception], bytes]] = None,
        enable_unix: bool = True,
        enable_tcp: bool = True,
    ) -> None:
        """
        Args:
            service: The in-process ``Daemon`` whose ``stream_images``/
                ``stream_tags`` a real accepted connection subscribes
                to. Never called from this constructor or from
                ``get_or_create_*`` -- only from a sender thread, after
                a real socket connects (module docstring).
            socket_dir: Where unix stream sockets are bound --
                ``directories.socket_dir`` for a real daemon
                (``design.md`` §7: "socket_dir/ # control.sock +
                per-camera stream sockets"), a short ``tmp_path`` for
                tests (unix socket paths have a ~103-character OS
                limit).
            image_encoder / tag_encoder: Serialize one
                ``aprilcam.types.ImageFrame``/``TagFrame`` into the
                exact protobuf-serialized ``bytes`` a stream socket
                sends -- supplied by ``daemon/rpc.py``'s
                :func:`~aprilcam.daemon.rpc.build_stream_transport` so
                this module never imports ``aprilcam.proto`` itself.
            error_encoder: Serialize a failed subscribe's exception into
                a ``StreamError``'s bytes (``daemon/rpc.py`` supplies
                it). Optional -- ``None`` keeps the old behavior of
                closing silently.
            enable_unix / enable_tcp: Which transport(s) each listener
                offers. Both default ``True`` (mirrors v1); a test may
                disable one to keep assertions to a single transport.
        """
        self._service = service
        self._socket_dir = socket_dir
        self._image_encoder = image_encoder
        self._tag_encoder = tag_encoder
        self._error_encoder = error_encoder
        self._enable_unix = enable_unix
        self._enable_tcp = enable_tcp

        self._lock = threading.Lock()
        self._image_listeners: Dict[Tuple[str, bool], _Listener] = {}
        self._tag_listeners: Dict[str, _Listener] = {}

    # -- endpoint allocation (called from daemon/rpc.py's GetImageStream/
    # GetTagStream handlers) ------------------------------------------------

    def get_or_create_image_endpoint(self, camera: str, *, deskewed: bool = False) -> StreamEndpoint:
        """Bind (or reuse) the listener for ``(camera, deskewed)`` and
        return where a client connects. Never touches ``self._service``
        -- see the module docstring's "No eager camera/calibration
        validation"."""
        key = (camera, deskewed)
        with self._lock:
            listener = self._image_listeners.get(key)
            if listener is None:
                product = "deskewed" if deskewed else "raw"
                listener = _Listener(
                    f"images-{product}-{camera}",
                    self._socket_dir,
                    subscribe=lambda: self._subscribe_images(camera, deskewed),
                    enable_unix=self._enable_unix,
                    enable_tcp=self._enable_tcp,
                    error_encoder=self._error_encoder,
                )
                self._image_listeners[key] = listener
        return listener.endpoint

    def get_or_create_tag_endpoint(self, camera: str) -> StreamEndpoint:
        """Bind (or reuse) the listener for *camera*'s tag stream and
        return where a client connects."""
        with self._lock:
            listener = self._tag_listeners.get(camera)
            if listener is None:
                listener = _Listener(
                    f"tags-{camera}",
                    self._socket_dir,
                    subscribe=lambda: self._subscribe_tags(camera),
                    enable_unix=self._enable_unix,
                    enable_tcp=self._enable_tcp,
                    error_encoder=self._error_encoder,
                )
                self._tag_listeners[camera] = listener
        return listener.endpoint

    # -- per-connection subscriptions (called from a _Listener's own
    # sender thread, once per accepted connection) --------------------------

    def _subscribe_images(self, camera: str, deskewed: bool) -> _Subscription:
        stream = self._service.stream_images(camera, deskewed=deskewed)
        encoder = self._image_encoder

        def _iterate() -> Iterator[bytes]:
            for frame in stream:
                yield encoder(frame)

        return _iterate(), stream.close

    def _subscribe_tags(self, camera: str) -> _Subscription:
        stream = self._service.stream_tags(camera)
        encoder = self._tag_encoder

        def _iterate() -> Iterator[bytes]:
            for frame in stream:
                yield encoder(frame)

        return _iterate(), stream.close

    # -- shutdown ----------------------------------------------------------

    def stop(self) -> None:
        """Stop every listener this manager ever created -- closes
        every bound socket and every accepted connection, unsubscribes
        every live subscription, and joins every accept/sender thread
        (bounded). Called from ``daemon/__main__.py``'s
        ``DaemonLifecycle`` shutdown hook, alongside
        ``DaemonService.close`` (sprint.md Migration Concerns: "Clean
        daemon shutdown", extended through this transport layer).
        Idempotent: a second call finds nothing left to stop.
        """
        with self._lock:
            listeners = list(self._image_listeners.values()) + list(self._tag_listeners.values())
            self._image_listeners.clear()
            self._tag_listeners.clear()
        for listener in listeners:
            listener.stop()
