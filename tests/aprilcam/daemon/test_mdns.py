"""Tests for ``aprilcam.daemon.mdns`` (ticket 028-003).

Fully headless: :class:`_FakeZeroconf` stands in for the real
``zeroconf.Zeroconf``, injected via ``MdnsAdvertiser``'s
``zeroconf_factory`` constructor seam (mirroring ``client.discovery.
Discovery``'s ``lock_holder``/``socket_reachable`` DI pattern), so
these tests exercise register/unregister/failure-swallowing behaviour
without ever opening a real multicast socket. ``_primary_routable_ipv4``
gets one real (unmocked) exercise of the actual UDP-socket trick plus
one mocked failure-path test.
"""

from __future__ import annotations

import logging
import socket
from typing import List, Optional

from zeroconf import Error as ZeroconfError
from zeroconf import ServiceInfo

from aprilcam.daemon.mdns import MdnsAdvertiser, _primary_routable_ipv4


class _FakeZeroconf:
    """A ``Zeroconf`` double: records register/unregister/close calls;
    optionally raises on any of them to simulate a real failure."""

    def __init__(
        self,
        *,
        register_raises: Optional[Exception] = None,
        unregister_raises: Optional[Exception] = None,
    ) -> None:
        self.registered: List[ServiceInfo] = []
        self.unregistered: List[ServiceInfo] = []
        self.closed = False
        self._register_raises = register_raises
        self._unregister_raises = unregister_raises

    def register_service(self, info: ServiceInfo) -> None:
        if self._register_raises is not None:
            raise self._register_raises
        self.registered.append(info)

    def unregister_service(self, info: ServiceInfo) -> None:
        if self._unregister_raises is not None:
            raise self._unregister_raises
        self.unregistered.append(info)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# start() -- successful registration
# ---------------------------------------------------------------------------


def test_start_registers_a_service_advertising_the_given_tcp_port():
    fake = _FakeZeroconf()
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)

    advertiser.start(5280)

    assert len(fake.registered) == 1
    assert fake.registered[0].port == 5280


def test_start_txt_properties_are_exactly_version_and_host():
    """AC: keep exactly these two TXT keys -- ticket 004's client-side
    self-filter keys on ``host``; a rename here would silently break
    it without either side raising an error."""
    fake = _FakeZeroconf()
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)

    advertiser.start(5280, hostname="some-host")

    properties = fake.registered[0].properties
    assert set(properties.keys()) == {b"version", b"host"}
    assert properties[b"host"] == b"some-host"


def test_start_hostname_override_is_used_verbatim_not_the_real_hostname(monkeypatch):
    """AC/rationale: the hostname override exists specifically so two
    real daemon *processes* sharing one real ``socket.gethostname()``
    (ticket 004's two-local-daemon-alternate-port test scenario) can be
    told apart by ticket 004's client-side self-filter, which compares
    a browsed entry's TXT ``host`` value against the client's own
    ``socket.gethostname()``. Without this override both processes
    would advertise under the identical real hostname and the second
    one would be indistinguishable from (and incorrectly filtered out
    as) the first."""
    monkeypatch.setattr(socket, "gethostname", lambda: "real-hostname")
    fake = _FakeZeroconf()
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)

    advertiser.start(5280, hostname="test-second-host")

    assert fake.registered[0].properties[b"host"] == b"test-second-host"


def test_start_with_no_hostname_override_uses_the_real_hostname(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "real-hostname")
    fake = _FakeZeroconf()
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)

    advertiser.start(5280)

    assert fake.registered[0].properties[b"host"] == b"real-hostname"


# ---------------------------------------------------------------------------
# start() -- failure is logged, never raised (mDNS is never load-bearing)
# ---------------------------------------------------------------------------


def test_start_swallows_a_registration_failure_and_logs_a_warning(caplog):
    fake = _FakeZeroconf(register_raises=ZeroconfError("collision"))
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)

    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.mdns"):
        advertiser.start(5280)  # must not raise

    assert any("collision" in record.message for record in caplog.records)
    # A failed start() leaves nothing behind for stop() to unregister.
    advertiser.stop()
    assert not fake.unregistered


def test_start_swallows_an_oserror_from_the_zeroconf_factory_itself(caplog):
    def _raising_factory() -> "object":
        raise OSError("no multicast interface")

    advertiser = MdnsAdvertiser(zeroconf_factory=_raising_factory)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.mdns"):
        advertiser.start(5280)  # must not raise

    assert any("no multicast interface" in record.message for record in caplog.records)


def test_start_closes_a_successfully_constructed_zeroconf_if_registration_then_fails():
    """A registration failure after ``Zeroconf()`` itself succeeded
    must not leak the constructed instance -- close() runs even though
    register_service() is what raised."""
    fake = _FakeZeroconf(register_raises=ZeroconfError("collision"))
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)

    advertiser.start(5280)

    assert fake.closed is True


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


def test_stop_before_start_is_a_quiet_no_op():
    advertiser = MdnsAdvertiser()
    advertiser.stop()  # must not raise, and must not construct a real Zeroconf


def test_stop_unregisters_and_closes():
    fake = _FakeZeroconf()
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)
    advertiser.start(5280)

    advertiser.stop()

    assert len(fake.unregistered) == 1
    assert fake.closed is True


def test_stop_is_idempotent():
    fake = _FakeZeroconf()
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)
    advertiser.start(5280)

    advertiser.stop()
    advertiser.stop()  # must not raise, must not double-unregister

    assert len(fake.unregistered) == 1


def test_stop_swallows_an_unregister_failure_and_logs_a_warning(caplog):
    fake = _FakeZeroconf(unregister_raises=ZeroconfError("teardown broke"))
    advertiser = MdnsAdvertiser(zeroconf_factory=lambda: fake)
    advertiser.start(5280)

    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.mdns"):
        advertiser.stop()  # must not raise

    assert any("teardown broke" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# _primary_routable_ipv4 -- the earned /etc/hosts 127.0.1.1 workaround
# ---------------------------------------------------------------------------


def test_primary_routable_ipv4_falls_back_to_loopback_when_the_probe_socket_fails(monkeypatch):
    class _RaisingSocket:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            raise OSError("no route")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _RaisingSocket)

    assert _primary_routable_ipv4() == "127.0.0.1"


def test_primary_routable_ipv4_returns_a_real_non_loopback_address_or_falls_back():
    """Not mocked -- exercises the real UDP-socket trick end to end.
    Any dev/CI machine with at least one configured network interface
    (no real connectivity required -- no packet is ever sent) resolves
    to a non-loopback address; a fully interface-less sandbox falls
    back to 127.0.0.1, which this assertion also accepts rather than
    asserting one specific outcome."""
    addr = _primary_routable_ipv4()
    assert addr == "127.0.0.1" or not addr.startswith("127.")
