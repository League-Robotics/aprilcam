---
status: pending
---

**Superseded 2026-08-23.** This describes camera config persisting to
disk "through this sanctioned Set proxy" — contradicted by the later
(2026-07-30), actually-implemented decision in
`docs/design/configuration.md` §5: RPC-pushed config (camera *and*
playfield) is in-memory only, never written to a file, by design.
`/etc` is not writable by the daemon at all. Making a push permanent is
`camera config --dump` plus a file copy (UC-24), not an automatic
write. Closing rather than implementing as written; the `Reset`
half (re-apply a stored document to hardware) may still be worth its
own issue if wanted, scoped against the real, current design.

# Camera configuration service: whole-document Set/Get plus Reset

Per [specification.md](../../docs/design/specification.md) §4.1: camera
configuration follows the same whole-document push/serve pattern as
playfields. `SetCameraConfig`/`GetCameraConfig` RPCs already exist as
JSON-blob proxies; this issue formalizes the contract and adds reset.

## Scope

- Document contract: camera kind, gains/shutter/brightness and other
  UVC controls, resolution, playfield link, `camera_position` (mount
  height/offset for parallax). The daemon introspects what it can from
  the hardware (dimensions, device name) and reports it; the pushed
  document owns everything adjustable.
- `ResetCameraConfig(cam_name)` — re-apply the stored config document to
  the hardware (UVC settings re-push, resolution, etc.) without
  restarting the daemon or reopening the camera unless required.
- Served camera config reports which playfield the camera is on
  (derived link, consistent with the playfield document reporting its
  cameras — see [[daemon-playfield-config-service]]).
- Whole documents only: clients get and send complete files, never
  fragments.
- Existing invariant preserved: the daemon pipeline *reads* config.json
  but never writes it except through this sanctioned Set proxy
  (`camera_config.py` boundary rule).
- MCP tools updated to the whole-document contract; CLI support for
  pushing a local config file.
