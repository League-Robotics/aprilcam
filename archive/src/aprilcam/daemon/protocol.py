"""
aprilcam.daemon.protocol — shared wire format for the AprilCam daemon.

Schema version 1 framing
-------------------------
Each message on the socket is a length-prefixed msgpack blob:

    [ uint32 big-endian length (4 bytes) ][ msgpack payload (length bytes) ]

The msgpack payload is a dict whose keys match the FrameMessage field names.
String keys are used (raw=False), so they decode as str on the receiving end.

``homography`` is None when the camera is uncalibrated; it round-trips as
msgpack nil.

This module has no runtime dependency beyond msgpack and the Python stdlib.
It does NOT import OpenCV, NumPy, or any other AprilCam domain module.
"""

from __future__ import annotations

import dataclasses
import socket
import struct
from typing import Optional

import msgpack

# ── Schema version ──────────────────────────────────────────────────────────

SCHEMA_VERSION: int = 1

# ── Message dataclass ────────────────────────────────────────────────────────


@dataclasses.dataclass
class FrameMessage:
    """One frame's worth of data broadcast by the daemon to all subscribers."""

    schema: int
    frame_id: int
    ts_mono_ns: int
    ts_wall_ms: int
    frame_jpeg: bytes
    frame_w: int
    frame_h: int
    tags: list[dict]
    homography: Optional[list[list[float]]]
    playfield_corners: list[list[float]]
    paths_file: str
    fps: float


# ── Codec ────────────────────────────────────────────────────────────────────

_LENGTH_FMT = ">I"  # 4-byte big-endian unsigned int
_LENGTH_SIZE = struct.calcsize(_LENGTH_FMT)


def encode_frame(msg: FrameMessage) -> bytes:
    """Encode a FrameMessage to a length-prefixed msgpack blob.

    Args:
        msg: The FrameMessage to encode.

    Returns:
        Bytes consisting of a 4-byte big-endian uint32 length prefix followed
        by the msgpack-packed dict representation of *msg*.
    """
    payload = msgpack.packb(dataclasses.asdict(msg), use_bin_type=True)
    prefix = struct.pack(_LENGTH_FMT, len(payload))
    return prefix + payload


def decode_frame(data: bytes) -> FrameMessage:
    """Decode a length-prefixed msgpack blob into a FrameMessage.

    Args:
        data: Bytes as returned by :func:`encode_frame` (prefix + payload).

    Returns:
        The reconstructed FrameMessage.
    """
    payload = data[_LENGTH_SIZE:]
    d = msgpack.unpackb(payload, raw=False)
    return FrameMessage(**d)


# ── Socket reader ────────────────────────────────────────────────────────────


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, handling partial reads.

    Raises:
        ConnectionError: If the socket is closed before *n* bytes arrive.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"Socket closed after {len(buf)} bytes (expected {n})"
            )
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> FrameMessage:
    """Read one FrameMessage from *sock*.

    Reads the 4-byte length prefix, then the exact payload, and decodes it.

    Args:
        sock: A connected socket.

    Returns:
        The decoded FrameMessage.

    Raises:
        ConnectionError: If the socket closes before the full message arrives.
    """
    prefix = _recv_exactly(sock, _LENGTH_SIZE)
    (length,) = struct.unpack(_LENGTH_FMT, prefix)
    payload = _recv_exactly(sock, length)
    return decode_frame(prefix + payload)
