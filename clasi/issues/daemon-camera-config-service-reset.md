---
status: pending
---

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
