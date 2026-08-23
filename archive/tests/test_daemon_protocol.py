"""Tests for aprilcam.daemon.protocol — msgpack frame schema and socket IO."""

from __future__ import annotations

import socket
import threading

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.daemon.protocol import (
    SCHEMA_VERSION,
    FrameMessage,
    decode_frame,
    encode_frame,
    read_frame,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_full_message() -> FrameMessage:
    """Return a FrameMessage with every field populated."""
    return FrameMessage(
        schema=SCHEMA_VERSION,
        frame_id=42,
        ts_mono_ns=1_000_000_000,
        ts_wall_ms=1_700_000_000_000,
        frame_jpeg=b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9",
        frame_w=1280,
        frame_h=720,
        tags=[
            {"id": 1, "cx": 320.5, "cy": 240.0, "yaw": 0.12},
            {"id": 7, "cx": 640.0, "cy": 360.0, "yaw": -1.57},
        ],
        homography=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        playfield_corners=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        paths_file="/tmp/paths.json",
        fps=29.97,
    )


# ── Round-trip tests ──────────────────────────────────────────────────────────


def test_round_trip_full_message():
    """encode then decode returns an identical FrameMessage."""
    msg = make_full_message()
    encoded = encode_frame(msg)
    decoded = decode_frame(encoded)
    assert decoded == msg


def test_round_trip_none_homography_and_empty_tags():
    """homography=None and empty tags list survive the round-trip."""
    msg = FrameMessage(
        schema=SCHEMA_VERSION,
        frame_id=0,
        ts_mono_ns=0,
        ts_wall_ms=0,
        frame_jpeg=b"",
        frame_w=640,
        frame_h=480,
        tags=[],
        homography=None,
        playfield_corners=[],
        paths_file="",
        fps=0.0,
    )
    encoded = encode_frame(msg)
    decoded = decode_frame(encoded)
    assert decoded == msg
    assert decoded.homography is None
    assert decoded.tags == []


def test_encode_has_length_prefix():
    """Encoded bytes start with a 4-byte big-endian length matching the payload."""
    import struct

    msg = make_full_message()
    encoded = encode_frame(msg)
    (length,) = struct.unpack(">I", encoded[:4])
    assert len(encoded) == 4 + length


# ── read_frame tests ──────────────────────────────────────────────────────────


def test_read_frame_single_send():
    """read_frame decodes a message sent in one send() call."""
    msg = make_full_message()
    encoded = encode_frame(msg)

    reader, writer = socket.socketpair()
    try:
        writer.sendall(encoded)
        writer.close()
        result = read_frame(reader)
    finally:
        reader.close()

    assert result == msg


def test_read_frame_split_send():
    """read_frame handles data arriving in two separate recv chunks."""
    msg = make_full_message()
    encoded = encode_frame(msg)

    # Split at an arbitrary point in the middle of the payload
    split = len(encoded) // 2

    reader, writer = socket.socketpair()
    try:
        # Send the first half, then the second half
        def _send_split():
            writer.sendall(encoded[:split])
            writer.sendall(encoded[split:])
            writer.close()

        t = threading.Thread(target=_send_split, daemon=True)
        t.start()
        result = read_frame(reader)
        t.join(timeout=2)
    finally:
        reader.close()

    assert result == msg


def test_read_frame_raises_on_closed_socket():
    """read_frame raises ConnectionError when the socket closes immediately."""
    reader, writer = socket.socketpair()
    writer.close()  # close before sending anything

    with pytest.raises(ConnectionError):
        read_frame(reader)

    reader.close()
