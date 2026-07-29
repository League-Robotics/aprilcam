---
status: pending
---

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
