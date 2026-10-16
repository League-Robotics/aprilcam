"""``aprilcam.client.streams`` -- ``SocketImageStream``/``SocketTagStream``
(ticket 027-006): the client-side implementations of
``daemon/interfaces.py``'s ``ImageStream``/``TagStream`` base classes,
completing the wire path ``daemon/stream_transport.py`` (ticket 027-005)
opened up -- a raw, length-prefixed socket carrying serialized
``ImageFrame``/``TagFrame`` protobuf messages, framed with
``aprilcam.framing`` on both ends (the identical reader ticket 005's own
tests already exercised against a real sender, ``[uint32 BE
length][protobuf]``, ``docs/design/design.md`` §5).

**Connection mechanics, not business logic.** ``GrpcDaemon.
stream_images``/``stream_tags`` (``client/daemon_client.py``) call the
``GetImageStream``/``GetTagStream`` RPCs to obtain a ``StreamEndpoint``
(socket_path and/or tcp_port -- see ``daemon/rpc.py``'s
``_stream_endpoint_to_proto``), then hand the raw response sub-message
straight to :meth:`SocketImageStream.connect`/
:meth:`SocketTagStream.connect` here -- the same one-hop
"response-field-to-a-converter" shape every other ``GrpcDaemon`` method
already uses for its own response sub-message (e.g. ``get_playfield``'s
``playfield_from_proto(response.playfield)``), just landing here instead
of ``client/models.py`` because opening a socket, not building an
``aprilcam.types`` dataclass, is what happens next. This module owns
that connection plus turning framed bytes back into
``aprilcam.types.ImageFrame``/``TagFrame`` objects via
``client/models.py``'s existing ``image_frame_from_proto``/
``tag_frame_from_proto`` -- the established proto boundary
(``docs/design/design.md`` §6). It never invents a second decode path.

**Unix preferred, TCP fallback -- v1's ``_host_is_local`` precedent**
(``archive/src/aprilcam/client/stream.py``'s ``ImageStreamConsumer``/
``TagStreamConsumer``). A daemon offers a unix socket only when it and
the client share a filesystem; *host* (the same ``target`` string
``GrpcDaemon`` already carries for its own ``ConnectionFailed`` error
messages -- ``"localhost"`` for a local daemon, a ``-d`` name for a
remote one) decides whether the unix path is even reachable. A remote
daemon's ``tcp_port`` is used instead.

**Stream end vs. a genuine failure stay distinguishable.**
``aprilcam.framing.read_frame`` raises ``EOFError`` on a clean close or
short read -- this module's ``__iter__`` catches exactly that and ends
iteration (``StopIteration``, the normal way a Python generator says "no
more items"), never surfacing a clean daemon-side disconnect as a
crash. Anything else -- a genuine ``OSError``, or ``socket.timeout`` on
a socket a caller has set one on (mirroring v1's ``set_timeout``, used
to bound the wait for a first frame rather than hanging forever when a
pipeline produces nothing) -- propagates uncaught: it is a real failure,
not "the stream ended".

**A failed subscribe arrives as an error frame, not a silent end of
stream (sprint 043 ticket 003).** When the daemon cannot satisfy the
subscribe (uncalibrated deskewed request, unknown/absent camera) it
sends one *error frame* then closes. The frame is told apart from data
frames by its first payload byte: ``framing.ERROR_FRAME_MARKER``
(``0x00``), which can never start a valid protobuf message, followed by
a serialized ``StreamError`` (kind, message, subject, available).
Both ``__iter__`` methods check every payload with
``framing.is_error_payload`` and raise the matching
``aprilcam.errors`` class -- ``NotCalibrated``, ``UnknownCamera``,
``CameraNotPresent``, else a generic ``AprilCamError`` carrying the
daemon's message -- instead of ending iteration cleanly. A close with no
error frame is still a clean end of stream.

**Closing unsubscribes -- daemon-side, not by an RPC.** ``.close()``
closes the client-side socket only. Per ``daemon/interfaces.py``'s
``ImageStream``/``TagStream`` docstrings, unsubscription is a
**daemon-visible event**: the daemon's own accept-loop sender thread
(``daemon/stream_transport.py``'s ``_Listener._sender_loop``) notices
the disconnect on its next write attempt (at worst, the next publish
tick) and unsubscribes/idle-gates from there. There is no explicit
unsubscribe RPC, and this module sends none.

cv2-free (``tests/test_no_cv2_in_client_modules.py``'s standing gate) --
this module touches only sockets, framing, and proto messages; no
business logic lives here.
"""

from __future__ import annotations

import socket
from typing import Iterator, Optional, Tuple

from aprilcam import framing
from aprilcam.client.models import image_frame_from_proto, tag_frame_from_proto
from aprilcam.daemon.interfaces import ImageStream, TagStream
from aprilcam.errors import (
    AprilCamError,
    CameraNotPresent,
    NotCalibrated,
    UnknownCamera,
)
from aprilcam.proto import aprilcam_v2_pb2
from aprilcam.types import ImageFrame, TagFrame

__all__ = ["SocketImageStream", "SocketTagStream"]


def _error_from_payload(payload: bytes) -> AprilCamError:
    """Decode an error frame's payload (``ERROR_FRAME_MARKER`` +
    serialized ``StreamError``) into the matching typed error."""
    msg = aprilcam_v2_pb2.StreamError.FromString(payload[len(framing.ERROR_FRAME_MARKER):])
    if msg.kind == "NotCalibrated":
        return NotCalibrated(msg.message)
    if msg.kind == "UnknownCamera":
        return UnknownCamera(msg.subject, tuple(msg.available))
    if msg.kind == "CameraNotPresent":
        return CameraNotPresent(msg.subject)
    return AprilCamError(msg.message or msg.kind or "stream failed")


def _host_is_local(host: str) -> bool:
    """True when *host* names this machine, so a unix socket endpoint is
    reachable -- the same set v1's ``ImageStreamConsumer``/
    ``TagStreamConsumer`` used (``archive/src/aprilcam/client/
    stream.py``)."""
    return host in ("", "localhost", "127.0.0.1", "::1")


def _resolve_tcp_endpoint(host: str, port: int) -> Tuple[int, str]:
    """The ``(family, address)`` a raw stream socket should actually
    dial for (*host*, *port*) -- the same IPv4-preferred resolution
    ``client/discovery.py``'s gRPC control channel uses (ticket
    040-007), applied here so this module's own TCP connect does not
    keep the identical bug: a plain ``socket.socket(AF_INET, ...)``
    cannot resolve an IPv6-only/``.local`` host at all (family-mismatch
    ``gaierror``, not the 5s hang the control channel saw, but broken
    either way).

    Imports ``client.discovery`` lazily, inside this function, rather
    than at module scope: ``daemon_client.py`` imports *this* module
    (``streams.py``) for ``SocketImageStream``/``SocketTagStream``, and
    ``discovery.py`` imports ``daemon_client.py`` for ``GrpcDaemon`` --
    a top-level ``from aprilcam.client.discovery import ...`` here would
    close that into an import cycle. Deferring the import to call time
    (long after both modules have finished loading) avoids it without
    restructuring either module's existing dependency direction.

    *host* may itself already carry a ``:port`` suffix (it is the same
    ``target`` string ``GrpcDaemon`` stores for its own error messages,
    e.g. ``"vidar.local:5280"`` for a non-default control-plane port) --
    :func:`~aprilcam.client.discovery.split_host_port` strips that
    before resolution so a literal colon-and-port never reaches
    ``getaddrinfo`` as part of the hostname.
    """
    from aprilcam.client.discovery import resolve_ipv4_preferred, split_host_port

    name, _control_port = split_host_port(host)
    resolved = resolve_ipv4_preferred(name, port)
    if resolved is not None:
        return resolved
    return socket.AF_INET, name


def _connect(endpoint: aprilcam_v2_pb2.StreamEndpoint, *, host: str) -> socket.socket:
    """Open a socket to *endpoint*, unix preferred when *host* is local,
    TCP otherwise (module docstring). Raises ``ValueError`` if
    *endpoint* offers neither -- a daemon that failed to bind either
    transport leaves the corresponding field unset rather than raising
    (``daemon/stream_transport.py``'s ``_Listener.__init__`` logs and
    moves on), so this is the first point a client can detect it."""
    if endpoint.HasField("socket_path") and _host_is_local(host):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(endpoint.socket_path)
        return sock
    if endpoint.HasField("tcp_port"):
        family, address = _resolve_tcp_endpoint(host, endpoint.tcp_port)
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.connect((address, endpoint.tcp_port))
        return sock
    if endpoint.HasField("socket_path"):
        # A unix path exists but *host* was not recognised as local --
        # should not happen in practice (a daemon only ever sets
        # socket_path when it itself bound one, on its own machine), but
        # one last honest attempt beats refusing outright when a socket
        # path is all that was offered.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(endpoint.socket_path)
        return sock
    raise ValueError("stream endpoint offers neither a unix socket path nor a tcp port")


class SocketImageStream(ImageStream):
    """A real socket subscription to one camera's image stream.

    Derives from ``daemon.interfaces.ImageStream`` (this codebase's
    zero-``Protocol`` rule) -- ``isinstance(stream, ImageStream)`` is a
    true, mechanical fact here, not a structural guess.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock: Optional[socket.socket] = sock

    @classmethod
    def connect(cls, endpoint: aprilcam_v2_pb2.StreamEndpoint, *, host: str) -> "SocketImageStream":
        """Open a socket to *endpoint* (module docstring) and wrap it.
        The one construction path ``GrpcDaemon.stream_images`` uses;
        equally usable directly by anything else already holding an
        endpoint -- there is no other public constructor a caller
        needs."""
        return cls(_connect(endpoint, host=host))

    def __iter__(self) -> Iterator[ImageFrame]:
        """Yield decoded frames until the connection closes or
        ``.close()`` is called -- ``for frame in stream: ...`` is the
        intended idiom.

        **Gotcha, inherited from v1's identical shape**: each call to
        ``__iter__`` returns a *new* generator wrapping this same
        socket, and that generator's own ``finally`` closes the socket
        -- including when the generator itself is merely garbage
        collected, not just when it runs to completion. A ``for``
        loop's implicit iterator (even one ended early with ``break``)
        keeps the generator alive for exactly as long as intended, so
        this is transparent there -- but a throwaway
        ``next(iter(stream))``, with no reference kept to the
        intermediate iterator, closes the socket immediately after that
        one frame (CPython drops the anonymous generator's refcount to
        zero as soon as the expression finishes, finalizing it right
        away). Keep the iterator (``it = iter(stream)``) if you intend
        to call ``next(it)`` more than once outside a ``for`` loop.
        """
        try:
            while True:
                sock = self._sock
                if sock is None:
                    return
                try:
                    payload = framing.read_frame(sock)
                except EOFError:
                    return
                if framing.is_error_payload(payload):
                    raise _error_from_payload(payload)
                msg = aprilcam_v2_pb2.GetFrameResponse.FromString(payload)
                yield image_frame_from_proto(msg)
        finally:
            self.close()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class SocketTagStream(TagStream):
    """A real socket subscription to one camera's tag stream. Same
    connect/read/close mechanics as :class:`SocketImageStream`, decoding
    ``aprilcam_v2_pb2.TagFrame`` (the identical shape
    ``GetTagsResponse.tags`` already uses) via ``client/models.py``'s
    ``tag_frame_from_proto`` instead of ``GetFrameResponse``.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock: Optional[socket.socket] = sock

    @classmethod
    def connect(cls, endpoint: aprilcam_v2_pb2.StreamEndpoint, *, host: str) -> "SocketTagStream":
        return cls(_connect(endpoint, host=host))

    def __iter__(self) -> Iterator[TagFrame]:
        """Yield decoded tag frames -- same ``for frame in stream: ...``
        idiom and the same throwaway-``next(iter(stream))`` gotcha as
        :meth:`SocketImageStream.__iter__` (see its docstring)."""
        try:
            while True:
                sock = self._sock
                if sock is None:
                    return
                try:
                    payload = framing.read_frame(sock)
                except EOFError:
                    return
                if framing.is_error_payload(payload):
                    raise _error_from_payload(payload)
                msg = aprilcam_v2_pb2.TagFrame.FromString(payload)
                yield tag_frame_from_proto(msg)
        finally:
            self.close()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
