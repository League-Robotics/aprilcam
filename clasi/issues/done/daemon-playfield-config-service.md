---
status: pending
---

**Superseded 2026-08-23.** This describes `SetPlayfield` validating and
persisting to `config_dir/playfields/<slug>.json` via an atomic write
-- contradicted by the later (2026-07-30), actually-implemented
decision in `docs/design/configuration.md` §5: RPC-pushed config is
in-memory only for both cameras and playfields, never written to a
file. A whole-project write-call-site audit
(2026-08-23, `clasi/issues/later/camera-config-clearing-a-field-does-not-persist-to-disk.md`'s
investigation) found zero daemon code paths writing to `config_dir` at
all. The source-of-record model is the human's repo file, pushed fresh
each session -- not the daemon writing anything back. Closing rather
than implementing as written.

# Playfield configuration service: whole-document Set/Get in the daemon

Per [specification.md](../../docs/design/specification.md) §2 principle 3,
§4.1, §4.5: the daemon is the rendezvous point (a Pi over the field,
remote clients collaborating through it), so playfield definitions must
be daemon-held state, not hand-placed files. Today the daemon only lists
playfield files that happen to exist under its config root
(`ListPlayfields`); there is no way to push one remotely.

## Scope

- `SetPlayfield(document)` — push a complete playfield definition (the
  existing `main-playfield.json` schema: dimensions, origin,
  `april_tags`, `aruco_tags`, `rectangles`, `dots`). The daemon
  validates, persists it under `config_dir/playfields/<slug>.json`
  (atomic write), and applies it (calibration ground truth, `where`
  lookups, field dimensions).
- `GetPlayfield(slug)` — serve the document back verbatim **plus derived
  info**: the cameras currently linked to it. Derived data is never
  stored into the source document.
- Whole documents only, both directions. No field-level edit RPCs —
  deliberately.
- Cardinality enforced/reported: a camera links to exactly one playfield
  (its config names it); a playfield has zero or more cameras.
- Feature-role semantics: ArUco + AprilTags are calibration ground
  truth; colored rectangles/dots are surveyed positions only, queryable
  via `where` ("give me the red square" answers from the definition,
  not vision).
- MCP tools: get/set playfield (whole document), list with camera links.
- CLI: push a local file to the daemon (source of record stays in the
  developer's repo).

Related: [[daemon-camera-config-service-reset]].
