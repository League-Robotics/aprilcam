"""Tests for ``aprilcam.framing`` (sprint 027 ticket 001).

A real local socket pair (``socket.socketpair()``) exercises the actual
wire format end to end -- no daemon, no client, no protobuf: just raw
bytes across a real socket, which is exactly this module's whole scope.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from aprilcam.framing import pack_frame, read_frame


def test_pack_frame_prefixes_four_byte_big_endian_length() -> None:
    payload = b"hello"
    framed = pack_frame(payload)

    assert framed == struct.pack(">I", len(payload)) + payload


def test_pack_frame_empty_payload() -> None:
    assert pack_frame(b"") == struct.pack(">I", 0)


@pytest.mark.parametrize("size", [0, 1, 4, 4096, 70_000])
def test_round_trip_over_real_socket_pair(size: int) -> None:
    """Write on one end, read on the other, byte-exact -- including a
    payload (70 KB) larger than both a single ``recv`` call and a
    socketpair's own kernel send buffer, so ``read_frame`` must loop
    rather than assume one ``recv`` is enough, *and* the write has to
    happen concurrently with the read rather than before it -- a
    70 KB ``sendall`` blocks until something drains the socket, exactly
    the situation a real producer/consumer pair is always in."""
    payload = bytes((i % 256 for i in range(size)))
    sender, receiver = socket.socketpair()
    try:
        sender_thread = threading.Thread(target=lambda: sender.sendall(pack_frame(payload)))
        sender_thread.start()
        result = read_frame(receiver)
        sender_thread.join(timeout=5.0)
        assert not sender_thread.is_alive()
    finally:
        sender.close()
        receiver.close()

    assert result == payload


def test_read_frame_multiple_messages_in_sequence() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(pack_frame(b"first"))
        sender.sendall(pack_frame(b"second"))

        assert read_frame(receiver) == b"first"
        assert read_frame(receiver) == b"second"
    finally:
        sender.close()
        receiver.close()


def test_read_frame_raises_eof_on_connection_closed_before_length_prefix() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.close()  # closed before anything at all was sent
        with pytest.raises(EOFError):
            read_frame(receiver)
    finally:
        receiver.close()


def test_read_frame_raises_eof_on_short_length_prefix() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.send(b"\x00\x00")  # 2 of the 4 length-prefix bytes
        sender.close()
        with pytest.raises(EOFError):
            read_frame(receiver)
    finally:
        receiver.close()


def test_read_frame_raises_eof_on_connection_closed_mid_payload() -> None:
    """The length prefix announces more bytes than actually arrive --
    the daemon's own definition of a short read/closed-mid-message."""
    sender, receiver = socket.socketpair()
    try:
        sender.send(struct.pack(">I", 100) + b"only ten b")  # 10 of 100
        sender.close()
        with pytest.raises(EOFError):
            read_frame(receiver)
    finally:
        receiver.close()
