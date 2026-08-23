---
status: done
sprint: '020'
tickets:
- 020-001
- 020-002
- 020-003
- 020-004
- 020-005
- 020-006
- 020-007
---

# Daemon bring-up with basic camera CLI

## Description

Stand up the v2 daemon and CLI **together**, per the build-order
revision in `docs/design/development-plan.md` (2026-07-31, vertical
slices): this is the first v2 sprint, and it ends with something a
person can run from the shell.

**Daemon:**

- Lifecycle: starts, stops, reports status — flock singleton (refuses
  naming the holder), control-plane socket, clean shutdown. Starting
  with no cameras and no configuration is success (UC-01).
- Camera identity and the persistent registry: stable slugs across
  reboots/re-plugs, stable enumeration numbers that never reshuffle
  (UC-14, UC-25).
- Deep probe: re-interrogate all backends without disturbing live
  subscribers, returning a `ProbeReport` that names skipped/timed-out
  backends (UC-25).
- Raw frame capture: serve one frame per request, JPEG by default,
  lossless PNG on request (UC-17).

**CLI surface** (thin clients over the minimum client library —
`Discovery`/`connect`, `Daemon.list_cameras` / `probe_cameras` /
`get_frame` / `status`):

- `aprilcam daemon start|stop|status`
- `aprilcam camera list`
- `aprilcam camera probe`
- `aprilcam camera image`

**Ground rules:**

- Interfaces are already designed — implement against
  `src/aprilcam/daemon/interfaces.py`, `camera/interfaces.py`,
  `client/interfaces.py`, `types.py`, and `errors.py`. The interface
  sufficiency traces (`docs/design/use-cases/daemon-interface-trace.md`,
  `cli-interface-trace.md`) verify these cover the use cases; deviations
  from the declared interfaces are design changes, not implementation
  details.
- Convert v1 code from `archive/src/aprilcam/` where it serves: daemon
  lifecycle, camera identity/registry, capture backends.
- Errors are the typed taxonomy in `aprilcam/errors.py`, not message
  strings.

**Acceptance:** the scenarios of UC-14, UC-17, UC-25 (probe/hotplug
basics), and the local-lifecycle parts of UC-13 and UC-01, per
`docs/design/use-cases/`.

**Out of scope (explicitly):** detection, calibration, deskew,
playfields, annotations, the MCP server, and the viewer — later sprints
per the development plan's sprint sequence.
