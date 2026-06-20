"""aprilcam.client.stream — ImageStreamConsumer and TagStreamConsumer.

Each consumer owns a raw TCP or Unix socket, reads length-prefixed
protobuf messages, and converts them to Pydantic models.

Wire framing: 4-byte big-endian uint32 length prefix + protobuf payload.
"""

from __future__ import annotations

import io
import socket
import struct
from typing import Iterator, Union

import numpy as np
from PIL import Image

from aprilcam.client.models import ImageFrame, StreamEndpoint, TagFrame
from aprilcam.proto import aprilcam_pb2


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, raising EOFError on short read."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(
                f"Connection closed after {len(buf)} of {n} expected bytes"
            )
        buf.extend(chunk)
    return bytes(buf)


def _read_length_prefixed(sock: socket.socket) -> bytes:
    """Read one length-prefixed protobuf message from *sock*."""
    header = _recv_exactly(sock, 4)
    (length,) = struct.unpack(">I", header)
    return _recv_exactly(sock, length)


# ---------------------------------------------------------------------------
# ImageStreamConsumer
# ---------------------------------------------------------------------------


def _host_is_local(host: str) -> bool:
    """True when *host* refers to this machine (so a unix socket is usable)."""
    return host in ("", "localhost", "127.0.0.1", "::1")


class ImageStreamConsumer:
    """Reads length-prefixed protobuf ``ImageFrame`` messages from a stream socket.

    Prefer Unix socket when ``endpoint.socket_path`` is set; fall back to TCP.

    Usage::

        consumer = ImageStreamConsumer(endpoint, cam_name="cam0")
        consumer.connect()
        for frame in consumer:          # numpy BGR array
            process(frame)
        consumer.close()
    """

    def __init__(
        self,
        endpoint: StreamEndpoint,
        *,
        cam_name: str = "",
        host: str = "localhost",
    ) -> None:
        """
        Args:
            endpoint: Stream endpoint returned by the daemon (socket_path or
                tcp_port).
            cam_name: Camera name for logging / model construction.
            host: Hostname or IP of the daemon.  Used when connecting via TCP
                (``endpoint.tcp_port`` is set).  Defaults to ``"localhost"``
                for backward compatibility with Unix-socket-only setups.
                Pass the resolved daemon host (``dc._host``) when the
                ``DaemonControl`` is TCP-connected so a remote Mac/Pi client
                reaches the correct machine.
        """
        self._endpoint = endpoint
        self._cam_name = cam_name
        self._host = host
        self._sock: socket.socket | None = None

    def connect(self) -> "ImageStreamConsumer":
        """Open the stream socket.  Idempotent."""
        if self._sock is not None:
            return self
        # Prefer the unix socket only when the daemon is local; a remote daemon's
        # unix path is on *its* filesystem and unreachable, so use TCP there.
        if self._endpoint.socket_path and _host_is_local(self._host):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self._endpoint.socket_path)
        elif self._endpoint.tcp_port:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self._host, self._endpoint.tcp_port))
        elif self._endpoint.socket_path:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self._endpoint.socket_path)
        else:
            raise ValueError(
                "StreamEndpoint has neither socket_path nor tcp_port"
            )
        self._sock = sock
        return self

    def close(self) -> None:
        """Close the stream socket."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_raw(self) -> tuple[int, bytes]:
        """Read one frame and return ``(frame_id, jpeg_bytes)``."""
        from aprilcam.proto import aprilcam_pb2

        if self._sock is None:
            raise RuntimeError("ImageStreamConsumer is not connected")

        data = _read_length_prefixed(self._sock)
        msg = aprilcam_pb2.ImageFrame()
        msg.ParseFromString(data)
        return int(msg.frame_id), bytes(msg.jpeg)

    def read(self) -> np.ndarray:
        """Read one frame and return an RGB ``np.ndarray``.

        Uses Pillow for JPEG decoding so that no OpenCV dependency is required
        in the base (client-only) install.  The returned array is RGB, not BGR.
        """
        _, jpeg = self.read_raw()
        try:
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception as exc:
            raise RuntimeError(f"Failed to decode JPEG frame from stream: {exc}") from exc
        return np.asarray(img)

    def read_image_frame(self) -> ImageFrame:
        """Read one frame and return a full ``ImageFrame`` Pydantic model."""
        from aprilcam.proto import aprilcam_pb2

        if self._sock is None:
            raise RuntimeError("ImageStreamConsumer is not connected")

        data = _read_length_prefixed(self._sock)
        msg = aprilcam_pb2.ImageFrame()
        msg.ParseFromString(data)
        return ImageFrame.from_proto(msg, cam_name=self._cam_name)

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[np.ndarray]:
        """Yield RGB frames (via :meth:`read`) until the connection closes."""
        try:
            while True:
                try:
                    yield self.read()
                except EOFError:
                    break
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ImageStreamConsumer":
        return self.connect()

    def __exit__(self, *_) -> None:
        self.close()


# ---------------------------------------------------------------------------
# TagStreamConsumer
# ---------------------------------------------------------------------------


class TagStreamConsumer:
    """Reads length-prefixed protobuf ``StreamMessage`` messages from a stream socket.

    Each message is a ``StreamMessage`` oneof that wraps either a ``TagFrame``
    or an ``OverlayFrame``.  ``read()`` returns the appropriate Python object.

    Prefer Unix socket when ``endpoint.socket_path`` is set; fall back to TCP.

    Usage::

        consumer = TagStreamConsumer(endpoint)
        consumer.connect()
        for msg in consumer:            # TagFrame or aprilcam_pb2.OverlayFrame
            process(msg)
        consumer.close()
    """

    def __init__(self, endpoint: StreamEndpoint, *, host: str = "localhost") -> None:
        """
        Args:
            endpoint: Stream endpoint returned by the daemon (socket_path or
                tcp_port).
            host: Hostname or IP of the daemon.  Used when connecting via TCP.
                Defaults to ``"localhost"`` for backward compatibility.
                Pass the resolved daemon host (``dc._host``) when the
                ``DaemonControl`` is TCP-connected.
        """
        self._endpoint = endpoint
        self._host = host
        self._sock: socket.socket | None = None

    def connect(self) -> "TagStreamConsumer":
        """Open the stream socket.  Idempotent."""
        if self._sock is not None:
            return self
        # Prefer the unix socket only when the daemon is local; a remote daemon's
        # unix path is on *its* filesystem and unreachable, so use TCP there.
        if self._endpoint.socket_path and _host_is_local(self._host):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self._endpoint.socket_path)
        elif self._endpoint.tcp_port:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self._host, self._endpoint.tcp_port))
        elif self._endpoint.socket_path:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self._endpoint.socket_path)
        else:
            raise ValueError(
                "StreamEndpoint has neither socket_path nor tcp_port"
            )
        self._sock = sock
        return self

    def close(self) -> None:
        """Close the stream socket."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(self) -> Union[TagFrame, aprilcam_pb2.OverlayFrame]:
        """Read one message and return a ``TagFrame`` or ``OverlayFrame``.

        The wire format is a length-prefixed ``StreamMessage`` protobuf.
        The ``payload`` oneof is inspected and the appropriate object is
        returned:

        * ``tag_frame`` field → :class:`~aprilcam.client.models.TagFrame`
        * ``overlay`` field   → :class:`aprilcam_pb2.OverlayFrame`
        """
        if self._sock is None:
            raise RuntimeError("TagStreamConsumer is not connected")

        data = _read_length_prefixed(self._sock)
        stream_msg = aprilcam_pb2.StreamMessage()
        stream_msg.ParseFromString(data)
        if stream_msg.HasField("tag_frame"):
            return TagFrame.from_proto(stream_msg.tag_frame)
        elif stream_msg.HasField("overlay"):
            return stream_msg.overlay
        else:
            raise ValueError("StreamMessage has no known payload field")

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[Union[TagFrame, aprilcam_pb2.OverlayFrame]]:
        """Yield TagFrame objects until the connection closes."""
        try:
            while True:
                try:
                    yield self.read()
                except EOFError:
                    break
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "TagStreamConsumer":
        return self.connect()

    def __exit__(self, *_) -> None:
        self.close()
