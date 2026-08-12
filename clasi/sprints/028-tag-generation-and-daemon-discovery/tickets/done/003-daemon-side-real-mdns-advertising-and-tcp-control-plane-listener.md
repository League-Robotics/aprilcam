---
id: '003'
title: Daemon-side real mDNS advertising and TCP control-plane listener
status: done
use-cases:
- SUC-004
- SUC-007
depends-on: []
github-issue: ''
issue: tag-generation-and-daemon-discovery.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon-side real mDNS advertising and TCP control-plane listener

**Depends on**: — (independent of ticket 001/002's tag-generation
work; parallel foundation for the discovery half of this sprint)
**Serves**: SUC-004, SUC-007 (UC-13) — the daemon-side half; SUC-004's
CLI-visible listing and SUC-007's fan-out both need a real second
daemon to find, which starts here.

## Description

Two changes, in dependency order within this one ticket:

1. **`daemon/rpc.py`'s `build_server()` also binds TCP**, alongside the
   existing unix-socket bind, at `src/aprilcam/daemon/rpc.py:1073`
   (`server.add_insecure_port(f"unix:{socket_path}")`). This is the
   control-plane listener referenced by `config/directories.py`'s
   Open Question 2 ("Opening TCP:5280 is deferred to whichever future
   sprint actually exercises a daemon-to-daemon or CLI-to-remote-daemon
   path end-to-end against a real second host") — this is that sprint;
   update that module's docstring to say so once the binding is real
   (see Documentation updates below).
2. **`daemon/mdns.py` (new)**: a fresh v2 `MdnsAdvertiser`, converted
   from `archive/src/aprilcam/daemon/mdns.py` — mine it for the two
   pieces of earned knowledge it has, do not port its structure (see
   "Converting from the archive" below).

**Stakeholder decision (recorded 2026-08-12, binding on this ticket):**
the TCP listener binds **all interfaces** (`0.0.0.0`) on port **5280**,
**unauthenticated**, matching UC-01's main-success-path step 2 ("the
daemon takes its singleton lock and opens its control plane (unix
socket + TCP, mDNS-advertised)") — not loopback-only. This is a
deliberate, explicitly-accepted change in the daemon's network exposure
(sprint architecture's Design Rationale and Open Question 1) — do not
narrow it to loopback or add authentication; both are out of scope
(sprint.md Out of Scope).

**Failure to bind TCP is non-fatal**, matching the existing
"never load-bearing" philosophy already established for the unix
socket and for mDNS itself in v1: if TCP:5280 cannot bind (e.g.
already in use — the expected case for the two-local-daemon test
scenario), log a warning, skip advertising for this process, and
continue serving on the unix socket exactly as today. The daemon must
never fail to start because of this.

## Files to touch

- `src/aprilcam/daemon/mdns.py` (new) — `MdnsAdvertiser`.
- `src/aprilcam/daemon/rpc.py` — `build_server()` gains an optional
  TCP bind.
- `src/aprilcam/daemon/lifecycle.py` — `DaemonLifecycle` gains
  `advertise`/`stop_advertise` hooks; `ControlPlaneListener` (and its
  real implementation `daemon.rpc._GrpcListener`) reports whether/what
  TCP port it bound.
- `src/aprilcam/daemon/__main__.py` — `build()` wires `MdnsAdvertiser`
  and the TCP port through.
- `src/aprilcam/config/directories.py` — gains `DEFAULT_DAEMON_PORT =
  5280`.
- `src/aprilcam/client/discovery.py` — its existing module-level
  `DEFAULT_DAEMON_PORT = 5280` (line 75) becomes a re-export of
  `config.directories.DEFAULT_DAEMON_PORT` rather than its own
  definition, so every existing importer of
  `client.discovery.DEFAULT_DAEMON_PORT` (grep for it first — at least
  `client/discovery.py`'s own `_connect_remote` uses it internally)
  keeps working unchanged. `client/` and `daemon/` both need this
  constant with neither importing the other — `config/directories.py`
  is the dependency-direction-clean shared home (sprint architecture
  Step 5, item 9).
- `tests/aprilcam/daemon/test_lifecycle.py` (existing) — extend with
  `advertise`/`stop_advertise` hook-ordering coverage, mirroring the
  existing `watch`/`stop_watch` event-order tests.
- `tests/aprilcam/daemon/test_mdns.py` (new).
- A TCP-bind test for `build_server()` — add to whichever existing
  test module already covers `daemon/rpc.py`'s `build_server`/
  `_Servicer` (grep for its current test coverage first; if none
  exists yet, create `tests/aprilcam/daemon/test_rpc.py`).

## Acceptance Criteria

- [x] `build_server(service, socket_path, tcp_port=<port>)` binds a TCP
      listener on `0.0.0.0:<port>` in addition to the existing unix
      socket, using `server.add_insecure_port(...)` exactly as the
      unix-socket bind already does (both are `grpc.Server` binds on
      the same server object — one gRPC server, two transports, no
      change to `_Servicer` or any RPC's request/response shape).
      `tcp_port=None` (the default) skips the TCP bind entirely,
      preserving today's unix-socket-only behavior for every existing
      caller that does not pass it.
- [x] `add_insecure_port` returns `0` on bind failure (confirmed
      in-tree behavior, not new to this ticket) — this return value is
      the sole signal used to decide "did TCP actually bind." On
      failure: log a WARNING naming the port and that the daemon is
      continuing unix-socket-only; do not raise; do not prevent
      `build_server` from returning a working listener.
      **Correction found during implementation** (see Completion
      Notes): this codebase's pinned `grpc` (1.80.0) never actually
      returns `0` to a Python caller — `grpc._common.
      validate_port_binding_result` converts the C-core's `0` into a
      raised `RuntimeError` before `add_insecure_port` returns.
      Verified by direct testing, not assumed. Implemented the
      *intent* of this criterion (bind failure caught, logged at
      WARNING naming the port, never raised, listener still works) by
      catching `RuntimeError`, with the literal `0`-return path also
      handled defensively in case a different `grpc` build ever
      surfaces it. Also disabled `grpc`'s default `SO_REUSEPORT` (not
      requested by this criterion's text, but necessary for a same-port
      collision to be observable as a failure at all — see Completion
      Notes and `build_server`'s docstring).
- [x] The bound TCP port (or `None` if not requested or the bind
      failed) is observable by whatever calls `build_server` and later
      `start()`s the listener it returns — this is what lets
      `DaemonLifecycle` decide whether to invoke the `advertise` hook
      and with which port. (Implementation note: this is a genuine,
      deliberate interface change — see Implementation Plan.)
- [x] `DaemonLifecycle` gains optional `advertise: Optional[Callable[
      [int], None]] = None` and `stop_advertise: Optional[Callable[[],
      None]] = None` constructor parameters, added near the existing
      `watch`/`stop_watch` parameters
      (`src/aprilcam/daemon/lifecycle.py:180-182`), documented the same
      way.
- [x] **Ordering**: `advertise(port)` is called in `start()` **after**
      `listener.start()` returns (`daemon/lifecycle.py`'s existing
      `start()` body, currently ending `listener.start(); self.
      _listener = listener; self._running = True; ...`) and only when
      a TCP port was actually bound — never before the control plane
      is genuinely accepting connections, and never when TCP did not
      bind. `stop_advertise()` is called in `stop()` alongside
      `_stop_watch` (i.e., early — before the listener stops accepting
      RPCs), matching "stop passive watching first... then the
      listener" ordering already documented in that method's
      docstring.
  - [x] A hook-ordering test (mirroring
        `test_start_calls_watch_before_building_the_control_plane_listener`)
        proves `advertise` fires after `build_listener`/`listener.
        start()` in the event log, and `stop_advertise` fires
        before the listener's own `stop()`.
- [x] `daemon/mdns.py`'s `MdnsAdvertiser`:
  - [x] `start(tcp_port: int, *, hostname: Optional[str] = None) -> None`
        registers a `_aprilcam._tcp.local.` zeroconf service record
        advertising `tcp_port`, with TXT `properties={b"version": ...,
        b"host": (hostname or socket.gethostname()).encode()}` — **keep
        exactly these two TXT keys** (`version`, `host`); ticket 004's
        client-side self-filter keys on `host` to avoid double-counting
        the local daemon's own advertisement, so a key rename here
        would silently break that. The `hostname` override (default
        `None` → real `socket.gethostname()`, unchanged behavior for
        every real caller) exists **specifically** so ticket 004's
        two-local-daemon-alternate-port integration test can advertise
        its second process under a distinguishable synthetic identity
        (e.g. `"test-second-host"`): both real test processes run on
        the same physical machine and therefore share one real
        `socket.gethostname()` value, and ticket 004's self-filter
        compares a browsed entry's `host` TXT against the *client's*
        `socket.gethostname()` to decide "is this my own local daemon's
        advertisement" — without this override, both processes would
        advertise identically and the second, alternate-port daemon
        would be indistinguishable from (and incorrectly filtered out
        as) the first. Document this rationale in the module
        docstring so a future reader does not mistake the parameter for
        dead code.
  - [x] `stop() -> None` unregisters the service record and closes the
        `Zeroconf` instance; safe to call even if `start()` was never
        called or failed partway (no-op, not an error).
  - [x] Every zeroconf call is wrapped so a registration or teardown
        failure is logged at WARNING and never propagates — mDNS
        advertising is never load-bearing for the daemon's own
        operation, mirroring the "TCP bind failure is non-fatal"
        contract above. **But**: catch specific, expected exception
        types (e.g. `OSError`, and whatever `zeroconf` itself
        documents as raisable), not a bare `except Exception:` — v1's
        `except Exception:  # noqa: BLE001` is exactly the kind of
        thing that does not pass this codebase's mypy/lint bar; see
        "Converting from the archive" below.
  - [x] No `Optional[object]`-typed fields (v1's
        `self._zeroconf: Optional[object]` / `self._info:
        Optional[object]`) — real types (`Optional[Zeroconf]`,
        `Optional[ServiceInfo]`), since `zeroconf` is already an
        unconditional base dependency in this codebase (already in
        `pyproject.toml`'s `dependencies`, confirmed — no lazy/guarded
        import needed here, unlike v1 which had to tolerate zeroconf
        being absent).
  - [x] `_primary_routable_ipv4()` (or equivalent) is preserved:
        the UDP-socket trick (connect a `SOCK_DGRAM` socket to a
        non-routable TEST-NET-1 address, read `getsockname()`) that
        avoids advertising `127.0.1.1` when `/etc/hosts` maps the
        hostname there (Ubuntu's common default) — this is real,
        earned knowledge from v1, keep it.
- [x] `daemon/__main__.py`'s `build()`:
  - [x] Gains a `tcp_port: Optional[int] = DEFAULT_DAEMON_PORT`
        parameter (imported from `config.directories`), mirroring the
        existing `lock_path`/`socket_path` override pattern — real
        callers get the default; the two-local-daemon test scenario
        overrides it to an alternate port.
  - [x] Passes `tcp_port` through to `build_server` via the existing
        `functools.partial(build_server, service, resolved_socket_path,
        transport=transport, ...)` construction.
  - [x] Constructs an `MdnsAdvertiser` and wires its `start`/`stop`
        methods as `DaemonLifecycle`'s `advertise`/`stop_advertise`
        hooks. No extra gating logic is needed here beyond what
        `DaemonLifecycle.start()` already does (only calls `advertise`
        when a port was actually bound) — do not duplicate that check
        in `__main__.py`.
- [x] No `.proto` change; `_Servicer` is unmodified.
- [x] `uv run mypy` clean on every file touched. Zero `typing.Protocol`
      anywhere touched.
- [x] Full existing suite stays green — in particular, no existing
      headless test that builds a server against a `tmp_path` socket
      breaks (`tcp_port=None` default preserves current behavior for
      every caller that does not opt in).

## Implementation Plan

**Approach:**

1. **`config/directories.py`**: add `DEFAULT_DAEMON_PORT = 5280` as a
   module-level constant (alongside the existing `SYSTEM_CONFIG_DIR`-
   style globals), with a short docstring note pointing at spec §4.1.
   Update `client/discovery.py` to import and re-export it instead of
   redefining it (`__all__` keeps listing `"DEFAULT_DAEMON_PORT"`, so
   `from aprilcam.client.discovery import DEFAULT_DAEMON_PORT` still
   works for every existing caller).
2. **`daemon/rpc.py`**: add `tcp_port: Optional[int] = None` to
   `build_server`'s signature. After the existing
   `server.add_insecure_port(f"unix:{socket_path}")` line, if
   `tcp_port is not None`, call `server.add_insecure_port(f"0.0.0.0:
   {tcp_port}")` and capture its return; `0` means failure (log
   WARNING, treat as "no TCP"), anything else is the real bound port
   (for a fixed non-zero request, grpc returns the same port number on
   success). Thread this bound-port value into whatever `_GrpcListener`
   returns from `start()` (see next step) — `build_server` itself
   still returns a `_GrpcListener`, just one that now knows its TCP
   port.
3. **`daemon/lifecycle.py`**: this is the one genuine interface change
   in this ticket. `ControlPlaneListener.start()`'s declared return
   type changes from `None` to `Optional[int]` (the bound TCP port, or
   `None` if none was requested or the bind failed) — still a plain
   base class (`raise NotImplementedError` body), still no
   `typing.Protocol`. This is backward compatible with the existing
   test double
   (`tests/aprilcam/daemon/test_lifecycle.py`'s `_FakeListener.start()
   -> None`) since a bare `return`/implicit `None` is a valid
   `Optional[int]` value — that existing fake needs no change for
   tests that do not care about TCP; add a second fake variant (or a
   constructor flag on the existing one) for the new
   `advertise`-ordering tests. `daemon.rpc._GrpcListener.start()`
   becomes `self._server.start(); return self._tcp_port` (with
   `_tcp_port` captured at `_GrpcListener.__init__` time from
   `build_server`'s bind result). Add `advertise`/`stop_advertise`
   constructor parameters to `DaemonLifecycle` (near `watch`/
   `stop_watch`, `src/aprilcam/daemon/lifecycle.py:180-182`), store
   them, and wire the ordering described in Acceptance Criteria into
   `start()`/`stop()`.
4. **`daemon/mdns.py`**: write fresh, following "Converting from the
   archive" below.
5. **`daemon/__main__.py`**: wire `DEFAULT_DAEMON_PORT`, `tcp_port`
   override parameter, and `MdnsAdvertiser` into `build()`.
6. Tests: hook-ordering tests in `test_lifecycle.py`; a TCP-bind test
   for `build_server` (bind succeeds on an OS-assigned port — use
   `tcp_port=0` in the test to get an ephemeral port from the OS rather
   than fighting for 5280 in CI, and confirm a real TCP client can
   connect; bind failure path — attempt to bind the same fixed port
   twice in one test process and confirm the second `build_server`
   call warns and returns a listener whose `start()` yields `tcp_port=
   None`, not an exception); `test_mdns.py` with a fake/injected
   `Zeroconf`+`ServiceInfo` (dependency-injected via constructor
   parameters — mirror `client.discovery.Discovery`'s existing
   `lock_holder`/`socket_reachable` DI seam so `MdnsAdvertiser` is
   testable without a real multicast stack) covering: successful
   register/unregister, a registration failure that logs and does not
   raise, `stop()` before `start()` is a no-op.

**Converting from the archive (`archive/src/aprilcam/daemon/mdns.py`):
mine it, don't port it.** Keep exactly two pieces of earned knowledge:
(a) `_primary_routable_ipv4()`'s UDP-socket trick — real, hard-won
knowledge about `/etc/hosts`/127.0.1.1 that has nothing to do with v1's
structure; (b) the TXT property keys `{b"version", b"host"}` — ticket
004 keys its self-filter on `host`. Do **not** carry forward: (1)
`Optional[object]`-typed fields — `zeroconf` is an unconditional base
dependency in v2 (unlike v1, which had to tolerate it being absent), so
import `ServiceInfo`/`Zeroconf` normally at module level and type the
fields as `Optional[Zeroconf]`/`Optional[ServiceInfo]`; (2) `except
Exception:  # noqa: BLE001` — catch the specific exception types that
can actually occur (`OSError` at minimum; check what `zeroconf`'s own
`register_service`/`unregister_service`/`Zeroconf()` construction
documents as raisable); (3) the lazy `from zeroconf import ...` inside
`start()`'s `try` block — with zeroconf unconditional, import it at
module scope like any other dependency.

**Files to create/modify:**
- `src/aprilcam/daemon/mdns.py` (new)
- `src/aprilcam/daemon/rpc.py`
- `src/aprilcam/daemon/lifecycle.py`
- `src/aprilcam/daemon/__main__.py`
- `src/aprilcam/config/directories.py`
- `src/aprilcam/client/discovery.py` (re-export only, no behavior
  change)
- `tests/aprilcam/daemon/test_lifecycle.py`
- `tests/aprilcam/daemon/test_mdns.py` (new)
- A `build_server`/TCP-bind test (new module or existing, see Files to
  touch above)

**Testing plan:** all headless — dependency-injected fakes for
zeroconf at the `MdnsAdvertiser` seam, a real (but ephemeral-port)
TCP bind test for `build_server`'s bind-success/bind-failure paths, and
event-order fakes for `DaemonLifecycle`'s hook ordering. No real second
machine needed for this ticket — the real two-local-daemon-alternate-
port integration test that proves the *whole* advertise→browse→connect
mechanism end to end belongs to ticket 004 (`Discovery.find_all()`),
which is where "did this daemon's advertisement get found" is actually
asserted; this ticket proves the daemon-side half in isolation.

**Documentation updates:**
- `config/directories.py`'s module docstring currently states Open
  Question 2 as unresolved ("Sprint 020 Open Question 2 —
  unix-socket-only, deliberately... Opening TCP:5280 is deferred to
  whichever future sprint..."). Update it to record that this sprint
  is the one that resolved it, briefly, without deleting the historical
  reasoning (matches this codebase's pattern of recording decisions in
  place rather than erasing the "why" once superseded).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/
  test_lifecycle.py` and the full suite (`uv run pytest`) to confirm
  every existing unix-socket-only `build_server` caller is unaffected.
- **New tests to write**: see Implementation Plan's step 6 — TCP
  bind success/failure for `build_server`, `advertise`/`stop_advertise`
  hook-ordering for `DaemonLifecycle`, and `MdnsAdvertiser`'s
  register/unregister/failure-swallowing behavior via injected fakes.
- **Verification command**: `uv run pytest && uv run mypy`

## Completion Notes

Implemented as planned, with two implementation-level corrections
found while building it — both documented in code, both preserving
this ticket's stated *intent* exactly (TCP bind failure is caught,
logged, non-fatal; a real port collision is observable rather than
silently misrouted). Nothing partial or blocked; no real second
machine was available or needed for this ticket (that verification
belongs to ticket 004, per this ticket's own Testing section).

**Correction 1 — `add_insecure_port`'s actual failure signal.** This
ticket's text states "`add_insecure_port` returns `0` on bind failure
(confirmed in-tree behavior)". Direct testing during implementation
(reproduced with plain `grpc.server()` scripts, not assumed) showed
this is not true for this codebase's pinned `grpc==1.80.0`:
`grpc._common.validate_port_binding_result` — the function the public
`Server.add_insecure_port` wrapper always calls — converts the
C-core's `0` into a raised `RuntimeError` before any Python caller
ever sees a `0` return. So `build_server` catches `RuntimeError`
around the TCP `add_insecure_port` call as the primary failure signal,
with the literal `0`-return path also still handled (defensively) in
case some other `grpc` build/platform ever surfaces it that way
instead. The observable contract this criterion actually cares about
— WARNING logged naming the port, no exception escapes `build_server`,
the returned listener still works, `start()` reports `tcp_port=None`
— holds in both cases and is covered by
`test_build_server_tcp_bind_failure_is_non_fatal_and_reports_no_port`.
Documented in `build_server`'s own docstring so a future reader does
not "fix" the `RuntimeError` catch back to a bare `== 0` check based
on this ticket's original text.

**Correction 2 — `grpc`'s default `SO_REUSEPORT` would have defeated
the collision test.** Not something the ticket asked for, but
necessary to make the ticket's own described failure scenario ("if
TCP:5280 cannot bind (e.g. already in use — the expected case for the
two-local-daemon test scenario)") actually reachable. `grpc.server()`
enables `SO_REUSEPORT` on its TCP listener by default; verified by
direct testing that two *independent* `grpc.server()` processes/
instances can both silently "succeed" binding the identical fixed TCP
port under that default, with the kernel load-balancing incoming
connections between two unrelated servers — a materially worse
failure mode than the graceful degradation this ticket specifies (a
client could be routed to the wrong daemon with no error at all).
`build_server` now constructs its `grpc.Server` with
`options=[("grpc.so_reuseport", 0)]`, which has no effect on the
existing unix-socket bind and makes a same-port collision surface as
the intended, observable bind failure. Documented in `build_server`'s
docstring.

**Files changed:**
- `src/aprilcam/config/directories.py` — added `DEFAULT_DAEMON_PORT =
  5280`; updated the "Sprint 020 Open Question 2" docstring section to
  record this sprint as the one that resolved it, without deleting the
  original reasoning.
- `src/aprilcam/client/discovery.py` — `DEFAULT_DAEMON_PORT` is now
  imported from `config.directories` and re-exported (not redefined);
  `__all__` and every existing importer (`Discovery._connect_remote`
  included) unchanged.
- `src/aprilcam/daemon/rpc.py` — `build_server` gains `tcp_port:
  Optional[int] = None`; `_GrpcListener` gains a `tcp_port` field and
  `start()` now returns `Optional[int]` (was `None`); `grpc.server()`
  construction adds `options=[("grpc.so_reuseport", 0)]` (see
  Correction 2). No change to `_Servicer` or any RPC shape.
- `src/aprilcam/daemon/lifecycle.py` — `ControlPlaneListener.start()`
  return type changed `None` -> `Optional[int]` (still a plain base
  class, `raise NotImplementedError` body); `DaemonLifecycle` gains
  `advertise`/`stop_advertise` constructor parameters and the ordering
  described in Acceptance Criteria, in both `start()` and `stop()`.
- `src/aprilcam/daemon/mdns.py` (new) — `MdnsAdvertiser`, converted
  from `archive/src/aprilcam/daemon/mdns.py` per "Converting from the
  archive": kept `_primary_routable_ipv4()`'s UDP-socket trick and the
  `{version, host}` TXT keys; dropped `Optional[object]` fields, the
  lazy in-`try` import, and the bare `except Exception`. No base
  class, per the sprint's Design Rationale. DI seam is a
  `zeroconf_factory: Callable[[], Zeroconf] = Zeroconf` constructor
  parameter, mirroring `client.discovery.Discovery`'s `lock_holder`/
  `socket_reachable` pattern.
- `src/aprilcam/daemon/__main__.py` — `build()` gains `tcp_port:
  Optional[int] = DEFAULT_DAEMON_PORT`, threaded into `build_server`
  via the existing `functools.partial`; constructs one
  `MdnsAdvertiser()` and wires its `start`/`stop` as `advertise`/
  `stop_advertise`, with no extra gating logic (per the acceptance
  criterion, `DaemonLifecycle.start()` already gates on a real bound
  port).
- `tests/aprilcam/daemon/test_rpc.py` (new) — `build_server`'s TCP
  bind success (including a real TCP client connecting), unix-bind
  preservation, and the bind-failure/non-fatal/no-`SO_REUSEPORT`-
  collision path.
- `tests/aprilcam/daemon/test_mdns.py` (new) — `MdnsAdvertiser`
  register/unregister, exact TXT key set, the `hostname` override
  (including a monkeypatched real-hostname check proving the override
  is actually used, not just accepted), registration/teardown failure
  swallowing via injected fakes raising `zeroconf.Error`/`OSError`,
  `stop()` before `start()` as a no-op, and `_primary_routable_ipv4`'s
  fallback path plus one real (unmocked) exercise of the UDP-socket
  trick.
- `tests/aprilcam/daemon/test_lifecycle.py` — extended `_FakeListener`
  with opt-in `tcp_port`/`events` parameters (defaults preserve every
  existing test's exact behavior and event sequences unchanged);
  extended `_make_lifecycle` with opt-in `with_advertise`/
  `listener_tcp_port`/`record_listener_events` parameters (same
  no-existing-test-affected guarantee); added four new tests covering
  "no TCP bound -> advertise never fires", the
  build_listener/listener.start()/advertise ordering, the
  stop_watch/stop_advertise/listener.stop()/on_stop ordering, and the
  "hooks unwired -> nothing fires" default.
- `tests/aprilcam/daemon/test_main.py` — two new tests verifying
  `build()`'s wiring specifically (tcp_port threaded to a real,
  connectable TCP bind; a fake `MdnsAdvertiser` — monkeypatched at
  `daemon_main.MdnsAdvertiser` — receives exactly one `start(port)`
  call with the real bound port and one `stop()` call, and receives no
  calls at all when `tcp_port=None`). A fake stands in for the real
  `MdnsAdvertiser` here deliberately: a real `Zeroconf()` registration
  measured ~1.6s wall-clock in this sandbox during manual testing and
  depends on the sandbox's multicast stack being usable at all —
  acceptable for `test_mdns.py`'s own targeted, injected-fake tests,
  but not worth the added runtime/flakiness risk for a wiring-only
  test in the always-run suite.

**Verification:**
- `uv run mypy` — clean, 78 source files, no errors, zero
  `typing.Protocol` anywhere touched.
- `uv run pytest -q -m "not camera and not integration"` — 1256
  passed, 49 deselected (1233 passing baseline noted in the ticket
  brief + 23 net new tests across the four files above; no
  regressions).
- Not verified (honestly out of scope for this ticket, not silently
  assumed passing): real mDNS discovery across a genuine second
  physical machine, and real multicast traversal across an actual LAN
  — both are sprint.md's documented Hardware Constraints, explicitly
  deferred to ticket 006's hardware-blocked documentation. This
  ticket's own coverage of `MdnsAdvertiser` is entirely
  dependency-injected (no real `Zeroconf` object is ever constructed
  in `test_mdns.py`'s failure-path tests) except for
  `_primary_routable_ipv4`'s one real, unmocked exercise of the actual
  UDP-socket trick and the manual ad hoc timing check mentioned above
  — neither depends on genuine multicast reachability, only on a local
  UDP/loopback stack being present, which this sandbox has.
