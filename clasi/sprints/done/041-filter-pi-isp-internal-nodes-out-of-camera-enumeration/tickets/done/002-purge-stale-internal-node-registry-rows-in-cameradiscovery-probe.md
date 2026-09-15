---
id: '002'
title: Purge stale internal-node registry rows in CameraDiscovery.probe()
status: done
use-cases:
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: linux-enumeration-lists-pi-isp-nodes-as-cameras-with-duplicate-rows.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Purge stale internal-node registry rows in CameraDiscovery.probe()

## Description

Ticket 001 stops enumeration from ever proposing a Pi-internal V4L2
node as a camera candidate again, but it does nothing about rows
*already written* to `registry.json` on a host that hit the bug before
this sprint's fix shipped (e.g. vidar's `[3]..[12]` `rp1-cfe-*` rows).
Today, `CameraDiscovery.probe()` treats any registry record it doesn't
see in the current enumeration pass as a real camera that is merely
absent (`present=False`) — which is correct for a genuinely unplugged
camera, but leaves junk rows in the registry forever, still showing up
in `camera list`/`camera probe` output.

Per sprint.md's Architecture/Design Rationale, fix this in
`camera/discovery.py`, not `camera/registry.py`: `CameraDiscovery`
already imports both `camera/identity.py` and `camera/registry.py`, so
reusing ticket 001's node classifier from `probe()`'s existing "record
not seen in this pass" branch adds no new cross-module dependency. A
match should call the registry's existing `forget(slug)` instead of
reporting the record as an absent camera.

## Acceptance Criteria

- [x] `CameraDiscovery.probe()`'s loop over `self._registry.records()`
      (the branch handling a record whose hardware key was not seen in
      the current enumeration pass) checks the record's `device_name`
      against ticket 001's node classifier (imported from
      `camera/identity.py`) before deciding what to do with it.
- [x] A record that matches the classifier is forgotten via
      `self._registry.forget(record.slug)` and is **not** added to the
      `ProbeReport`'s `cameras` tuple at all (not even as
      `present=False`).
- [x] A record that does not match the classifier keeps today's exact
      behavior: reported `present=False`, `usable=False`, not forgotten.
- [x] Forgetting a junk record never changes another record's `number`
      (relies on `CameraRegistry.forget`'s existing "gaps are
      structural" contract — no change to `registry.py` itself).
- [x] No new import cycle or new module-level dependency is introduced
      — `discovery.py` already imports from both `identity.py` and
      `registry.py`; this ticket only adds a name to an existing import
      line.
- [x] Covered by an offline unit test that seeds a fake
      `CameraRegistry` (temp-dir `registry.json`) with a mix of
      classifier-matching junk records and real-camera records, runs
      `probe()` with an `identity_source` fixture that reports only the
      real cameras, and asserts: junk records are gone from the
      registry afterward, real records' numbers are unchanged, and the
      `ProbeReport` contains no entry for the junk records.

## Implementation Plan

**Approach**:

1. In `camera/discovery.py`, extend the `from aprilcam.camera.identity
   import CameraIdentity, resolve_all` line to also import ticket 001's
   classifier function.
2. In `probe()`, locate the loop `for record in
   self._registry.records(): if record.slug in
   seen_hardware_keys.values(): continue ...` — inside that loop, before
   constructing the `present=False` `CameraState`, call the classifier
   against `record.device_name` (this is the only field available on a
   `CameraRecord` describing what the node was — no live `/sys` path
   exists for a record that's no longer enumerated, so this call must
   work name-only, without requiring a driver-symlink read; confirm
   ticket 001's classifier signature supports being called this way, or
   expose a name-only variant if it does not).
3. On a match: call `self._registry.forget(record.slug)`, log at
   `info` level (mirroring the existing style of
   `_reconcile_location_duplicates`'s logging in `registry.py`), and
   `continue` without appending to `cameras`.
4. On no match: keep the existing `present=False` branch unchanged.
5. Update `probe()`'s docstring to describe the new third outcome for a
   registry record (present / absent-but-real / absent-and-purged).

**Files to modify**:
- `src/aprilcam/camera/discovery.py`

**Testing plan**:
- New test: registry seeded with 2 real + several junk records (mirror
  vidar's `[3]..[12]` shape), `probe()` called with an `identity_source`
  fixture returning only the real cameras' identities — assert the
  returned `ProbeReport.cameras` has exactly the real cameras, the
  registry's `records()` no longer includes the junk ones, and the real
  cameras' `number` values are unchanged from before the call.
- New test: a genuinely-unplugged real camera (not matching the
  classifier) is still reported `present=False` and remains in the
  registry — regression guard against over-purging.
- Run existing `discovery.py` test suite to confirm no behavior change
  for the already-covered present/held-open/absent paths.

**Documentation updates**: `probe()`'s docstring (see step 5 above). No
`docs/design/` overlay changes expected (see sprint.md's Architecture
section).

## Testing

- **Existing tests to run**: the full `camera/discovery.py` test file
  (locate under `tests/`), scoped per `.claude/rules/source-code.md`.
- **New tests to write**: as described in the Testing plan above.
- **Verification command**: `uv run pytest tests/camera/test_discovery.py -m "not camera and not integration"`
  (adjust path once located).
