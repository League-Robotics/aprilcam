---
status: in-progress
sprint: '017'
tickets:
- 017-001
- 017-002
- 017-003
- 017-004
---

# aprilcam view cannot parse the B6-style host-qualified camera codes that aprilcam cameras prints

## Summary

`aprilcam cameras` lists cameras across hosts using letter+digit codes
(e.g. `B6` = camera 6 on host B) and its own help text says to run
`aprilcam view B6`. But `aprilcam view B6` fails with `Error: no camera
numbered 6 on the daemon` — the host-letter prefix is dropped and the
bare index `6` is looked up on the **local** daemon instead of routing
to host B's daemon. The command advertises a syntax it then rejects.

## Reproduction

```
$ aprilcam cameras
  gala  [A]  (local)
      1     Brio 501
      3     Arducam OV9782 USB Camera
  vidar  [B]
      B0    HD Pro Webcam C920
      B6    imx296-88000
      B7    imx296-80000
Open one with:  aprilcam view <code>
        'aprilcam view B6'  (camera 6 on host B)
$ aprilcam view B6
Error: no camera numbered 6 on the daemon. Run 'aprilcam cameras' to list them.
```

## Root cause (confirmed by read-only code investigation)

The CLI has **two divergent, coexisting host-code schemes** that are
never reconciled by a shared parse/format helper:

1. **letter+letter** scheme (`A`, `FB`) — canonical in
   `client/host_codes.py` (`code_for` / `resolve_code`, which actually
   validates the camera exists on the host).
2. **letter+digit** scheme (`B6`) — used by the network listing in
   `cli/cameras_cli.py:79-83` (`_list_all_from_store`, where the digit
   is the camera's `enum` and the letter is `num_to_alpha(host_num)`)
   and parsed by `resolve_host_letter_number` in `cli/_daemon.py:192-251`.

The producer and consumer of the `B6` code disagree:

- **Producer** (`cameras_cli.py:83`): `code = f"{h_letter}{num}"` where
  letter = `num_to_alpha(host["num"])`, digit = camera `enum`.
- **Consumer** (`_daemon.py:223-251`): regex `([A-Za-z])(\d+)` (single
  leading letter only), re-derives the host via `alpha_to_num(letter)`,
  looks up **only** the host (`num == host_num`), and returns the raw
  digit substring **without** verifying that camera exists on that host
  and **without** translating store `num` ↔ daemon `enum`.
- A `local`-kind shortcut at `_daemon.py:242-243` returns
  `(number, None)` (no reconnect) when the resolved host entry is local
  — so if the letter→host mapping drifts or resolves to the local entry,
  `view` never reconnects to host B and queries bare `6` on the local
  daemon → the reported error (raised at `view_cli.py:475-479`).

Additionally the single-daemon listing (`cameras_cli.py:150-159`) uses
`code_for` (letter+letter), so `aprilcam cameras` itself prints
different code styles depending on local-only vs network mode — an
internal inconsistency.

## Key locations

- `view` dispatch + parse: `cli/view_cli.py:421-491` (two-stage:
  `resolve_host_letter_number` at 427-441, `resolve_camera_code` at
  443-462, bare-int fallback at 463-491); error at `view_cli.py:475-479`.
- `B6` producer: `cli/cameras_cli.py:79-83` (network) vs `150-159`
  (single daemon, uses `code_for`).
- `B6` consumer (defective): `cli/_daemon.py:192-251` (regex 223,
  host-only lookup 233-235, local shortcut 242-243, unchecked digit at
  243/251).
- letter+letter parser: `cli/_daemon.py:131-189` →
  `client/host_codes.py:130-195`.
- Host `num` assignment / num-vs-enum: `client/host_codes.py:338-479`.
- Daemon target precedence: `client/discovery.py:158-224`.

## Expected

`aprilcam view <code>` must accept exactly the codes `aprilcam cameras`
prints, resolve the host-letter prefix to the correct remote daemon,
translate the code's index to the right camera on that host, and connect
there. The producer and consumer of host-qualified codes should share
**one** canonical parse/format helper so they cannot drift. `aprilcam
cameras` should print a single consistent code scheme in both local-only
and network modes.

## Fix direction (for planning, not prescriptive)

Unify on a single host-qualified code scheme with one shared
format+parse helper used by **both** `cameras` (all listing paths) and
`view`. Ensure the parser validates the camera against the resolved host
and routes to that host's daemon (reusing the existing
`resolve_daemon_target` / `--daemon-host` precedence), and fix the
local-kind shortcut so a genuine remote code always reconnects. Decide
whether the canonical scheme is letter+letter (`code_for`, already
validates) or letter+digit (matches the current network listing / help
text) — pick one and make everything conform. Add tests that round-trip
every code `cameras` emits back through `view`'s parser.
