---
id: '009'
title: Add mDNS advertisement via zeroconf
status: done
use-cases:
  - SUC-007
depends-on:
  - '006'
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add mDNS advertisement via zeroconf

## Description

When the daemon starts with TCP transport enabled, register a
`_aprilcam._tcp.local.` mDNS/Bonjour service record using the `zeroconf` library.
This allows clients on the same LAN to discover the daemon without manual IP
configuration. On daemon shutdown (or when `--no-tcp` is specified), the record is
unregistered.

The `zeroconf` dependency was added to `pyproject.toml` in ticket 002.

## Acceptance Criteria

- [ ] When `tcp_enabled=True`, daemon registers `_aprilcam._tcp.local.` with the
      correct TCP port at startup.
- [ ] When `tcp_enabled=False` (`--no-tcp`), no mDNS registration occurs.
- [ ] On daemon shutdown, the mDNS record is unregistered cleanly; no stale records.
- [ ] mDNS registration failure does not crash the daemon (log warning, continue).
- [ ] `uv run pytest` passes.

## Implementation Plan

### Approach

1. Create `src/aprilcam/daemon/mdns.py` with a `MdnsAdvertiser` class:
   - `start(service_name, tcp_port, host_name)`: register the service via
     `zeroconf.Zeroconf` and `zeroconf.ServiceInfo`.
   - `stop()`: call `zeroconf_instance.unregister_service(info)` then
     `zeroconf_instance.close()`.
   - Wraps all zeroconf calls in try/except; logs warnings on failure.

2. In `DaemonServer.run()` (server.py):
   - After starting the gRPC server successfully, if `tcp_enabled=True`, construct
     `MdnsAdvertiser` and call `start(...)`.
   - On shutdown, call `advertiser.stop()` before releasing the pidfile.

3. Service info:
   - `type_`: `"_aprilcam._tcp.local."`
   - `name`: `f"aprilcam-{hostname}._aprilcam._tcp.local."`
   - `port`: the actual TCP port
   - `properties`: `{b"version": version_bytes}`
   - `addresses`: list of local IP addresses obtained via `socket.gethostbyname(hostname)`

### Files to Create / Modify

- `src/aprilcam/daemon/mdns.py` — new
- `src/aprilcam/daemon/server.py` — call `MdnsAdvertiser` when tcp_enabled

### Testing Plan

- Unit test `MdnsAdvertiser.start()` with a mock `Zeroconf`; verify `register_service`
  is called with the correct type string and port.
- Unit test `MdnsAdvertiser.stop()` verifies `unregister_service` and `close` are called.
- Unit test: if `zeroconf.Zeroconf()` raises, `MdnsAdvertiser.start()` logs a warning
  rather than propagating the exception.
- Run `uv run pytest`.
