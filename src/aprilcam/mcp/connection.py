"""Late-binding daemon connection resolution (UC-22; ticket 026-001;
sprint.md's Architecture Step 3 ``mcp/connection.py`` row and the
"cache-and-evict-on-failure" Design Rationale entry).

**Why cache-and-evict, not reconnect-per-call or preflight-probe.** An
agent session outlives any one daemon connection and may issue dozens
of tool calls a minute (UC-22's own framing). Reconnecting fresh on
every call is correct but pays a channel-setup cost every single time,
almost always for nothing; a preflight health check before every real
call adds a full extra round trip to catch a rare restart window. This
module instead resolves once per distinct target and reuses that
resolution — via :meth:`ConnectionManager.call` — until an actual RPC
raises :class:`~aprilcam.errors.ConnectionFailed`, at which point *only
that target's* cache entry is evicted. The *next* call for the same
target re-resolves; every other cached target is untouched. That is
UC-22.S-3's scenario exactly: "the in-flight call fails with a
connection error; the following call reconnects and succeeds" — zero
overhead in the overwhelmingly common case, self-healing within exactly
one failed call, never more.

**Never a daemon RPC at construction time.** ``__init__`` stores its
arguments and nothing else; ``discovery_factory`` is not even called
until the first :meth:`resolve`/:meth:`call`. This is what lets
``mcp/server.py``'s ``build_server`` construct a ``ConnectionManager``
unconditionally, before any agent has connected or any daemon may even
exist (UC-22.S-2) — construction alone can never fail for lack of a
daemon.

A plain class, not ``typing.Protocol`` — this codebase declares none
(see ``daemon/interfaces.py``'s module docstring for the rule this
package follows throughout).
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, TypeVar

from aprilcam.client.addressing import resolve_camera
from aprilcam.client.discovery import Discovery
from aprilcam.client.interfaces import Discovery as DiscoveryBase
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import ConnectionFailed

__all__ = ["ConnectionManager"]

T = TypeVar("T")

# Cache key for the local daemon reached explicitly (a ``HOST-N`` naming
# this machine). Distinct from ``None`` -- "whatever ``connect(None)``
# picks", which honors ``APRILCAM_DAEMON_HOST`` -- and from any real
# host string.
_LOCAL_KEY = "\0local"


class ConnectionManager:
    """Resolves a :class:`~aprilcam.daemon.interfaces.Daemon` per tool
    call, from a small cache keyed by target host, reconnecting only
    after a target's cached connection has actually failed. See the
    module docstring for the full rationale.

    Parameters
    ----------
    default_host:
        The session default target — ``aprilcam -d HOST mcp``'s value,
        or ``None`` for the local daemon. Any call's own explicit
        *host* argument overrides this for that one call, matching
        every other adapter's flag > per-call argument precedence
        (sprint.md's Design Rationale, "a session default, plus an
        optional per-tool-call daemon parameter").
    discovery_factory:
        Builds the :class:`~aprilcam.client.interfaces.Discovery` used
        to resolve a target on a cache miss. Defaults to the real
        :class:`aprilcam.client.discovery.Discovery`; tests inject a
        fake that never touches a real socket. Called lazily, on the
        first cache miss — never at construction time.
    """

    def __init__(
        self,
        *,
        default_host: Optional[str] = None,
        discovery_factory: Callable[[], DiscoveryBase] = Discovery,
    ) -> None:
        self._default_host = default_host
        self._discovery_factory = discovery_factory
        self._discovery: Optional[DiscoveryBase] = None
        self._cache: Dict[Optional[str], Daemon] = {}

    def _discovery_instance(self) -> DiscoveryBase:
        """The lazily-constructed ``Discovery`` — built on first use,
        never in ``__init__`` (see the module docstring)."""
        if self._discovery is None:
            self._discovery = self._discovery_factory()
        return self._discovery

    def _target_key(self, host: Optional[str]) -> Optional[str]:
        """The cache key for *host*: an explicit per-call *host* always
        wins; otherwise the session default (possibly also ``None``,
        meaning "the local daemon") — both ends of the precedence
        ``Discovery.connect`` itself documents."""
        return host if host is not None else self._default_host

    def _connect_key(self, key: Optional[str]) -> Daemon:
        """The cached ``Daemon`` for an already-final cache *key*."""
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        discovery = self._discovery_instance()
        daemon = discovery.connect_local() if key == _LOCAL_KEY else discovery.connect(key)
        self._cache[key] = daemon
        return daemon

    def resolve(self, host: Optional[str] = None) -> Daemon:
        """A live ``Daemon`` for *host* (or the session default),
        served from cache when one is already resolved for that exact
        target key. A cache miss is the only path that ever calls
        ``Discovery.connect`` — never a preflight probe beyond what
        ``connect`` itself performs to hand back a usable channel.
        """
        return self._connect_key(self._target_key(host))

    def call(self, fn: Callable[[Daemon], T], *, host: Optional[str] = None) -> T:
        """Resolve a ``Daemon`` for *host* and invoke *fn* against it.

        Every tool function in ``mcp/tools/*`` (tickets 002-005) calls
        its daemon through here rather than holding a `Daemon` of its
        own — this is the one place a ``ConnectionFailed`` from an
        actual RPC evicts *this target's* cache entry, so the failing
        call still raises (a tool-level error, once
        ``mcp/errors.py`` maps it) but the *next* call for the same
        target re-resolves via :meth:`resolve` instead of reusing a
        channel already known to be dead. Any other target's cache
        entry is untouched.
        """
        key = self._target_key(host)
        daemon = self.resolve(host)
        try:
            return fn(daemon)
        except ConnectionFailed:
            self._cache.pop(key, None)
            raise

    def call_camera(
        self,
        camera: str,
        fn: Callable[[Daemon, str], T],
        *,
        host: Optional[str] = None,
    ) -> T:
        """Like :meth:`call`, for a tool addressed by *camera*, which may
        be a ``HOST-N`` fan-out address (sprint 048).

        :func:`aprilcam.client.addressing.resolve_camera` picks the daemon
        and the camera string to send it: the per-call *host* is the
        explicit ``target`` (a ``HOST-N`` naming a different daemon raises
        ``ConflictingTarget``), the session default is only the
        ``default_target`` (``HOST-N`` overrides it). A ``HOST-N`` naming
        this machine connects to the local daemon explicitly, so neither
        the session default nor ``APRILCAM_DAEMON_HOST`` can redirect it.
        The cache is keyed by the resolved daemon and evicted on
        ``ConnectionFailed`` exactly as in :meth:`call`; slugs and numbers
        resolve to the same key :meth:`call` would use.
        """
        resolved = resolve_camera(
            camera,
            discovery=self._discovery_instance(),
            target=host,
            default_target=self._default_host,
        )
        key = _LOCAL_KEY if resolved.local else self._target_key(resolved.host)
        daemon = self._connect_key(key)
        try:
            return fn(daemon, resolved.camera)
        except ConnectionFailed:
            self._cache.pop(key, None)
            raise
