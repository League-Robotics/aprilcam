"""Tests for ``aprilcam.daemon.rpc``'s ``build_server`` TCP bind
(ticket 028-003).

Fully headless except for real socket binds: a bare
``aprilcam.daemon.interfaces.Daemon()`` stands in for a real
``DaemonService`` since these tests only exercise the unix+TCP bind
machinery in ``build_server``/``_GrpcListener``, never dispatch an RPC
through ``_Servicer`` -- see ``test_service.py`` for real RPC
round-trip coverage over the unix socket.

Unix socket paths use a short top-level ``/tmp`` directory (not
pytest's deeply nested ``tmp_path``) to stay under the ~103-character
OS limit for unix domain socket paths -- matching ``test_service.py``'s
``_bind_stub`` and ``test_main.py``'s ``short_tmp_dir`` convention.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

import grpc
import pytest

from aprilcam.daemon.interfaces import Daemon
from aprilcam.daemon.rpc import build_server


@pytest.fixture
def short_tmp_dir():
    d = Path(tempfile.mkdtemp(prefix="ac-rpc-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_server_without_tcp_port_binds_unix_socket_only(short_tmp_dir):
    """AC: ``tcp_port=None`` (the default) preserves today's
    unix-socket-only behavior for every existing caller that does not
    pass it."""
    socket_path = short_tmp_dir / "control.sock"
    listener = build_server(Daemon(), socket_path)

    bound_port = listener.start()
    try:
        assert bound_port is None

        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
        finally:
            channel.close()
    finally:
        listener.stop(1.0)


def test_build_server_with_tcp_port_zero_binds_an_ephemeral_port_reachable_by_a_real_client(
    short_tmp_dir,
):
    """AC: a TCP bind succeeds and the bound port is observable through
    ``start()``'s return value; a real TCP client can connect."""
    socket_path = short_tmp_dir / "control.sock"
    listener = build_server(Daemon(), socket_path, tcp_port=0)

    bound_port = listener.start()
    try:
        assert isinstance(bound_port, int)
        assert bound_port != 0

        channel = grpc.insecure_channel(f"127.0.0.1:{bound_port}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
        finally:
            channel.close()
    finally:
        listener.stop(1.0)


def test_build_server_still_binds_the_unix_socket_when_tcp_is_also_requested(short_tmp_dir):
    """AC: one gRPC server, two transports -- the existing unix bind is
    unaffected by also requesting TCP."""
    socket_path = short_tmp_dir / "control.sock"
    listener = build_server(Daemon(), socket_path, tcp_port=0)

    listener.start()
    try:
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
        finally:
            channel.close()
    finally:
        listener.stop(1.0)


def test_build_server_tcp_bind_failure_is_non_fatal_and_reports_no_port(short_tmp_dir, caplog):
    """AC: a genuine TCP bind failure (the same fixed port already
    bound by a first, still-live server in this process) is caught,
    logged at WARNING naming the port, and never raised -- build_server
    still returns a fully working (unix-socket) listener, and that
    listener's start() reports tcp_port=None rather than raising.

    Two independent build_server()-constructed servers deliberately
    attempting the exact same fixed TCP port is exactly the "already in
    use" scenario the ticket describes as the expected real-world case
    (e.g. a stray second daemon process). build_server() disables
    grpc's default SO_REUSEPORT specifically so this collision is
    observable as a failure here rather than both servers silently
    succeeding and the kernel load-balancing between them -- see
    build_server's own docstring.
    """
    first_socket = short_tmp_dir / "first.sock"
    first_listener = build_server(Daemon(), first_socket, tcp_port=0)
    first_port = first_listener.start()
    assert isinstance(first_port, int) and first_port != 0

    try:
        second_socket = short_tmp_dir / "second.sock"
        with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.rpc"):
            second_listener = build_server(Daemon(), second_socket, tcp_port=first_port)
        second_bound = second_listener.start()
        try:
            assert second_bound is None

            # The unix socket still works even though TCP did not bind.
            channel = grpc.insecure_channel(f"unix:{second_socket}")
            try:
                grpc.channel_ready_future(channel).result(timeout=5)
            finally:
                channel.close()
        finally:
            second_listener.stop(1.0)

        assert any(str(first_port) in record.message for record in caplog.records)
    finally:
        first_listener.stop(1.0)
