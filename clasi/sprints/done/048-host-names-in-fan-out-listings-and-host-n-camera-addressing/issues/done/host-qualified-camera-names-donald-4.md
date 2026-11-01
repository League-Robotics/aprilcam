---
status: done
sprint: 048
tickets:
- 048-001
- 048-003
- 048-004
- 048-005
---

# Fan-out listings show host names, and `HOST-N` addresses a camera on any daemon

## Description

Stakeholder request, 2026-11-01. `aprilcam camera list --all` labels each
daemon block with an address (`localhost (local):`, `192.168.1.23:5280:`,
`192.168.4.60:5280:`). The stakeholder wants the machine's name instead
(`gala (local):`, `buzzkill:`, `donald:`). They also want to name a
camera on any host as `HOST-N`, e.g. `donald-4`, without `-d`.

Current output from gala:

```
localhost (local):
  [1]    brio-501                   present ...
  [2]    arducam-ov9281-usb-camera  absent  ...
192.168.1.23:5280:
  [2]    arducam-ov9281-usb-camera  present ...
  [3]    hd-usb-camera              present ...
192.168.4.60:5280:
  [1]    hd-pro-webcam-c920         absent  ...
  [4]    arducam-ov9782-usb-camera  closed  ...
```

### Needed

- **Host names in fan-out labels.** Every `--all` command (`camera list`,
  `camera probe`, `camera image --all`, ...) labels each block with the
  daemon's short hostname: the TXT `host` property it already advertises
  (`socket.gethostname()`, trimmed of `.local` and any domain). The local
  daemon uses this machine's own short name, marked `(local)`. The
  address is not shown; `-v` can add it if it is ever needed.
- **`HOST-N` camera addresses.** Wherever a command takes a camera
  (`camera image|tags|view|close|config`, and the MCP tools' `camera`
  parameter), `donald-4` means camera `[4]` on the daemon named
  `donald`. **N is the bracketed persistent number, not the row's
  position.** Position changes when cameras come and go, and shared
  cameras already have the same number on every host.
- **Resolution rules (the names collide):** slugs contain hyphens and
  end in digits (`brio-501`, `hd-pro-webcam-c920`).
  1. An exact slug or number match on the targeted daemon wins.
  2. Otherwise, split at the last `-` into `HOST` and `N` (all digits).
     If `HOST` names a daemon, connect to it (mDNS browse, which maps
     names to addresses, then a direct `HOST` connect as fallback) and
     use camera `N`.
  3. Combining `HOST-N` with `-d` that names a different host is an
     error.
  4. An unknown host gives a clear error that lists the daemons found.
- Case-insensitive host match (`Donald-4` == `donald-4`).
- In the `--all` listing, each row can show its `HOST-N` address so
  the user can copy it.

### Notes

- Each daemon lists every camera it has ever registered, including absent
  ones (Donald lists the C920, #2 and #3 although only #4 is plugged in).
  This is out of scope here. A follow-up could hide absent cameras in
  `--all`.
- Code map: `cli/_common.py` `fan_out` (label), `client/discovery.py`
  `find_all` / `_BrowseResult.hostname` (already captured, dropped when
  building `DaemonInfo`), `client/interfaces.py` `DaemonInfo` (needs a
  `name` field), and CLI camera argument handling in `cli/camera.py`.
