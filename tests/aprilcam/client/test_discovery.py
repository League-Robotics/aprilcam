"""Tests for ``aprilcam.client.discovery`` (ticket 020-005; real mDNS
browse added by ticket 028-004).

Fully headless and local-only for every test but two, explicitly marked
below (ticket 028-004's own AC): every other test drives ``Discovery``
against a ``tmp_path``-derived ``socket_dir`` it never shares with a
real running daemon on the machine executing the suite, and against a
fake or explicitly-shrunk ``browse`` so it never depends on a real
multicast round trip either. "A daemon is running" is simulated by
genuinely holding an ``flock`` on a lock file in *this* test process on
a separate file descriptor from the one ``Discovery`` probes with --
``fcntl.flock`` locks are per open-file-description, not per-process, so
two independent ``open()`` calls on the same path really do contend,
exactly as ``tests/aprilcam/daemon/test_lifecycle.py`` documents for the
server-side equivalent. "Responds" is simulated with a bare
``grpc.server()`` bound to the unix socket (local) or to an
OS-assigned TCP port (browsed/remote) -- connectivity readiness does
not require the ``AprilCamV2`` service to be registered, so this needs
no camera/cv2 machinery at all.

The ``directories`` fixture below additionally monkeypatches this
module's ``_browse_zeroconf`` -- ``Discovery``'s ``browse`` parameter
resolves that name lazily (see ``Discovery.__init__``'s own docstring
for why), so this one shared fixture is what keeps every pre-028-004
test (none of which know or care that ``browse`` exists) from ever
touching a real multicast socket, without any of those nine tests
needing to change.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import socket
import tempfile
import time
from concurrent import futures
from pathlib import Path
from typing import Iterator, Tuple

import grpc
import pytest

from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.client.discovery import (
    Discovery,
    _BrowseResult,
    _browse_zeroconf,
    resolve_ipv4_preferred,
    short_host_name,
    split_host_port,
)
from aprilcam.client.interfaces import DaemonInfo
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.lifecycle import LOCK_FILENAME, SOCKET_FILENAME
from aprilcam.daemon.mdns import MdnsAdvertiser
from aprilcam.errors import ConnectionFailed, DiscoveryFailed


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests deeply enough to routinely exceed it (see
    ``tests/aprilcam/daemon/test_service.py``'s ``stub`` fixture for
    the same workaround)."""
    d = Path(tempfile.mkdtemp(prefix="ac-disc-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def directories(tmp_path: Path, short_socket_dir: Path, monkeypatch) -> Directories:
    """A ``Directories`` whose ``socket_dir`` is *short_socket_dir* --
    achieved by making the hard-coded system socket root
    (``/run/aprilcam``) always look unavailable, which forces
    resolution down to the ``XDG_RUNTIME_DIR`` env tier
    ``config/directories.py``'s module docstring calls out as
    monkeypatch-friendly for exactly this purpose.

    Also blanket-replaces ``discovery._browse_zeroconf`` (ticket
    028-004) with an empty-result stub -- see the module docstring's
    explanation of why this single shared fixture, not each test, is
    what keeps every pre-028-004 ``Discovery(directories=directories)``
    call (none of which pass ``browse=``) from ever opening a real
    multicast socket. A test that wants real mDNS behaviour passes its
    own ``browse=`` explicitly, which bypasses this default entirely.
    """
    monkeypatch.setattr("aprilcam.config.directories._writable_or_creatable", lambda path: False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_socket_dir))
    monkeypatch.setattr("aprilcam.client.discovery._browse_zeroconf", lambda: ())
    dirs = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    assert dirs.socket_dir == short_socket_dir / "aprilcam"
    return dirs


def _hold_lock(lock_path: Path) -> int:
    """Genuinely take the daemon lock, as if a daemon were running --
    returns the fd so the caller can release it. See the module
    docstring for why a second, independent ``open()`` in this same
    process still contends with this one."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _bind_bare_server(socket_path: Path) -> grpc.Server:
    """A ``grpc.Server`` bound to *socket_path* with no service
    registered -- connectivity becomes READY as soon as something is
    listening, regardless of which (if any) service answers, so this
    is sufficient to exercise "responds" without any daemon/camera
    machinery."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_insecure_port(f"unix:{socket_path}")
    server.start()
    return server


def _bind_bare_tcp_server() -> Tuple[grpc.Server, int]:
    """The TCP-target counterpart of :func:`_bind_bare_server`, for a
    browsed "remote" entry that must genuinely answer a channel-ready
    probe (ticket 028-004's "not merely 'it advertised so it must be
    up'" requirement) -- an OS-assigned loopback port, so tests never
    collide with each other or a real daemon's ``DEFAULT_DAEMON_PORT``."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, port


# ---------------------------------------------------------------------------
# find_all() -- AC3 (empty vs. one entry, reachable vs. not)
# ---------------------------------------------------------------------------


def test_find_all_returns_empty_tuple_when_no_daemon_running(directories: Directories):
    discovery = Discovery(directories=directories)

    assert discovery.find_all() == ()


def test_find_all_lists_a_held_but_unreachable_daemon(directories: Directories, monkeypatch):
    """A daemon that advertises (holds the lock) but does not answer is
    listed, not filtered out -- ``DaemonInfo.reachable`` says which."""
    monkeypatch.setattr(socket, "gethostname", lambda: "My-Machine.local")
    lock_fd = _hold_lock(directories.socket_dir / LOCK_FILENAME)
    try:
        discovery = Discovery(directories=directories)

        results = discovery.find_all()

        assert results == (
            DaemonInfo(host="localhost", port=0, is_local=True, reachable=False, name="my-machine"),
        )
    finally:
        _release_lock(lock_fd)


def test_find_all_lists_a_reachable_local_daemon(directories: Directories):
    lock_fd = _hold_lock(directories.socket_dir / LOCK_FILENAME)
    server = _bind_bare_server(directories.socket_dir / SOCKET_FILENAME)
    try:
        discovery = Discovery(directories=directories)

        results = discovery.find_all()

        assert len(results) == 1
        assert results[0].is_local is True
        assert results[0].reachable is True
    finally:
        server.stop(None)
        _release_lock(lock_fd)


def test_find_all_raises_discovery_failed_distinctly_from_empty(directories: Directories):
    """Discovery *failing* (e.g. permission denied probing socket_dir)
    must never look like "found nothing" (UC-13.S-5)."""

    def _broken_lock_holder(_path: Path):
        raise PermissionError("simulated: cannot read socket_dir")

    discovery = Discovery(directories=directories, lock_holder=_broken_lock_holder)

    with pytest.raises(DiscoveryFailed):
        discovery.find_all()


# ---------------------------------------------------------------------------
# find_all() -- ticket 028-004: real mDNS browse merged with the local
# lock-probe (SUC-004/SUC-005/SUC-006). Every test below except the two
# explicitly marked (the bounded-real-browse test and the
# @pytest.mark.integration test at the bottom of the file) uses a fake
# ``browse=`` -- no real network -- mirroring this file's existing
# ``lock_holder``/``socket_reachable`` DI style exactly.
# ---------------------------------------------------------------------------


def test_find_all_returns_empty_when_nothing_local_and_browse_finds_nothing(
    directories: Directories,
):
    """SUC-005: no daemon running or advertising anywhere -- an empty
    result, not an error, now also covering the browse step explicitly
    (this test's ``browse=`` fake still returns nothing, on top of the
    ``directories`` fixture's own blanket-empty default)."""
    discovery = Discovery(directories=directories, browse=lambda: ())

    assert discovery.find_all() == ()


def test_find_all_merges_local_and_a_distinctly_hostnamed_remote_daemon(
    directories: Directories, monkeypatch
):
    """SUC-004: a local daemon (found via lock-probe) and a second,
    distinctly-hostnamed advertising daemon (found via browse -- the
    two-local-daemon test stand-in) both appear: one
    ``DaemonInfo(is_local=True, host="localhost", ...)``, one
    ``DaemonInfo(is_local=False, host="<ip>:<port>", ...)`` whose
    ``reachable`` came from a real connect probe, not merely "it
    advertised so it must be up"."""
    monkeypatch.setattr(socket, "gethostname", lambda: "my-machine")
    lock_fd = _hold_lock(directories.socket_dir / LOCK_FILENAME)
    local_server = _bind_bare_server(directories.socket_dir / SOCKET_FILENAME)
    remote_server, remote_port = _bind_bare_tcp_server()
    try:
        remote_entry = _BrowseResult(
            address="127.0.0.1", port=remote_port, hostname="test-second-host"
        )
        discovery = Discovery(directories=directories, browse=lambda: (remote_entry,))

        results = discovery.find_all()

        assert len(results) == 2
        local = next(r for r in results if r.is_local)
        remote = next(r for r in results if not r.is_local)
        assert local == DaemonInfo(
            host="localhost", port=0, is_local=True, reachable=True, name="my-machine"
        )
        assert remote.name == "test-second-host"
        assert remote.host == f"127.0.0.1:{remote_port}"
        assert remote.port == remote_port
        assert remote.reachable is True
    finally:
        remote_server.stop(None)
        local_server.stop(None)
        _release_lock(lock_fd)


def test_find_all_filters_out_a_browsed_entry_matching_the_local_hostname(
    directories: Directories, monkeypatch
):
    """The local daemon's own mDNS advertisement (same hostname as the
    client) never produces a second, duplicate entry alongside the
    lock-probe's local entry -- here, with no lock held at all, it must
    not produce *any* entry either."""
    monkeypatch.setattr(socket, "gethostname", lambda: "my-machine")
    own_advertisement = _BrowseResult(address="10.0.0.5", port=6000, hostname="my-machine")
    discovery = Discovery(directories=directories, browse=lambda: (own_advertisement,))

    assert discovery.find_all() == ()


def test_find_all_raises_discovery_failed_when_the_browse_seam_raises(directories: Directories):
    """SUC-006: a genuine browse failure (simulated via the ``browse=``
    DI seam) raises ``DiscoveryFailed`` -- never silently degrading to
    an empty or partial result. Mirrors
    ``test_find_all_raises_discovery_failed_distinctly_from_empty``
    above, for the browse seam instead of ``lock_holder``."""

    def _broken_browse():
        raise OSError("simulated: multicast blocked")

    discovery = Discovery(directories=directories, browse=_broken_browse)

    with pytest.raises(DiscoveryFailed):
        discovery.find_all()


def test_find_all_lists_a_held_but_unreachable_local_alongside_a_reachable_remote(
    directories: Directories, monkeypatch
):
    """Extends the existing "listed, not filtered out" contract
    (``test_find_all_lists_a_held_but_unreachable_daemon``) to the
    multi-daemon case: a held-but-unreachable local daemon combined
    with a distinct, reachable browsed one -- both appear, each with
    its own correct ``reachable`` value."""
    monkeypatch.setattr(socket, "gethostname", lambda: "my-machine")
    lock_fd = _hold_lock(directories.socket_dir / LOCK_FILENAME)
    remote_server, remote_port = _bind_bare_tcp_server()
    try:
        remote_entry = _BrowseResult(
            address="127.0.0.1", port=remote_port, hostname="test-second-host"
        )
        discovery = Discovery(directories=directories, browse=lambda: (remote_entry,))

        results = discovery.find_all()

        local = next(r for r in results if r.is_local)
        remote = next(r for r in results if not r.is_local)
        assert local.reachable is False
        assert remote.reachable is True
    finally:
        remote_server.stop(None)
        _release_lock(lock_fd)


def test_find_all_lists_a_browsed_but_unreachable_remote(directories: Directories, monkeypatch):
    """The remote counterpart of
    ``test_find_all_lists_a_held_but_unreachable_daemon``: a browsed
    entry that advertised but does not answer is listed, flagged
    unreachable, not dropped."""
    monkeypatch.setattr(socket, "gethostname", lambda: "my-machine")
    # Port 1 is privileged and nothing is listening -- a fast, reliable
    # connection refusal, the same trick
    # test_connect_remote_host_raises_connection_failed_when_nothing_answers
    # already relies on elsewhere in this file.
    remote_entry = _BrowseResult(address="127.0.0.1", port=1, hostname="test-second-host")
    discovery = Discovery(directories=directories, browse=lambda: (remote_entry,))

    results = discovery.find_all()

    assert len(results) == 1
    assert results[0].is_local is False
    assert results[0].reachable is False


def test_connect_with_explicit_host_never_calls_find_all(directories: Directories, monkeypatch):
    """SUC-006's explicit callout: ``connect(host)`` (an explicitly
    named ``-d HOST``) is unaffected by this ticket -- it never goes
    through ``find_all()``/``browse`` at all, already true before this
    ticket (module docstring), reconfirmed here now that ``find_all()``
    does real network I/O by default."""
    discovery = Discovery(directories=directories, connect_timeout=0.3)

    def _boom() -> Tuple[DaemonInfo, ...]:
        raise AssertionError("find_all() must not be called by connect()")

    monkeypatch.setattr(discovery, "find_all", _boom)

    with pytest.raises(ConnectionFailed):
        discovery.connect("127.0.0.1:1")


def test_find_all_with_the_real_browse_returns_promptly_and_well_formed(
    directories: Directories,
):
    """AC: the real (default) browse implementation performs an actual
    bounded ``zeroconf.ServiceBrowser`` scan -- this is the one test in
    this file (besides the ``@pytest.mark.integration`` test at the
    bottom) that deliberately touches a real zeroconf/multicast socket,
    to prove the bound itself. A short injected timeout (well below
    ``_DEFAULT_BROWSE_TIMEOUT``) keeps it fast; ``find_all()`` must
    still return within a small multiple of that timeout, not hang.

    040-007 fix-up: this test previously also asserted ``result == ()``,
    i.e. that nothing answers on this LAN. That assumption held only
    while no AprilCam daemon was reachable from wherever the offline
    gate happened to run. Sprint 040 deliberately left a permanent
    daemon running on ``vidar.local``, which mDNS-advertises on this
    developer's LAN -- so this machine's offline-gate run now *always*
    finds at least one real entry, which is correct behaviour, not a
    bug. This test's job is only to prove the real browse is bounded;
    it stays in the offline gate (rather than moving to
    ``@pytest.mark.integration``) because "does a real bounded browse
    return promptly" is exactly the kind of environment-independent
    property the offline gate should keep covering, and no fake
    ``browse=`` can exercise the real ``zeroconf.ServiceBrowser`` path
    this AC targets. What it must not assume is *what* the LAN
    contains -- so it now only checks the result's shape and type, not
    its contents."""
    discovery = Discovery(directories=directories, browse=lambda: _browse_zeroconf(timeout=0.2))

    start = time.monotonic()
    result = discovery.find_all()
    elapsed = time.monotonic() - start

    assert isinstance(result, tuple)
    assert all(isinstance(entry, DaemonInfo) for entry in result)
    assert elapsed < 2.0  # a small multiple of the 0.2s timeout, not "hung"


# ---------------------------------------------------------------------------
# connect() -- AC1/AC2 (local unix socket, remote TCP failure)
# ---------------------------------------------------------------------------


def test_connect_none_connects_to_the_local_daemon_over_the_unix_socket(directories: Directories):
    server = _bind_bare_server(directories.socket_dir / SOCKET_FILENAME)
    try:
        discovery = Discovery(directories=directories, connect_timeout=2.0)

        daemon = discovery.connect(None)

        assert isinstance(daemon, GrpcDaemon)
    finally:
        server.stop(None)


def test_connect_none_raises_connection_failed_when_nothing_is_listening(directories: Directories):
    discovery = Discovery(directories=directories, connect_timeout=0.5)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect(None)

    assert excinfo.value.host == "localhost"


def test_connect_remote_host_raises_connection_failed_when_nothing_answers(directories: Directories):
    discovery = Discovery(directories=directories, connect_timeout=0.5)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect("127.0.0.1:1")

    # Distinct in type from a successful-but-empty discovery result --
    # and names the host that did not answer.
    assert excinfo.value.host == "127.0.0.1:1"
    assert excinfo.value.reason is not None


def test_connect_prefers_explicit_host_over_env_var(directories: Directories, monkeypatch):
    """Precedence: an explicit ``host`` argument beats
    ``APRILCAM_DAEMON_HOST`` (docs/design/configuration.md's cascade)."""
    monkeypatch.setenv("APRILCAM_DAEMON_HOST", "env-host-should-be-ignored:1")
    discovery = Discovery(directories=directories, connect_timeout=0.3)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect("127.0.0.1:1")

    assert excinfo.value.host == "127.0.0.1:1"


def test_connect_falls_back_to_env_var_when_host_is_none(directories: Directories, monkeypatch):
    monkeypatch.setenv("APRILCAM_DAEMON_HOST", "127.0.0.1:1")
    discovery = Discovery(directories=directories, connect_timeout=0.3)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect(None)

    assert excinfo.value.host == "127.0.0.1:1"


# ---------------------------------------------------------------------------
# ticket 040-007: IPv4-preferred hostname resolution
#
# Root cause reproduced offline: the Mac's resolver lists a ``.local``
# host's IPv6 link-local addresses (``dscacheutil -q host -a name
# vidar.local``) ahead of its IPv4 address, and a raw hostname handed to
# ``grpc.insecure_channel`` lets gRPC's own resolver pick the
# unreachable link-local candidate and wait out the full connect
# timeout. Every test below fakes ``socket.getaddrinfo`` -- no real DNS
# or network resolution -- either unit-testing ``split_host_port``/
# ``resolve_ipv4_preferred`` directly, or driving ``Discovery.connect``
# end to end to prove gRPC itself never even dials the (fake,
# unreachable) IPv6 candidate once an IPv4 one is available.
# ---------------------------------------------------------------------------

def test_split_host_port_bare_hostname_gets_default_port():
    assert split_host_port("vidar.local") == ("vidar.local", 5280)


def test_split_host_port_hostname_with_explicit_port():
    assert split_host_port("vidar.local:1234") == ("vidar.local", 1234)


def test_split_host_port_ipv4_literal_with_explicit_port():
    assert split_host_port("192.168.1.144:5280") == ("192.168.1.144", 5280)


def test_split_host_port_bracketed_ipv6_literal_with_port():
    assert split_host_port("[fe80::1]:5280") == ("fe80::1", 5280)


def test_split_host_port_bracketed_ipv6_literal_without_port():
    assert split_host_port("[fe80::1]") == ("fe80::1", 5280)


def test_split_host_port_bare_unbracketed_ipv6_literal_has_no_port_to_find():
    """A bare IPv6 literal's own colons are indistinguishable from a
    ``:port`` separator by counting alone -- this matches the
    pre-040-007 ``":" in host`` behaviour exactly (module docstring),
    not a new limitation this ticket introduces."""
    assert split_host_port("fe80::1") == ("fe80::1", 5280)


def test_resolve_ipv4_preferred_returns_none_for_an_ipv4_literal():
    assert resolve_ipv4_preferred("192.168.1.144", 5280) is None


def test_resolve_ipv4_preferred_returns_none_for_an_ipv6_literal():
    assert resolve_ipv4_preferred("fe80::1", 5280) is None


def test_resolve_ipv4_preferred_picks_ipv4_when_resolver_lists_ipv6_link_local_first(monkeypatch):
    """The exact ``dscacheutil`` ordering ticket 003 observed against
    ``vidar.local``: link-local IPv6 candidates before the IPv4 one."""

    def _fake_getaddrinfo(host, port, proto=0):
        assert host == "vidar.local"
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::7ca:35b0:f731:6b4a", port, 0, 6)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::c:abcd:ef01:2345", port, 0, 12)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.144", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    result = resolve_ipv4_preferred("vidar.local", 5280)

    assert result == (socket.AF_INET, "192.168.1.144")


def test_resolve_ipv4_preferred_ipv4_only_host(monkeypatch):
    def _fake_getaddrinfo(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.144", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    result = resolve_ipv4_preferred("v4-only.local", 5280)

    assert result == (socket.AF_INET, "192.168.1.144")


def test_resolve_ipv4_preferred_falls_back_to_ipv6_when_no_ipv4_candidate(monkeypatch):
    """AC: a hostname with only AAAA records must still work -- there is
    nothing to prefer IPv4 *over*, so the (only) IPv6 candidate is
    returned rather than ``None``, letting the caller dial it."""

    def _fake_getaddrinfo(host, port, proto=0):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    result = resolve_ipv4_preferred("v6-only.example", 5280)

    assert result == (socket.AF_INET6, "2001:db8::1")


def test_resolve_ipv4_preferred_returns_none_on_resolution_failure(monkeypatch):
    def _fake_getaddrinfo(host, port, proto=0):
        raise socket.gaierror("nodename nor servname provided, or not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    assert resolve_ipv4_preferred("no-such-host.invalid", 5280) is None


def test_resolve_ipv4_preferred_returns_none_when_getaddrinfo_returns_nothing(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, proto=0: [])

    assert resolve_ipv4_preferred("empty.example", 5280) is None


def test_connect_remote_dials_the_resolved_ipv4_address_not_the_fake_ipv6_one(
    directories: Directories, monkeypatch
):
    """End-to-end proof of the fix: a fake ``getaddrinfo`` returns the
    real ``dscacheutil`` ordering (IPv6 link-local first, IPv4 second)
    for a made-up hostname. The IPv6 candidate is a syntactically valid
    but entirely undialed address -- if ``Discovery.connect`` still
    handed the raw hostname to gRPC (pre-040-007 behaviour), gRPC's own
    resolver could pick it and this test would hang for the full
    ``connect_timeout`` instead of succeeding immediately against the
    real server bound below."""
    remote_server, remote_port = _bind_bare_tcp_server()
    try:

        def _fake_getaddrinfo(host, port, proto=0):
            assert host == "fake-vidar.local"
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::dead:beef", port, 0, 9)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
        discovery = Discovery(directories=directories, connect_timeout=5.0)

        start = time.monotonic()
        daemon = discovery.connect(f"fake-vidar.local:{remote_port}")
        elapsed = time.monotonic() - start

        assert isinstance(daemon, GrpcDaemon)
        # Well under the 5s connect_timeout -- proves IPv4 was dialed
        # directly rather than gRPC waiting out an unreachable IPv6
        # candidate first.
        assert elapsed < 2.0
    finally:
        remote_server.stop(None)


def test_connect_remote_preserves_the_typed_host_for_identity_after_resolution(
    directories: Directories, monkeypatch
):
    """Resolution must never leak into the identity a caller sees:
    ``ConnectionFailed.host``/``GrpcDaemon``'s ``target`` stay exactly
    what was typed (display, error messages, host-qualified camera
    codes -- sprint 017), never the resolved IP literal."""

    def _fake_getaddrinfo(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    discovery = Discovery(directories=directories, connect_timeout=0.5)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect("fake-vidar.local:1")  # port 1: nothing listens, refused fast

    assert excinfo.value.host == "fake-vidar.local:1"


def test_connect_remote_resolution_failure_still_raises_connection_failed_cleanly(
    directories: Directories, monkeypatch
):
    """AC: resolution failure gives a clean typed error, not an
    unhandled ``socket.gaierror`` -- ``resolve_ipv4_preferred`` swallows
    the failure and the pre-040-007 fallback path (raw hostname to
    gRPC) still produces the same ``ConnectionFailed`` a bad host
    already raised before this ticket."""

    def _fake_getaddrinfo(host, port, proto=0):
        raise socket.gaierror("nodename nor servname provided, or not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    discovery = Discovery(directories=directories, connect_timeout=0.5)

    with pytest.raises(ConnectionFailed) as excinfo:
        discovery.connect("no-such-host.invalid")

    assert excinfo.value.host == "no-such-host.invalid"
    assert excinfo.value.reason is not None


# ---------------------------------------------------------------------------
# ticket 028-004: one real, two-local-daemon-alternate-port integration
# test. Unlike every test above, this one is deliberately NOT fully
# DI'd -- it starts two real ticket-003 daemon stacks (real
# daemon.__main__.build(), real daemon.rpc.build_server TCP bind, real
# daemon.mdns.MdnsAdvertiser) and drives a real Discovery().find_all()
# (default browse, real zeroconf, no fakes) against them, proving
# advertise -> browse -> connect end to end without a second physical
# machine (sprint.md Hardware Constraints item 2). A short real-
# multicast wait is expected and acceptable here. Marked
# @pytest.mark.integration (registered in pyproject.toml) so it is
# excluded from the fast `-m "not camera and not integration"` sweep,
# matching this project's existing convention for real-daemon tests.
# ---------------------------------------------------------------------------


class _SecondHostAdvertiser:
    """Delegates to a real ``MdnsAdvertiser``, but bakes in a synthetic
    ``hostname=`` override for every ``start()`` call.

    ``daemon.__main__.build()`` constructs its own ``MdnsAdvertiser()``
    internally and calls ``advertiser.start(tcp_port)`` with no way to
    pass a ``hostname`` through -- there is no reason for it to, outside
    this one test scenario (see ``MdnsAdvertiser.start``'s own
    docstring). Monkeypatching ``daemon_main.MdnsAdvertiser`` to this
    class for the *second* ``build()`` call only (mirroring
    ``tests/aprilcam/daemon/test_main.py``'s own precedent of
    monkeypatching this exact name) lets that second real daemon
    process advertise under a hostname distinct from this real
    machine's own -- without which ticket 004's client-side self-filter
    would collapse it into "that's just my own local daemon" and defeat
    the whole point of this test (see ``client/discovery.py``'s module
    docstring and ``MdnsAdvertiser.start``'s docstring for the full
    reasoning)."""

    def __init__(self) -> None:
        self._real = MdnsAdvertiser()

    def start(self, tcp_port, *, hostname=None):
        self._real.start(tcp_port, hostname="test-second-host")

    def stop(self) -> None:
        self._real.stop()


@pytest.fixture
def _two_short_socket_dirs() -> Iterator[Tuple[Path, Path]]:
    """Two independent short socket dirs, one per stand-in daemon.

    Deliberately **not** the shared ``short_socket_dir``/``directories``
    fixtures above: those also blanket-monkeypatch
    ``discovery._browse_zeroconf`` to an empty-result stub (see the
    ``directories`` fixture's own docstring), which is exactly right for
    every other test in this file and exactly wrong for this one -- this
    is the one test that must exercise the *real* default browse, per
    this ticket's own AC ("default browse, real zeroconf, no DI fakes").
    """
    d1 = Path(tempfile.mkdtemp(prefix="ac-disc-int1-", dir="/tmp"))
    d2 = Path(tempfile.mkdtemp(prefix="ac-disc-int2-", dir="/tmp"))
    try:
        yield d1, d2
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)


@pytest.mark.integration
def test_find_all_discovers_two_real_local_daemons_on_alternate_ports_via_real_mdns(
    tmp_path: Path,
    _two_short_socket_dirs: Tuple[Path, Path],
    monkeypatch,
):
    """SUC-004, proven end to end: a real local daemon (found via the
    lock-probe, and also self-filtered out of the browse results even
    though it genuinely advertises too) plus a second real daemon
    *process* standing in for a second host (found via real mDNS
    browse, alternate OS-assigned TCP port, synthetic hostname) --
    ``Discovery().find_all()`` reports both, with no duplicate for the
    local one. Uses ``Discovery``'s real default ``browse`` -- no
    ``browse=`` override -- so this genuinely exercises
    ``_browse_zeroconf`` end to end, not a fake standing in for it."""
    short1, short2 = _two_short_socket_dirs
    monkeypatch.setattr("aprilcam.config.directories._writable_or_creatable", lambda path: False)

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short1))
    directories1 = Directories(config_dir=tmp_path / "config1", state_dir=tmp_path / "state1")
    assert directories1.socket_dir == short1 / "aprilcam"

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short2))
    directories2 = Directories(config_dir=tmp_path / "config2", state_dir=tmp_path / "state2")
    assert directories2.socket_dir == short2 / "aprilcam"

    lifecycle1 = daemon_main.build(directories1, start_watch=False, tcp_port=0)
    lifecycle1.start()
    try:
        monkeypatch.setattr(daemon_main, "MdnsAdvertiser", _SecondHostAdvertiser)
        lifecycle2 = daemon_main.build(directories2, start_watch=False, tcp_port=0)
        lifecycle2.start()
        try:
            # The real default browse (no browse= override, and
            # directories1 was never touched by the directories
            # fixture's blanket _browse_zeroconf patch) -- this is the
            # one call in this whole file that exercises discovery.py's
            # real _browse_zeroconf against real advertisements.
            discovery = Discovery(directories=directories1)

            results = discovery.find_all()

            assert len(results) == 2
            local = next(r for r in results if r.is_local)
            remote = next(r for r in results if not r.is_local)
            assert local.host == "localhost"
            assert remote.is_local is False
            assert ":" in remote.host
            assert remote.port != 0
            assert remote.reachable is True
        finally:
            lifecycle2.stop()
    finally:
        lifecycle1.stop()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Donald.local", "donald"),
        ("DONALD", "donald"),
        ("donald.home", "donald"),
        ("10.0.0.5", "10.0.0.5"),
        ("::1", "::1"),
    ],
)
def test_short_host_name(raw: str, expected: str):
    assert short_host_name(raw) == expected
