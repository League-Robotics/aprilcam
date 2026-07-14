---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 017 Use Cases

## SUC-001: `aprilcam view <code>` opens exactly the camera `aprilcam cameras` printed

Parent: UC (camera management / multi-host addressing — see overview.md
"Camera Management" and architecture-update-014.md "New `client/discovery.py`")

- **Actor**: Robotics developer running the CLI on a multi-daemon LAN (e.g.
  a Mac talking to a Pi's `aprilcamd` over mDNS/TCP discovery).
- **Preconditions**: `aprilcam probe` has been run at least once, so the host
  store (`hosts.json`) has entries for the local daemon and one or more
  remote daemons, each with enumerated cameras.
- **Main Flow**:
  1. The developer runs `aprilcam cameras` (no `--host`) and sees every
     camera on every known daemon, each printed with a host-qualified code.
  2. The developer copies one of the printed codes for a camera on a remote
     host and runs `aprilcam view <code>`.
  3. `view` parses the code with the same helper that produced it, resolving
     it to `(host, camera)`.
  4. `view` connects to the resolved host's daemon (reconnecting if the
     current connection is to a different daemon) and opens the resolved
     camera — not a bare index re-interpreted against the wrong daemon.
  5. The live view opens showing the intended camera's feed.
- **Postconditions**: Every code `aprilcam cameras` prints round-trips
  through `view`'s parser to the same `(host, camera)` pair the listing
  described. No code that `cameras` prints ever fails to resolve.
- **Acceptance Criteria**:
  - [ ] `aprilcam view <code>` succeeds for every code currently printed by
        `aprilcam cameras`, in both local-only and network (multi-host)
        listing modes.
  - [ ] A code addressing a remote camera always reconnects to that host's
        daemon before opening the camera (no silent local-daemon fallback).
  - [ ] `aprilcam view <code>` for a code whose host or camera no longer
        exists in the store produces a clear, actionable error (not a
        misleading "no camera numbered N on the daemon" pointing at the
        wrong daemon).

## SUC-002: `aprilcam cameras` prints one consistent code scheme everywhere

Parent: UC (camera management / multi-host addressing — see overview.md
"Camera Management")

- **Actor**: Robotics developer inspecting available cameras, locally or
  across the network.
- **Preconditions**: One or more daemons are reachable; the host store may
  or may not be populated.
- **Main Flow**:
  1. The developer runs `aprilcam cameras` with no `--host` (network/store
     listing) and separately with `--host <X>` (single live daemon listing).
  2. Both code paths format camera codes using the same shared helper.
  3. The codes shown in both modes use the identical scheme (letter for
     local, host-letter + camera-letter for remote) and the identical
     letter-to-host / letter-to-camera mapping the store defines.
  4. The printed usage hint (`Open one with: aprilcam view <code>`) shows an
     example in the same scheme and both examples work if pasted verbatim.
- **Postconditions**: There is exactly one code format a user ever sees from
  `aprilcam cameras`, regardless of which listing path executed.
- **Acceptance Criteria**:
  - [ ] The network-listing path (`_list_all_from_store`) and the
        single-daemon path (`_list_single_daemon`) format codes via the same
        function, with identical output for the same store entry.
  - [ ] The printed help/usage examples use the canonical scheme and are
        directly copy-pasteable into `aprilcam view`.

## SUC-003: Codes and their parsing/formatting logic cannot drift apart

Parent: UC (camera management / multi-host addressing — see overview.md
"Camera Management")

- **Actor**: AprilCam maintainer / future contributor changing camera
  listing or view-resolution code.
- **Preconditions**: The codebase has a single canonical camera-code
  format+parse helper, used by both the producer (`cameras`) and consumer
  (`view`, and any other command accepting a camera code, e.g. `tags`).
- **Main Flow**:
  1. A contributor changes how codes are generated or parsed.
  2. Because there is one shared helper (not two divergent
     implementations), the change is made in one place and automatically
     applies to every producer and consumer.
  3. A round-trip test suite generates codes for a variety of store shapes
     (local-only, single remote, multiple remotes, host numbers requiring
     two-letter host codes) and asserts every emitted code parses back to
     the exact `(host, camera)` pair it was generated from.
- **Postconditions**: There is no code path left that implements its own
  regex or ad hoc parsing of host-qualified camera codes outside the shared
  helper.
- **Acceptance Criteria**:
  - [ ] `resolve_host_letter_number` (or any divergent letter+digit parser)
        is removed or reconciled into the single canonical helper — no two
        implementations of code parsing coexist.
  - [ ] A test suite round-trips every code emitted by the `cameras`
        listing functions back through the canonical parser for a matrix of
        store shapes (0, 1, and 2+ remote hosts; local-only).
  - [ ] The canonical parser validates that the resolved camera actually
        exists on the resolved host before returning a match (no
        unchecked/unvalidated digit substrings).
