---
status: in-progress
sprint: 049
tickets:
- 049-003
---

# Daemon advertises 127.0.0.1 over mDNS when started before the network is up

## Description

If `aprilcamd` registers its mDNS service before the host has a routable
IPv4 address, it advertises `127.0.0.1`. Remote clients then dial their
own loopback, reach their **local** daemon, and list it under the remote
host's name.

### Seen on angst, 2026-11-04 (Wi-Fi only, no cable)

- 01:28:16 `aprilcamd` started (`After=network-online.target` did not
  hold it back long enough)
- 01:28:19 `wlo1` gained carrier
- 01:28:21 mDNS registered `aprilcam-angst._aprilcam._tcp.local.`

`_primary_routable_ipv4()` in `src/aprilcam/daemon/mdns.py` found no
route and fell back to `"127.0.0.1"`, and that address was published.
On gala, `_browse_zeroconf()` returned
`_BrowseResult(address='127.0.0.1', port=5280, hostname='angst')`, and
`aprilcam camera list --all` showed `angst (127.0.0.1:5280)` with gala's
own Brio 501 and Arducam 12MP. Restarting `aprilcamd` on angst fixed it,
but any reboot can bring it back.

The mDNS log line doesn't include the advertised address, which made
this harder to spot.

### Fix ideas

1. **Daemon:** never advertise a loopback address. Defer or retry
   registration until a routable IPv4 exists, and re-register when the
   host's addresses change (zeroconf can advertise every interface
   address, not just one). Log the advertised address(es).
2. **Client:** in `_browse_zeroconf()` (`src/aprilcam/client/discovery.py`),
   drop a remote record whose address is loopback rather than dialing
   it. Dialing it always reaches the wrong daemon and mislabels it.

### Workaround

`sudo systemctl restart aprilcamd` on the affected host once its network
is up.
