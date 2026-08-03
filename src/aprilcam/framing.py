"""The wire framing shared by every bulk-stream socket.

``[uint32 BE length][payload]`` (``docs/design/design.md`` §5,
``docs/design/specification.md`` §5): every stream socket -- raw image,
deskewed image, tag stream alike -- prefixes each protobuf-serialized
message with its length as a 4-byte big-endian unsigned int before
writing it, and a reader consumes exactly that many bytes for one
message. This module knows nothing about protobuf, cameras, or daemons
at all -- it moves opaque length-prefixed byte blobs, full stop; the
*payload* is somebody else's concern (``daemon/stream_transport.py``,
ticket 005, produces protobuf-serialized ``ImageFrame``/``TagFrame``
bytes and hands them here already-serialized).

**Sits at the top level, not under ``daemon/`` or ``client/``, on
purpose.** Both a future ``daemon/stream_transport.py`` (ticket 005, the
write side -- a producer thread frames a message once and hands the
result to a per-connection sender) and ``client/streams.py`` (ticket 006,
the read side) need this, and neither of those packages imports the
other (``daemon/`` and ``client/`` are siblings, not a chain) -- putting
it under either one would create a cross-package dependency this file
doesn't need. This mirrors how ``types.py``/``errors.py``/``guides.py``
already sit at this same top level for the identical reason: plain,
dependency-free code both sides of the daemon/client boundary import.

Adapted from -- not copied wholesale from -- v1's
``archive/src/aprilcam/client/stream.py`` (``_recv_exactly``/
``_read_length_prefixed``) and ``archive/src/aprilcam/daemon/stream.py``
(``_LENGTH_FMT``/``_frame_bytes``): same framing, same ``EOFError`` on a
short read (so a client's iteration loop can already tell "the daemon
closed this stream" apart from a genuine socket error, exactly as v1's
``ImageStreamConsumer.__iter__``/``TagStreamConsumer.__iter__`` already
did), but v1 mixed this with proto message construction
(``_frame_bytes`` built a ``StreamMessage`` wrapper) and camera-specific
read helpers in the same module as the framing itself. This module is
deliberately narrower: it operates on raw ``bytes`` only, never a proto
type.
"""

from __future__ import annotations

import socket
import struct

#: 4-byte big-endian unsigned int -- the length prefix's own wire shape.
_LENGTH_STRUCT = struct.Struct(">I")


def pack_frame(payload: bytes) -> bytes:
    """Prefix *payload* with its 4-byte big-endian length.

    Pure -- does not touch a socket. A producer (ticket 005's
    per-connection sender) calls this once per publish to get the exact
    bytes it later hands to ``socket.sendall``; framing and the actual
    write are two different responsibilities; only the encoding lives
    here, matching this module's own "raw bytes only" scope.
    """
    return _LENGTH_STRUCT.pack(len(payload)) + payload


def read_frame(sock: socket.socket) -> bytes:
    """Read exactly one length-prefixed message from *sock* and return
    its payload (the 4-byte length prefix itself is consumed, not part
    of the return value).

    Raises:
        EOFError: the connection closed before a complete message --
            either the 4-byte length prefix or the payload it
            announced -- was received. A short read is never silently
            returned as a truncated result; the caller gets a clear,
            specific signal it can catch to end iteration cleanly (a
            stream closing is not a bug), the same distinction v1's
            stream consumers already relied on ``EOFError`` for.
    """
    header = _recv_exactly(sock, _LENGTH_STRUCT.size)
    (length,) = _LENGTH_STRUCT.unpack(header)
    return _recv_exactly(sock, length)


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, raising ``EOFError`` on a
    short read (the connection closed with fewer than *n* bytes sent).
    ``socket.recv`` itself makes no such guarantee -- it may return
    anywhere from 1 to *n* bytes per call -- so this loops until *n* is
    reached or the connection signals EOF (an empty ``recv`` result).
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(
                f"connection closed after {len(buf)} of {n} expected bytes"
            )
        buf.extend(chunk)
    return bytes(buf)
