---
id: '003'
title: 'mDNS: never advertise loopback (daemon deferral and client filter)'
status: open
use-cases: ["SUC-003", "SUC-004"]
depends-on: []
github-issue: ''
issue: daemon-advertises-loopback-over-mdns-when-started-before-network.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mDNS: never advertise loopback (daemon deferral and client filter)

## Description

Implements both halves of the issue (sprint.md Architecture; overlay
`design/daemon-DESIGN.md` and `design/client-DESIGN.md`). Per CLAUDE.md,
skim `archive/src/aprilcam/daemon/mdns.py` and v1 discovery for earned
knowledge, but write fresh; the module keeps no `typing.Protocol` and no
base class.

Daemon, `src/aprilcam/daemon/mdns.py`:
- `_primary_routable_ipv4()` returns `None` (not `"127.0.0.1"`) when the
  UDP trick fails or yields a loopback address.
- `MdnsAdvertiser.__init__` gains injectable `address_lookup`
  (default `_primary_routable_ipv4`), `retry_interval` (default 5.0) and a
  `wait` callable (default `threading.Event.wait`-based) so tests drive
  time without sleeping.
- `start()`: if an address exists, register as today; else log once at INFO
  ("no routable IPv4 yet; deferring") and start a daemon thread that polls
  at `retry_interval`, registers when an address appears, and, once
  registered, unregisters and re-registers when the address changes
  (unregister old record first to avoid `NonUniqueNameException`).
  Registration failures stay non-fatal (WARNING).
- Log the advertised address(es) at INFO on every (re)registration.
- `stop()`: signal and join the thread (bounded), then unregister/close as
  today; safe if never started. Guard shared state with a lock.
- TXT keys stay exactly `version` and `host`; `start(tcp_port, hostname=)`
  signature unchanged so `daemon/__main__.py` is untouched.

Client, `src/aprilcam/client/discovery.py` `_browse_zeroconf`:
- From `info.parsed_addresses()` choose the first non-loopback address
  (`ipaddress.ip_address(a).is_loopback` false; tolerate unparsable
  entries). If none, drop the record and log at INFO. Extract the choice
  into a small pure helper so it is directly testable.

## Acceptance Criteria

- [ ] With address lookup returning `None`, `start()` registers nothing and
      does not raise; after the lookup returns an address, exactly one
      registration happens with that address.
- [ ] A later address change causes unregister + re-register with the new
      address; an unchanged address does not re-register.
- [ ] No registration ever carries a `127.x` address (including when the
      real lookup would have returned loopback).
- [ ] Advertised address logged (caplog).
- [ ] `stop()` ends the retry thread, including while still deferred, and
      unregisters if registered.
- [ ] Client: loopback-only record dropped; mixed record uses the
      non-loopback address; IPv6 `::1` treated as loopback.
- [ ] No real network or real sleeping in the new tests.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/test_mdns.py tests/aprilcam/client -q`
- **New tests to write**: extend `tests/aprilcam/daemon/test_mdns.py`
  (fake `zeroconf_factory`, fake address lookup, fake wait) for deferral,
  later registration, re-registration, stop-while-deferred, logging; extend
  `tests/aprilcam/client/test_discovery.py` for the address-choice helper
  and for `_browse_zeroconf` filtering via fake `ServiceInfo`s if the seam
  allows (otherwise test the helper only).
- **Verification command**: the existing-tests command above.
