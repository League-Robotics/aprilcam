---
id: '004'
title: Client-side real mDNS discovery (Discovery.find_all)
status: done
use-cases:
- SUC-004
- SUC-005
- SUC-006
depends-on:
- '003'
github-issue: ''
issue: tag-generation-and-daemon-discovery.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Client-side real mDNS discovery (Discovery.find_all)

**Depends on**: 003 (needs a real daemon-side advertiser to discover
against, both for the DI-seam design and for this ticket's own real
two-local-daemon integration test)
**Serves**: SUC-004, SUC-005, SUC-006 (UC-13) — this is the ticket
where multi-daemon discovery actually becomes real; SUC-007's fan-out
(ticket 005) depends on this working.

## Description

`src/aprilcam/client/discovery.py`'s `Discovery.find_all()` is
currently stubbed to local-only (its own module docstring, "Architecture
Decision 2 (sprint 020)"). Make it real:

- **Keep the existing local lock-probe unchanged.** It is the more
  precise, instant, network-independent answer for "is a daemon running
  on *this* machine," and the sprint architecture's Design Rationale
  (Step 6) explicitly decided against relying on mDNS for local
  detection too — dropping the lock-probe would make local detection
  spuriously dependent on the local multicast stack working at all,
  regressing `test_find_all_lists_a_reachable_local_daemon`'s
  already-tested "no network needed" property.
- **Add a real zeroconf browse** for everything else, merged with the
  lock-probe result.
- **Self-filter** the browse results so the local daemon's own mDNS
  advertisement (which it also picks up on its own multicast segment)
  is never double-counted alongside the lock-probe's `DaemonInfo`. The
  filter compares each browsed entry's TXT `host` property (ticket
  003's `MdnsAdvertiser`, `{b"version", b"host"}`) against this
  process's own `socket.gethostname()` — **not** IP address, since two
  daemons can share a subnet with NAT/multiple interfaces complicating
  IP comparison, and hostname is what ticket 003's advertiser already
  publishes for exactly this purpose. In real single-daemon-per-host
  deployment (guaranteed by the singleton flock — `daemon/lifecycle.py`
  — one daemon per host, always) hostname-equality is a correct,
  sufficient self-test. See "The same-host test-scenario problem"
  below for why this ticket's own integration test needs one more
  piece to make this work without a real second host.
- **A genuine browse failure raises `DiscoveryFailed`**, distinctly
  from "browsed and found nothing" (UC-13.S-5), mirroring the existing
  `lock_holder` failure path in `find_all()`
  (`except OSError as exc: raise DiscoveryFailed(...) from exc`,
  `src/aprilcam/client/discovery.py:199-202`) — apply the identical
  pattern to the new browse call.

**The same-host test-scenario problem.** This sprint verifies the
mechanism using two local daemon processes on alternate TCP ports
standing in for two hosts (Hardware Constraints, sprint.md) — but both
real processes share one real `socket.gethostname()` value, which would
make the hostname self-filter above incorrectly collapse the second
process's advertisement into "that's just my own local daemon." Ticket
003 added `MdnsAdvertiser.start(tcp_port, *, hostname=None)`
specifically so this ticket's integration test can advertise the second
process under a distinguishing synthetic hostname (e.g.
`"test-second-host"`) — use it. This is a test-only workaround for a
test-only topology limitation; production code paths are unaffected
(real deployments never have two daemons sharing one hostname).

**Real port, embedded in `DaemonInfo.host`.** `cli/_common.py`'s
`fan_out()` (and, by extension, ticket 005's `daemon list`) connects to
a discovered remote daemon via `discovery.connect(info.host)` — it
never reads `info.port` separately (confirmed by reading `fan_out`,
`src/aprilcam/cli/_common.py:169-217`). For `fan_out`/`connect` to keep
working **completely unmodified** on a discovered daemon that is not on
the default port 5280 (the two-local-daemon test's whole point), a
browsed `DaemonInfo`'s `host` field must itself carry the port in
`"ip:port"` form — exactly the format `Discovery.connect`'s existing
`_connect_remote` already parses (`":" in host` check,
`src/aprilcam/client/discovery.py:233-241`). Populate `DaemonInfo.port`
too (the field already exists), but the connect-ability contract runs
through `host`, matching this codebase's existing convention rather
than inventing a new one. This is a **deliberate, load-bearing
formatting choice** — document it in the module docstring so a future
reader does not "simplify" `host` back to a bare IP and silently break
remote fan-out to a non-default port.

## Files to touch

- `src/aprilcam/client/discovery.py` — real `find_all()`, `browse=`
  DI seam.
- `tests/aprilcam/client/test_discovery.py` (existing, 9 tests,
  extended — not replaced).

## Acceptance Criteria

- [x] `find_all()`'s existing local-lock-probe behavior is
      **byte-for-byte unchanged** for every existing test in
      `test_discovery.py` — none of the 9 existing tests needs to
      change to keep passing (only new tests are added).
- [x] SUC-004: with a local daemon (found via lock-probe) and a second,
      distinctly-hostnamed advertising daemon (found via browse — the
      two-local-daemon test stand-in), `find_all()` returns both: one
      `DaemonInfo(is_local=True, host="localhost", ...)`, one
      `DaemonInfo(is_local=False, host="<ip>:<port>", reachable=...)`
      for the browsed entry (`reachable` determined the same way the
      local branch already determines it — attempt a real connect/
      channel-ready probe, not merely "it advertised so it must be
      up").
- [x] The local daemon's own mDNS advertisement (same hostname as the
      client) never produces a **second, duplicate** entry alongside
      the lock-probe's local entry.
- [x] SUC-005: no daemon running or advertising anywhere → `find_all()`
      returns `()` — not an error (unchanged behavior, now also true
      when the browse step itself finds nothing).
- [x] SUC-006: the browse step itself raising (simulated via the
      `browse=` DI seam — e.g. an injected callable raising `OSError`)
      causes `find_all()` to raise `DiscoveryFailed`, **never** silently
      degrading to an empty or partial result. Add a test mirroring
      `test_find_all_raises_discovery_failed_distinctly_from_empty`
      (`tests/aprilcam/client/test_discovery.py:140-150`) but for the
      browse seam instead of `lock_holder`.
  - [x] `connect(host)` (explicitly-named `-d HOST`) is confirmed
        unaffected by this ticket — it does not call `find_all()` at
        all (already true today; add a one-line assertion/comment in a
        test if not already obviously covered, since SUC-006's own
        acceptance criteria call this out explicitly).
- [x] A held-but-unreachable local daemon combined with a distinct,
      reachable remote/browsed one: both appear, each with its own
      correct `reachable` value (extends the existing "listed, not
      filtered out" contract — `test_find_all_lists_a_held_but_
      unreachable_daemon` — to the multi-daemon case).
- [x] `Discovery.__init__` gains a `browse: Callable[[], <BrowseResultType>]`
      parameter, defaulting to a real zeroconf-backed implementation,
      overridable exactly like the existing `lock_holder`/
      `socket_reachable` parameters (`src/aprilcam/client/discovery.py:
      168-179`) — same DI style, same place in the constructor.
      `<BrowseResultType>` should be a small, local, easily-faked shape
      (e.g. a tuple of a small frozen dataclass with `address: str`,
      `port: int`, `hostname: str` fields) — **not** a raw zeroconf
      `ServiceInfo`/`Zeroconf` object — so every test but the one real
      integration test can inject plain data with no zeroconf import at
      all, mirroring how `lock_holder`/`socket_reachable` take plain
      `Path`s rather than daemon-internal types.
- [x] The real (default) `browse` implementation performs an actual
      zeroconf `ServiceBrowser` scan of `_aprilcam._tcp.local.` with a
      bounded wait (a fixed, short timeout — document the chosen value
      and why — so `find_all()` cannot hang indefinitely on a machine
      with no responders; this bound is itself worth a test: `find_all()`
      returns within a small multiple of the timeout when nothing
      answers).
- [x] One real, two-local-daemon-alternate-port integration test:
      start ticket 003's real daemon stack twice in-process (or as two
      subprocesses — implementer's call, matching how `daemon/
      __main__.py`'s own `build()` is already tested elsewhere in this
      codebase) on alternate TCP ports, the second one using
      `MdnsAdvertiser`'s `hostname=` override, and confirm a real
      `Discovery().find_all()` (default `browse`, real zeroconf, no DI
      fakes) reports both. This is the test that proves the *whole*
      mechanism end to end without a second physical machine (Hardware
      Constraints). Mark it clearly in a way that lets it be
      distinguished from the fast, fully-DI'd unit tests around it
      (e.g. a short real-multicast wait is expected and acceptable
      here, unlike everywhere else in this file).
- [x] `uv run mypy` clean. Zero `typing.Protocol`.
- [x] Full existing suite stays green, including all 9 pre-existing
      `test_discovery.py` tests unmodified.

## Completion Notes

Implemented exactly as planned. Key decisions and honest caveats:

- **The `browse=` DI seam resolves lazily, not by direct default
  binding.** `Discovery.__init__` takes `browse: Optional[Callable[...]]
  = None` and resolves `None` to `_browse_zeroconf` *inside* `__init__`
  (a bare-name lookup in this module's globals at call time), not as a
  literally-bound default-argument value. This was necessary, not
  cosmetic: with a directly-bound default (`browse: Callable[...] =
  _browse_zeroconf`), the 9 pre-existing tests — which construct
  `Discovery(directories=directories)` with no opinion about mDNS at
  all — would each open a real multicast socket and wait
  `_DEFAULT_BROWSE_TIMEOUT` (2s), violating this file's own "no real
  network" design intent and this ticket's "every test except the one
  integration test" instruction, and introducing real flakiness (a
  developer's own already-running `aprilcam daemon start` would leak
  into `test_find_all_returns_empty_tuple_when_no_daemon_running`'s
  result). The lazy-resolution form lets the shared `directories`
  fixture blanket-monkeypatch the module-level `_browse_zeroconf` name
  to a no-op for every test that doesn't explicitly pass its own
  `browse=` — production callers see zero difference (they never pass
  `browse=` either way, so they always get the real implementation).
  The one integration test deliberately does *not* use the
  `directories` fixture, for exactly this reason (see its own comment).
- **`_SERVICE_TYPE` is duplicated, not imported, from `daemon/mdns.py`.**
  Same call already made in this sprint for `tags/generator.py` vs.
  `vision/detector.py`'s family→dictionary map: the name is
  module-private on the daemon side, and `client/`/`daemon/` are drawn
  as independent `zeroconf` consumers in the sprint architecture's
  dependency diagram, not of each other. Cross-referenced in both
  places.
- **`_DEFAULT_BROWSE_TIMEOUT = 2.0`** — chosen and documented in
  `discovery.py`; validated empirically during implementation (a
  same-process advertise→browse round trip over real loopback multicast
  resolved in ~0.01–2s across repeated manual runs before this was
  written into the test suite).
- **Honest gap, matching the ticket's own "same-host test-scenario
  problem" framing**: the integration test proves the mechanism (real
  advertise, real bounded browse, real self-filter, real TCP
  reachability probe) using two real daemon *processes* on one physical
  machine, exactly as designed. It does **not** and cannot prove
  multicast traversal across a genuine LAN with a second physical host
  — that remains hardware-blocked per sprint.md and is ticket 006's
  documentation responsibility, not this ticket's.
- No `daemon/__main__.py` or other file outside this ticket's declared
  scope (`client/discovery.py`, `tests/aprilcam/client/test_discovery.py`)
  was touched. The integration test works around `build()`'s lack of a
  `hostname=` passthrough by monkeypatching `daemon_main.MdnsAdvertiser`
  to a thin wrapper that delegates to the real `MdnsAdvertiser` with the
  override baked in (mirroring `tests/aprilcam/daemon/test_main.py`'s
  own precedent of monkeypatching that exact name) — a test-only
  workaround, not a production code change.

**Verification performed**: `uv run pytest tests/aprilcam/client/test_discovery.py
-m "not integration"` (17 passed, ~5s); `uv run pytest
tests/aprilcam/client/test_discovery.py -m integration` (1 passed,
~6s); `uv run mypy` (clean, 78 files); `uv run pytest -q -m "not camera
and not integration"` (1264 passed — 1256 baseline + 8 new fast tests,
50 deselected, ~112s); `uv run pytest -q -m "not camera"` (the full
suite including every `@pytest.mark.integration` test, this ticket's
new one included: 1270 passed, 44 deselected [camera-only], ~171s) —
confirms no resource contention or flakiness between this ticket's real
daemon/mDNS integration test and the rest of the suite when run
together. `-m camera` was not run (no camera hardware attached to this
machine; unrelated to this ticket, matches the stated pre-existing
SIGABRT caveat for headless+hardware together).

## Implementation Plan

**Approach:**
1. Add the small `browse`-result shape (frozen dataclass, module-level
   in `client/discovery.py` or a tiny private helper type — it is an
   internal seam type, not a public interface, so it does not need an
   entry in `client/interfaces.py`).
2. Add `browse: Callable[[], Tuple[<shape>, ...]]` to `Discovery.
   __init__`, defaulting to a new module-level function (e.g.
   `_browse_zeroconf`) that does the real zeroconf `ServiceBrowser`
   scan and returns the parsed shape — this mirrors `probe_lock_holder`/
   `probe_socket_reachable`'s existing "module-level function is the
   default, constructor parameter is the override" pattern exactly.
3. Rewrite `find_all()`:
   - Run the existing lock-probe branch unchanged, building the local
     `DaemonInfo` exactly as today.
   - Call `self._browse()`, wrapped in the same `try/except OSError:
     raise DiscoveryFailed(...) from exc` pattern already used for
     `lock_holder`.
   - Filter out any browsed entry whose `hostname` equals
     `socket.gethostname()` (the client's own).
   - For each remaining entry, probe reachability (reuse
     `probe_socket_reachable`'s *shape* of check, adapted for a TCP
     target instead of a unix socket — a short `grpc.insecure_channel`
     + `channel_ready_future` probe against `f"{address}:{port}"`,
     mirroring `_connect_remote`'s own connect logic but as a
     probe-only, not a kept-open channel).
   - Build `DaemonInfo(host=f"{address}:{port}", port=port,
     is_local=False, reachable=<probed>)` for each.
   - Return the local entry (if any) followed by the remote entries, as
     one tuple.
4. Write the DI-fake-driven unit tests (mirroring `test_discovery.py`'s
   existing style), then the one real two-local-daemon integration
   test.

**Files to create/modify:**
- `src/aprilcam/client/discovery.py`
- `tests/aprilcam/client/test_discovery.py`

**Testing plan:**
- The bulk of the suite: fully headless, DI-injected `browse` fakes —
  no real network, mirroring the existing `lock_holder`/
  `socket_reachable` seam style exactly (`tests/aprilcam/client/
  test_discovery.py`'s own docstring already states this file's
  no-real-network intent; extend it, do not violate it, for every test
  except the one integration test).
- One real integration test using ticket 003's real `MdnsAdvertiser`/
  `build_server` stack, two processes/instances on alternate ports, the
  second under a synthetic hostname (see "The same-host test-scenario
  problem" above). This is the sprint's proof that advertise→browse→
  connect works end to end; it is explicitly *not* proof of real
  multi-host multicast traversal (Hardware Constraints item 2 —
  documented, not silently assumed, in ticket 006).

**Documentation updates:** `client/discovery.py`'s module docstring
currently states "Architecture Decision 2 (sprint 020): mDNS is
stubbed to local-only this sprint" as if still current — update it to
record that this ticket makes it real, keeping the historical
reasoning in place (why it was stubbed, why the lock-probe stays) per
this codebase's "record decisions in place" convention, and add the
`DaemonInfo.host`-carries-port-for-remote-entries convention explained
above so a future reader has the rationale, not just the code.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/client/
  test_discovery.py` (all 9 existing tests, unmodified) and the full
  suite.
- **New tests to write**: multi-daemon `find_all()` (local + remote,
  both reachable; local + remote-unreachable; remote-only); self-filter
  correctness (browsed entry matching local hostname is dropped);
  `DiscoveryFailed` from a broken `browse` seam; the timeout-bounded
  "browses and finds nothing" case; the one real two-local-daemon
  integration test.
- **Verification command**: `uv run pytest && uv run mypy`
