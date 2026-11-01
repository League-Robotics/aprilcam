"""Tests for ``discovery._browse_zeroconf``'s fallback when the default
``Zeroconf()`` cannot bind UDP 5353 on every interface (``EADDRINUSE``):
a unicast instance plus a default-interface instance, merged."""

from __future__ import annotations

import errno
from typing import Any, Dict, List, Optional

import pytest
from zeroconf import InterfaceChoice, ServiceStateChange

from aprilcam.client import discovery
from aprilcam.client.discovery import _BrowseResult, _browse_zeroconf


def _in_use() -> OSError:
    return OSError(errno.EADDRINUSE, "Address already in use")


class _Info:
    def __init__(self, address: str, host: bytes) -> None:
        self.port = 5280
        self.properties = {b"host": host}
        self._address = address

    def parsed_addresses(self) -> List[str]:
        return [self._address]


def _fake_zeroconf(
    refuse: List[str], answers: Dict[str, Dict[str, _Info]], made: List[str], closed: List[str]
) -> Any:
    """A Zeroconf stand-in keyed by its mode: "all" (the default
    constructor), "unicast", or "default" (InterfaceChoice.Default).
    Modes in *refuse* raise EADDRINUSE; *answers* maps a mode to the
    services its browser reports."""

    class FakeZeroconf:
        def __init__(self, interfaces: Any = None, unicast: bool = False) -> None:
            self.mode = (
                "unicast" if unicast else "default" if interfaces is InterfaceChoice.Default else "all"
            )
            if self.mode in refuse:
                raise _in_use()
            made.append(self.mode)

        def get_service_info(self, _type: str, name: str, timeout: int = 3000) -> Optional[_Info]:
            return answers.get(self.mode, {}).get(name)

        def close(self) -> None:
            closed.append(self.mode)

    return FakeZeroconf


class _FakeBrowser:
    def __init__(self, zc: Any, service_type: str, handlers: List[Any]) -> None:
        for name in (zc.get_service_info(service_type, n) and n for n in ("a", "b")):
            if name:
                for handler in handlers:
                    handler(
                        zeroconf=zc,
                        service_type=service_type,
                        name=name,
                        state_change=ServiceStateChange.Added,
                    )

    def cancel(self) -> None:
        pass


@pytest.fixture
def fake_mdns(monkeypatch):
    def install(refuse: List[str], answers: Dict[str, Dict[str, _Info]]):
        made: List[str] = []
        closed: List[str] = []
        monkeypatch.setattr(discovery, "Zeroconf", _fake_zeroconf(refuse, answers, made, closed))
        monkeypatch.setattr(discovery, "ServiceBrowser", _FakeBrowser)
        return made, closed

    return install


def test_port_taken_merges_unicast_and_default_interface_results(fake_mdns):
    made, closed = fake_mdns(
        refuse=["all"],
        answers={
            "unicast": {"a": _Info("192.168.1.23", b"buzzkill")},
            "default": {"b": _Info("192.168.4.60", b"donald")},
        },
    )

    result = _browse_zeroconf(timeout=0.01)

    assert made == ["unicast", "default"]
    assert sorted(closed) == ["default", "unicast"]
    assert sorted(result, key=lambda r: r.hostname) == [
        _BrowseResult(address="192.168.1.23", port=5280, hostname="buzzkill"),
        _BrowseResult(address="192.168.4.60", port=5280, hostname="donald"),
    ]


def test_port_taken_everywhere_browses_unicast_only(fake_mdns):
    made, closed = fake_mdns(
        refuse=["all", "default"],
        answers={"unicast": {"a": _Info("192.168.1.23", b"buzzkill")}},
    )

    result = _browse_zeroconf(timeout=0.01)

    assert made == closed == ["unicast"]
    assert result == (_BrowseResult(address="192.168.1.23", port=5280, hostname="buzzkill"),)


def test_no_conflict_uses_only_the_all_interfaces_instance(fake_mdns):
    made, _closed = fake_mdns(refuse=[], answers={"all": {"a": _Info("10.0.0.2", b"x")}})

    assert _browse_zeroconf(timeout=0.01) == (
        _BrowseResult(address="10.0.0.2", port=5280, hostname="x"),
    )
    assert made == ["all"]


def test_any_other_socket_error_still_propagates(monkeypatch):
    def no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(discovery, "Zeroconf", no_network)
    with pytest.raises(OSError) as excinfo:
        _browse_zeroconf(timeout=0.01)
    assert excinfo.value.errno == errno.ENETUNREACH
