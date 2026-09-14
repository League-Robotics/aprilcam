---
id: '007'
title: Fix -d vidar.local hostname connect timeout (IPv6 link-local vs IPv4-only bind)
status: open
use-cases: []
depends-on: []
github-issue: ''
issue: remote-daemon-by-mdns-hostname-times-out-ipv6-link-local-vs-ipv4-only-bind.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix -d vidar.local hostname connect timeout (IPv6 link-local vs IPv4-only bind)

## Description

Found live during ticket 003 (see that ticket's Findings item 7, and
`clasi/issues/remote-daemon-by-mdns-hostname-times-out-ipv6-link-local-vs-ipv4-only-bind.md`).
From the Mac, `aprilcam camera list -d vidar.local` times out after the
full 5s `_DEFAULT_CONNECT_TIMEOUT`; the identical command with
`-d 192.168.1.144:5280` connects immediately. `aprilcam daemon list`
(mDNS service-browse) does discover the daemon correctly, but reports it
by `ip:port`, not hostname.

Root cause: the Mac's resolver returns vidar's IPv6 link-local addresses
before its IPv4 address for `vidar.local`
(`dscacheutil -q host -a name vidar.local`). `client/discovery.py` passes
the raw hostname straight to `grpc.insecure_channel(target)`, so gRPC's
C-core resolver picks an unreachable IPv6 link-local candidate and waits
out the full timeout instead of falling back to IPv4.

**Direction chosen: fix the client's address resolution, not the
daemon's bind.** `daemon/rpc.py`'s `build_server` binds
`0.0.0.0:<tcp_port>` (IPv4 only) as an explicit, recorded stakeholder
decision from sprint 028-003. Switching that to a dual-stack `[::]` bind
would reopen a decision this sprint has no new information to
relitigate — the reported symptom (a `.local` hostname connect timing
out) is fully addressed by making the *client* resolve and prefer a
reachable address, with no need to touch the daemon's binding behavior
at all. A client-side fix is also lower-risk: it changes one function in
one already-thin client module (`client/discovery.py`), rather than the
daemon's socket bind semantics that every other client (MCP server, CLI,
future consumers) depends on. If a future need for genuine IPv6
reachability to the daemon itself arises, that is a new decision for a
new sprint to make deliberately — not a side effect of fixing this
timeout. Do not change `daemon/rpc.py`'s bind address in this ticket.

## Acceptance Criteria

- [ ] `client/discovery.py`'s connection path resolves the target
      hostname itself (e.g. via `socket.getaddrinfo`) and prefers/tries an
      IPv4 (`AF_INET`) address before an IPv6 link-local candidate, so a
      `.local` hostname with a reachable IPv4 address connects promptly
      even when the resolver also returns unreachable IPv6 link-local
      addresses. A pure IPv4-first ordering is sufficient — a full
      Happy-Eyeballs-style race is not required unless it turns out to be
      the simpler implementation.
- [ ] `daemon/rpc.py`'s `build_server` bind address is unchanged
      (`0.0.0.0:<tcp_port>`, IPv4 only) — this ticket does not revisit the
      028-003 decision.
- [ ] A regression test exists for the resolution/ordering logic using a
      faked/mocked `getaddrinfo` result that returns IPv6 link-local
      candidates before an IPv4 candidate, asserting the client attempts
      or selects the IPv4 candidate — no real network or DNS dependency
      in the offline test.
- [ ] Verified live against `vidar.local`: `aprilcam camera list -d
      vidar.local` (and at least one other `-d vidar.local` command,
      e.g. `camera image`) connects and succeeds without the prior 5s
      timeout, quoted in this ticket (command + observed timing).
- [ ] `aprilcam daemon list` (mDNS browse, no `-d`) still works exactly as
      before — this ticket does not touch the mDNS browse path, only the
      direct-hostname connect path.
- [ ] Existing `client/discovery.py` tests and any TCP-connect tests still
      pass unmodified except where this ticket's new behavior requires an
      updated assertion.

## Implementation Plan

**Approach**: Read `client/discovery.py`'s current connect path (where
`grpc.insecure_channel(target)` is built from a raw hostname/`-d` value)
and `daemon/rpc.py`'s `build_server` to confirm the exact 028-003 bind
decision is recorded there before touching anything. Add a resolution
step ahead of channel construction: resolve the hostname via
`socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)`, filter/sort
candidates so an `AF_INET` result is tried first, and build the gRPC
target from the resolved IPv4 literal (falling back to the original
hostname/IPv6 behavior if no IPv4 candidate resolves, so non-`.local`
hostnames and pure-IPv6 hosts are not broken). Keep the change inside
`client/discovery.py`'s existing connection-building function(s) — no new
module, no interface change. Write the offline test first with a faked
`getaddrinfo` returning IPv6-then-IPv4 (matching the real
`dscacheutil` ordering ticket 003 observed), then implement until it
passes. Finish with the live verification against vidar (no code changes
needed there — this exercises the daemon started in ticket 003/006's
ad hoc style, or the fully installed one if ticket 004 has already run
by execution order).

**Files to modify**: `src/aprilcam/client/discovery.py` (or wherever the
raw hostname currently reaches `grpc.insecure_channel` — confirm exact
call site by reading the module; the issue file's evidence points there)
and its corresponding test file under `tests/`. No changes expected to
`daemon/rpc.py`, `daemon/mdns.py`, or any `interfaces.py` contract.

**State changes allowed on vidar.local** (`ssh ros@vidar.local`,
passwordless sudo):
- May run an ad hoc daemon from `/tmp` (same pattern as ticket 003 —
  `python -m aprilcam.daemon` in a `/tmp` venv with
  `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` under `/tmp`) purely to have
  something listening on TCP 5280 to connect to from the Mac. Does not
  require a camera bridge running — this ticket only exercises the
  connection path, not capture.
- Must **not** install or enable any systemd unit and must **not** change
  ROS's or the v1 units' enabled/active state — same constraint as ticket
  006, left exactly as ticket 003 left it for ticket 004.
- Must remove all throwaway `/tmp` state (venv, config, state dirs, any
  daemon process) at the end of the session.
- This ticket's live verification runs from the **Mac** side
  (`aprilcam camera list -d vidar.local`) against whatever ad hoc or
  already-installed daemon is reachable at the time — no camera hardware
  interaction is required, so camera i2c@88000's physical fault (ticket
  003 Finding 6) does not block this ticket.

**Testing plan**: `uv run pytest tests/aprilcam/client/ -k discovery -m
"not camera and not integration"` (adjust path to the actual test file
location) scoped to the touched module, run in the foreground. The live
verification has no pytest form; its result is recorded directly in this
ticket file.

**Documentation updates**: if `docs/wiki/robot-direct-api.md` or
`docs/design/use-cases/cli.md` document the exact resolution behavior of
`-d <host>.local`, add a short note that IPv4 is preferred when both
address families resolve. Otherwise, no documentation change is expected
beyond what ticket 005 already plans to fold in from this sprint's
findings.
