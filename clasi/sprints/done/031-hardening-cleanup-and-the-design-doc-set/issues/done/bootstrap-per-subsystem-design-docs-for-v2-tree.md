---
status: done
sprint: '031'
tickets:
- 031-008
---

# Bootstrap per-subsystem DESIGN.md docs for the v2 tree

## Description

`clasi design validate` fails: every subsystem directory under
`src/aprilcam/` is missing its co-located `DESIGN.md`, and so is the
source root itself.

`docs/design/design.md`'s subsystem table already links to these files
(e.g. `[DESIGN.md](../../src/aprilcam/daemon/DESIGN.md)`) — the links
are dead. The project has `design_docs: enabled` in `.clasi/config.yaml`
but the `bootstrap-design` skill was never run against the v2 tree.

## Cause

Pre-existing, not caused by sprint 020: the v1 `DESIGN.md` files went
with the archived source tree (`archive/src/aprilcam/`), and the v2 tree
was rebuilt as interface skeletons without them.

Sprint 020 makes the gap more acute rather than causing it — `camera/`,
`client/`, `cli/`, `config/`, `daemon/`, and `proto/` now hold real
implementations, so there is finally something for a subsystem doc to
describe beyond the interface docstrings.

## Proposed fix

Run the `bootstrap-design` skill to produce `src/aprilcam/DESIGN.md`
plus one co-located `DESIGN.md` per subsystem, seeded from the already
written design set (`docs/design/design.md`, `specification.md`, the
interface skeletons, and the two interface-sufficiency traces) and from
the code sprint 020 delivered.

Subsystems with implementations (document from code): `camera/`,
`client/`, `cli/`, `config/`, `daemon/`, `proto/`.
Subsystems still interface-only (document the contract and say so):
`calibration/`, `playfield/`, `tags/`, `vision/`.

Verify with `clasi design validate` passing.

## Related

- `.clasi/config.yaml` — `design_docs: enabled`
- `docs/design/design.md` §3 subsystem table (the dead links)
- Sprint 020 — the sprint that populated the subsystems
