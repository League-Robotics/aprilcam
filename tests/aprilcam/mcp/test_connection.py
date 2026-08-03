"""Tests for ``aprilcam.mcp.connection.ConnectionManager`` (ticket
026-001, UC-22).

Headless: every test drives ``ConnectionManager`` against a fake
``client.interfaces.Discovery`` injected through its
``discovery_factory`` keyword seam, matching this codebase's
established fake-injection convention (``tests/aprilcam/cli/
test_daemon_cli.py``'s ``_FakeDiscovery``). No real daemon, socket, or
gRPC channel anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pytest

from aprilcam.errors import ConnectionFailed
from aprilcam.mcp.connection import ConnectionManager


@dataclass
class _FakeDaemon:
    label: str


@dataclass
class _FakeDiscovery:
    """Hands back a fresh, distinguishable ``_FakeDaemon`` per
    ``connect()`` call so tests can tell "reused the cached one" apart
    from "reconnected" -- and records every host ``connect()`` was
    called with, in order."""

    connect_calls: List[Optional[str]] = field(default_factory=list)
    fail_hosts: Dict[Optional[str], Exception] = field(default_factory=dict)
    _counter: int = 0

    def connect(self, host: Optional[str] = None) -> _FakeDaemon:
        self.connect_calls.append(host)
        if host in self.fail_hosts:
            raise self.fail_hosts[host]
        self._counter += 1
        return _FakeDaemon(label=f"{host}-{self._counter}")

    def find_all(self):
        return ()


# ---------------------------------------------------------------------------
# construction -- never touches Discovery, per the module's own contract
# ---------------------------------------------------------------------------


def test_construction_never_calls_discovery_factory():
    calls: List[bool] = []

    def poisoned_factory():
        calls.append(True)
        raise AssertionError("discovery_factory must not be called at construction")

    ConnectionManager(default_host="pi.local", discovery_factory=poisoned_factory)

    assert calls == []


def test_construction_stores_arguments_without_raising_even_with_no_discovery_factory_call():
    # Constructing with the real default discovery_factory (the real
    # Discovery class) must not raise or touch a socket either -- this
    # is what lets build_server construct a ConnectionManager
    # unconditionally, before any daemon may exist (UC-22.S-2).
    manager = ConnectionManager(default_host="pi.local")
    assert manager is not None


# ---------------------------------------------------------------------------
# resolve -- connects once per target, then serves from cache
# ---------------------------------------------------------------------------


def test_resolve_connects_once_and_caches_the_same_target():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    first = manager.resolve()
    second = manager.resolve()

    assert first is second
    assert discovery.connect_calls == [None]


def test_resolve_caches_distinct_targets_independently():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    local = manager.resolve()
    remote_a = manager.resolve("pi-a.local")
    remote_a_again = manager.resolve("pi-a.local")
    remote_b = manager.resolve("pi-b.local")

    assert remote_a is remote_a_again
    assert local is not remote_a
    assert remote_a is not remote_b
    assert discovery.connect_calls == [None, "pi-a.local", "pi-b.local"]


def test_explicit_host_overrides_the_session_default():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(default_host="default.local", discovery_factory=lambda: discovery)

    manager.resolve()
    manager.resolve("other.local")
    manager.resolve()  # cached -- still the default target

    assert discovery.connect_calls == ["default.local", "other.local"]


def test_resolve_failure_propagates_and_leaves_nothing_cached():
    discovery = _FakeDiscovery(
        fail_hosts={"pi-a.local": ConnectionFailed("pi-a.local", reason="no answer")}
    )
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    with pytest.raises(ConnectionFailed):
        manager.resolve("pi-a.local")

    # a later resolve tries the connect again -- nothing was cached
    # from the failed attempt.
    discovery.fail_hosts.clear()
    manager.resolve("pi-a.local")
    assert discovery.connect_calls == ["pi-a.local", "pi-a.local"]


# ---------------------------------------------------------------------------
# call -- the eviction contract: evict only on ConnectionFailed, only
# the failing target, only reconnect on the *next* call (UC-22.S-3)
# ---------------------------------------------------------------------------


def test_call_invokes_fn_against_the_resolved_daemon():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    result = manager.call(lambda daemon: daemon.label)

    assert result == "None-1"


def test_call_success_does_not_evict_the_cache():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    manager.call(lambda daemon: daemon)
    manager.call(lambda daemon: daemon)

    assert discovery.connect_calls == [None]


def test_call_evicts_on_connection_failed_and_the_next_call_reconnects():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    manager.resolve()  # populate the cache
    assert discovery.connect_calls == [None]

    def failing(daemon):
        raise ConnectionFailed("localhost", reason="socket closed")

    with pytest.raises(ConnectionFailed):
        manager.call(failing)

    # the failing call itself never reconnected mid-call --
    # `discovery.connect` was not called a second time yet.
    assert discovery.connect_calls == [None]

    # the *next* call for the same target reconnects.
    manager.resolve()
    assert discovery.connect_calls == [None, None]


def test_call_never_evicts_a_different_targets_cache_entry():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    manager.resolve("pi-a.local")
    manager.resolve("pi-b.local")
    assert discovery.connect_calls == ["pi-a.local", "pi-b.local"]

    def failing(daemon):
        raise ConnectionFailed("pi-a.local")

    with pytest.raises(ConnectionFailed):
        manager.call(failing, host="pi-a.local")

    # pi-b's cache entry survives untouched.
    manager.resolve("pi-b.local")
    assert discovery.connect_calls == ["pi-a.local", "pi-b.local"]

    # pi-a reconnects on its own next call.
    manager.resolve("pi-a.local")
    assert discovery.connect_calls == ["pi-a.local", "pi-b.local", "pi-a.local"]


def test_call_does_not_evict_on_an_unrelated_exception():
    discovery = _FakeDiscovery()
    manager = ConnectionManager(discovery_factory=lambda: discovery)

    manager.resolve()

    def failing(daemon):
        raise ValueError("not a connection problem")

    with pytest.raises(ValueError):
        manager.call(failing)

    manager.resolve()
    assert discovery.connect_calls == [None]  # no second connect -- cache entry survived
