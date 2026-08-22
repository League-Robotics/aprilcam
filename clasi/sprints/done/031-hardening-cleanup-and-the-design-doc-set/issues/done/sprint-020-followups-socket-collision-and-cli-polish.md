---
status: done
sprint: '031'
tickets:
- 031-001
- 031-003
- 031-005
- 031-006
---

# Sprint 020 follow-ups: socket collision, CLI polish, deferred backends

## Description

Findings from sprint 020's end-to-end acceptance pass (ticket 020-007)
and its ticket handoffs. None blocked the sprint; all are real and worth
resolving.

### 1. v1 daemon shares the v2 control-plane socket path

An actively running **v1** daemon from a sibling checkout
(`/Volumes/Proj/proj/RobotProjects/AprilTags`) uses the same conventional
control-plane socket path as the v2 daemon. During acceptance testing
this produced one transient `Method not found!` / instant-zero-cameras
collision — a v2 client reached a v1 daemon and got a v1 answer.

This is a **deployment-path collision risk**, not a test artifact: any
machine running both trees hits it. Options: version the socket name,
namespace by tree, or have the daemon advertise its protocol version and
the client refuse a mismatch (the last is the honest one — a v2 client
talking to a v1 daemon should say so, not fail obscurely).

### 2. `camera image --base64 | head` raises BrokenPipeError

Piping the CLI's base64 output into a truncating consumer prints an
unhandled `BrokenPipeError` traceback. Standard Unix-tool behaviour is
to exit quietly. Needs the usual SIGPIPE/BrokenPipe handling at the CLI
boundary.

### 3. Deferred capture backends

- **libcamera**: `src/aprilcam/camera/backends/libcamera.py` implements
  `/sys` loopback enumeration only. The gst-mode capture backend is
  deferred — no Raspberry Pi hardware was available to validate against.
  Needed before a Pi-over-the-field deployment works.
- **screencap**: `archive/.../screencap.py` was not ported (lowest
  priority, no stub). Decide whether v2 wants it at all.

### 4. Camera slug is never renamed on reconnect

`CameraRegistry.resolve()` deliberately never renames a camera's
persisted slug even when a fresh identity resolution proposes a
different one (only `device_name`/`last_seen` refresh). This is the
conservative reading of UC-25's still-open "what if the name comes out
different" question. It is a **decision made by ticket 020-002, not a
question the use case closed** — confirm it is the behaviour wanted, or
specify the alternative.

## Verification

- Socket collision: run both trees' daemons on one machine; a v2 client
  must either reach the right daemon or report a version mismatch
  clearly.
- BrokenPipe: `aprilcam camera image <cam> --base64 | head -c 80` exits
  cleanly with no traceback.
- Slug rename: covered by whichever behaviour is chosen, with a test.

## Related

- Sprint 020 ticket 007 completion notes (full observed output)
- `docs/design/use-cases/cli.md` UC-25 (the open naming question)
