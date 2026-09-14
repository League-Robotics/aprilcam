---
status: done
sprint: '040'
tickets:
- 040-007
---

# `-d <host>.local` times out: client tries IPv6 link-local, daemon binds IPv4 only

Found live during sprint 040 ticket 003 (see that ticket's Findings item 7).

From the Mac, `aprilcam camera list -d vidar.local` fails after the full 5 s
`_DEFAULT_CONNECT_TIMEOUT` in `client/discovery.py`. The identical command
with `-d 192.168.1.144:5280` connects immediately, and every command works.
`aprilcam daemon list` (mDNS service browse) does discover the daemon, but
reports it by ip:port.

## Evidence

- On the Mac, `dscacheutil -q host -a name vidar.local` returns **IPv6
  link-local first** (`fe80:6::7ca:35b0:f731:6b4a`, `fe80:c::…`), then IPv4
  `192.168.1.144`.
- `daemon/rpc.py` `build_server` binds TCP with
  `server.add_insecure_port(f"0.0.0.0:{tcp_port}")`, which is IPv4 only. That
  is the 028-003 stakeholder decision recorded there.
- `client/discovery.py` builds `grpc.insecure_channel(target)` from the raw
  hostname, so gRPC's resolver picks an IPv6 link-local candidate. Nothing
  listens there, and the call waits out the timeout instead of falling back
  to IPv4.

## Why it matters

Connecting to a field Pi by its `.local` name is the documented primary UX
(`docs/design/use-cases/cli.md`, `docs/wiki/robot-direct-api.md`
`discovery.connect("pi-field.local")`). Sprint 040's success criteria use
exactly this form.

## Expected

`-d <name>.local` connects promptly whenever any resolved address reaches the
daemon. Candidate fixes, for the planner to choose from or combine:

- Bind dual-stack `[::]:5280`, keeping IPv4 working and failing soft when v6
  is unavailable.
- Have the client resolve the name itself and try IPv4 first or race
  candidates.

Whichever fix is chosen needs a regression test and live verification against
vidar.
