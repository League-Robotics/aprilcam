---
status: pending
---

# Cameras must be referenceable by their persistent number

## Description

A camera's enumeration number should work anywhere a camera name works.
Today it does not:

```
$ uv run aprilcam camera image 3 > frame.png
error: unknown camera '3'; available: brio-501, hd-usb-camera,
       arducam-ov9782-usb-camera, usb2-video
```

`camera list` prints the numbers, so the number is the obvious short
handle to type — and typing it fails. Anywhere a camera is addressed
(`camera image`, `camera list`, and every future camera-addressed
command, plus the MCP tools when they land) a decimal number must
resolve to the camera holding that number.

**Numbering requirements** (all already satisfied by the stored data —
see Cause):

- Numbers **persist** across daemon restarts and machine reboots.
- Unplug and re-plug returns the **same** number, even in a different
  USB port.
- Numbers are **never reissued or reshuffled**, so a listing may show
  gaps (1, 2, 5) after a camera is forgotten. Gaps are correct, not a
  bug to compact away.
- Two cameras with the **same model name** stay distinct.

## Cause

The persistence and identity half of this is **already built and
working** (sprint 020, tickets 002/003) — the gap is only that nothing
resolves a number *as* a camera reference.

`~/.local/share/aprilcam/cameras/registry.json` on this machine today:

```json
"avf:0x21141400c456366": {
  "device_name": "Arducam OV9782 USB Camera",
  "hardware_key": "avf:0x21141400c456366",
  "number": 3,
  "slug": "arducam-ov9782-usb-camera"
},
...
"next_number": 5
```

Which means:

- Records are keyed on `hardware_key` — the AVFoundation unique id, or
  libcamera id / serial / `vid:pid@location` further down the fallback
  chain — **not** on the port or the name. That is what makes re-plug
  return the same record, and what keeps two same-model cameras apart
  (each has its own `hardware_key`; a name collision only affects the
  *slug*, which gets a numeric suffix, never the identity).
- `number` is assigned once on first sight and persisted; reconnect
  refreshes `device_name`/`last_seen` and deliberately keeps the
  existing `slug` and `number`.
- `next_number` is monotonic and, on load, clamped above every stored
  number so a corrupt or missing counter can never re-issue a number
  already in use — gaps are structural, exactly as required.

The failure is purely at the addressing layer:
`src/aprilcam/cli/camera.py` passes `args.camera` straight through, and
`DaemonService._camera_for(slug)`
(`src/aprilcam/daemon/service.py:257`) matches on slug only, raising
`UnknownCamera` for anything else.

## Proposed fix

**Resolve in the daemon, not in each adapter.** `_camera_for` should
accept either form: an all-digits argument resolves by `number`,
anything else by slug. Recommended over client-side resolution because
`docs/design/adapters.md` §2's test (b) — "would two adapters otherwise
duplicate it" — is exactly met here: the CLI, the MCP server, the future
ROS node, and robot programs would each have to reimplement the same
lookup, and any drift between them becomes a wrong camera rather than an
error. One daemon-side rule, every adapter inherits it.

Details to get right:

- **A number that matches nothing** raises `UnknownCamera` like any
  other unknown reference, and the "available" list should show numbers
  alongside slugs (`3 arducam-ov9782-usb-camera`) so the error teaches
  the addressing scheme instead of just rejecting it.
- **Absent cameras are still addressable by number** — a registry record
  outliving its hardware is normal (UC-25), so `camera image 3` on an
  unplugged camera must fail with "camera present: no", not "unknown
  camera 3".
- **No ambiguity is possible** between the two forms: slugs are derived
  from device names and always contain at least one non-digit, so an
  all-digits token is unambiguously a number. Confirm this holds for the
  last-resort slug in `camera/identity.py` and state it as an invariant
  where the slug is built, so a future slug scheme can't silently break
  addressing.
- **Document the scheme** in `docs/design/use-cases/cli.md`'s addressing
  conventions and in the packaged guide (UC-27) — an agent that only
  sees slugs will never discover numbers exist.

## Verification

- `aprilcam camera image 3 > frame.png` writes a real frame from the
  camera listed as 3.
- `aprilcam camera image 3` and `aprilcam camera image
  arducam-ov9782-usb-camera` produce frames from the same camera.
- Stop the daemon, restart, re-run: same number, same camera.
- Physically unplug that camera, re-plug it (ideally in a *different*
  USB port), re-run: same number. (This also closes the sprint 020
  acceptance leg that could not be verified without hands on hardware —
  UC-25.S-5.)
- Forget a mid-list camera, re-probe: remaining numbers unchanged, gap
  left in place, new camera gets the next unused number.
- `aprilcam camera image 99` errors naming the available cameras with
  their numbers.

## Related

- `src/aprilcam/camera/registry.py` — `CameraRecord.number`,
  `next_number` clamping, reconnect-keeps-number path
- `src/aprilcam/camera/identity.py` — `hardware_key` fallback chain
  (the reason re-plug is stable)
- `src/aprilcam/daemon/service.py:257` — `_camera_for`, where slug-only
  matching lives
- `docs/design/use-cases/cli.md` UC-14 (camera listing, including the
  still-open naming question), UC-25 (probe/hotplug)
- Sprint 020 — delivered the persistent numbering this issue exposes
