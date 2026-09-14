---
id: '007'
title: Fix -d vidar.local hostname connect timeout (IPv6 link-local vs IPv4-only bind)
status: done
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

- [x] `client/discovery.py`'s connection path resolves the target
      hostname itself (e.g. via `socket.getaddrinfo`) and prefers/tries an
      IPv4 (`AF_INET`) address before an IPv6 link-local candidate, so a
      `.local` hostname with a reachable IPv4 address connects promptly
      even when the resolver also returns unreachable IPv6 link-local
      addresses. A pure IPv4-first ordering is sufficient — a full
      Happy-Eyeballs-style race is not required unless it turns out to be
      the simpler implementation.
- [x] `daemon/rpc.py`'s `build_server` bind address is unchanged
      (`0.0.0.0:<tcp_port>`, IPv4 only) — this ticket does not revisit the
      028-003 decision.
- [x] A regression test exists for the resolution/ordering logic using a
      faked/mocked `getaddrinfo` result that returns IPv6 link-local
      candidates before an IPv4 candidate, asserting the client attempts
      or selects the IPv4 candidate — no real network or DNS dependency
      in the offline test.
- [x] Verified live against `vidar.local`: `aprilcam camera list -d
      vidar.local` (and at least one other `-d vidar.local` command,
      e.g. `camera image`) connects and succeeds without the prior 5s
      timeout, quoted in this ticket (command + observed timing).
- [x] `aprilcam daemon list` (mDNS browse, no `-d`) still works exactly as
      before — this ticket does not touch the mDNS browse path, only the
      direct-hostname connect path.
- [x] Existing `client/discovery.py` tests and any TCP-connect tests still
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

## Findings / Verification

**Fix.** `client/discovery.py` gained two small helpers: `split_host_port`
(parses a `-d`-style value into `(name_or_literal, port)`, honouring
gRPC's bracketed-IPv6 syntax) and `resolve_ipv4_preferred` (resolves a
bare hostname via `socket.getaddrinfo`, returning the first `AF_INET`
candidate if one exists, else the first `AF_INET6` candidate, else
`None` for an IP literal or a genuine resolution failure). `Discovery.
_connect_remote` now builds its `grpc.insecure_channel` target through a
new `_resolve_grpc_target` that consults these helpers ahead of channel
construction; on `None` it falls through to the exact pre-040-007
formatting. `host`/`target`/`ConnectionFailed.host` are never touched —
only the literal address gRPC actually dials changes. `daemon/rpc.py`'s
`build_server` bind (`0.0.0.0:<tcp_port>`) is unmodified.

**Second caller found and fixed**: `client/streams.py`'s raw TCP stream
connect (`_connect`, used by `SocketImageStream`/`SocketTagStream`) hard-
coded `socket.socket(socket.AF_INET, ...)`, which cannot resolve an
IPv6-only host at all. Added `_resolve_tcp_endpoint`, using the same
`discovery.py` helpers via a call-time (not module-scope) import to avoid
a `daemon_client.py` ⇄ `streams.py` ⇄ `discovery.py` import cycle
(`daemon_client.py` already imports `streams.py`; `discovery.py` already
imports `daemon_client.py` for `GrpcDaemon`).

**Callers audited**: `-d` flag and `APRILCAM_DAEMON_HOST`
(`Discovery.connect` → `_connect_remote`, fixed above), mDNS-discovered
daemons (`find_all()`'s browsed entries carry a literal IP from zeroconf,
never a hostname — already unaffected, confirmed by `daemon list` below),
the MCP server's `daemon` param (`mcp/connection.py` calls
`Discovery.connect` — same fix applies), and stream transports
(`daemon/stream_transport.py`'s `_bind_tcp_socket` is a server-side
`0.0.0.0` bind, not a hostname connect — correctly out of scope;
`client/streams.py`'s client-side connect — fixed above).

**Offline tests**: `tests/aprilcam/client/test_discovery.py` — new
tests for `split_host_port` (bare hostname, hostname:port, IPv4/IPv6
literals bracketed and bare) and `resolve_ipv4_preferred` (IPv4-literal
→ `None`, IPv6-literal → `None`, v6-then-v4 resolver ordering → picks
v4, v4-only, v6-only → falls back to the v6 candidate, `getaddrinfo`
raising `socket.gaierror` → `None`, empty result → `None`), plus
end-to-end `Discovery.connect` tests with a faked `getaddrinfo`: dials
the resolved IPv4 address without ever touching a fake unreachable IPv6
literal (bounded well under `connect_timeout`), preserves the typed
`host` on `ConnectionFailed`, and a resolution failure still raises a
clean `ConnectionFailed` rather than an unhandled `socket.gaierror`.

Command: `uv run pytest tests/aprilcam/client/test_discovery.py
tests/aprilcam/client/test_streams.py -m "not camera and not
integration" -q` → `48 passed, 1 deselected`. Also ran the wider
`tests/aprilcam/client/`, `tests/aprilcam/cli/test_camera_cli.py`,
`tests/aprilcam/mcp/`, and `tests/test_no_cv2_in_client_modules.py`
under the same marker filter — all green, no regressions, `client/`
stays `cv2`-free.

**Live verification against `vidar.local`** (`ssh ros@vidar.local`, one
`ControlMaster` connection reused throughout, per the SIGSEGV-wedge
caution below):

- Confirmed the root cause first: `dscacheutil -q host -a name
  vidar.local` on the Mac returns `fe80:6::7ca:35b0:f731:6b4a` and
  `fe80:c::7ca:35b0:f731:6b4a` before `192.168.1.144` — the exact
  ordering this ticket targets.
- Built a wheel from this branch (`uv build --wheel`), copied it to
  `/tmp` on vidar, installed it in a throwaway venv
  (`/tmp/ac040007-venv`) with `pip install "<wheel>[daemon]"`.
- Ran `python -m aprilcam.daemon` by hand as `ros` with
  `APRILCAM_CONFIG_DIR=/tmp/ac040007-config` /
  `APRILCAM_STATE_DIR=/tmp/ac040007-state` — bound TCP 5280 on all
  interfaces as expected.
- Ran `deploy/aprilcam-camera-bridge` (the copy already installed at
  `/usr/local/bin/` on vidar, confirmed still the proven pipeline) by
  hand for camera i2c@80000 → `/dev/video71` (`cam -l` position 2,
  1-based → script argument `1`, 0-based): no SIGSEGV, pipeline
  produced frames normally for the whole session.
- **Before** (this ticket's changes `git stash`ed, same branch
  otherwise): `time uv run aprilcam camera list -d vidar.local` →
  `error: daemon at 'vidar.local' did not answer: no daemon answered`,
  **5.94s** (the full `_DEFAULT_CONNECT_TIMEOUT`).
- **After** (`git stash pop`): `time uv run aprilcam camera list -d
  vidar.local` → succeeded, listing both cameras, **0.79s**.
- `uv run aprilcam camera image -d vidar.local imx296-2 -o
  /tmp/vidar-cam2-040007.jpg` → `wrote 216612 bytes to
  /tmp/vidar-cam2-040007.jpg (jpeg)` in **0.76s** (real JPEG, verified
  non-empty).
- `uv run aprilcam daemon list` (no `-d`, mDNS browse) → unchanged
  behaviour: `localhost (local): ... ` plus `192.168.1.144:5280:
  cameras: 2, playfields: (none)` — still reported by `ip:port`, not
  hostname, exactly as before this ticket; **3.04s** total (mDNS browse
  window, not the connect path this ticket touches).
- Additionally verified `client/streams.py`'s fix directly (not
  explicitly required by the AC, but in the "check every caller"
  instruction): a short script called `Discovery().connect("vidar.
  local")` (0.09s) then `daemon.stream_images("imx296-2")` and read one
  real frame (1280x720, 216775 bytes) in 0.06s — proving the raw TCP
  stream socket also resolves and connects promptly by hostname now.

**SSH/libcamerasrc safety**: one `ControlMaster` connection was reused
for every command above. No SIGSEGV was observed in the bridge's log at
any point. Cleanup ran as three separate SSH calls after an unrelated
mid-session multiplexed-channel drop (immediately reconnected fine on
the very next call, confirmed unrelated to the camera pipeline — the
bridge and `gst-launch` were already dead by the time it happened).

**Vidar state at end of session**: no `aprilcam`/`gst-launch` processes
running; `/tmp/ac040007-*` and the copied wheel removed; `ros-cameras`
and `camera-watchdog.timer` confirmed `inactive`/`enabled` (untouched,
left exactly as ticket 003/006 left them for ticket 004). No systemd
unit installed or enabled by this ticket.
