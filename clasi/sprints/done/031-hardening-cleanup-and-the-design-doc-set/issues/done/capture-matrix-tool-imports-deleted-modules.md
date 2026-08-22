---
status: done
sprint: '031'
tickets:
- 031-007
---

# testdev/capture_matrix.py imports modules deleted in the v2 rebuild

## Description

`testdev/capture_matrix.py` fails to import. It is the tool that
**regenerates the image-matrix fixtures** in `tests/images/`, which
`docs/design/design.md` §8 describes as "regenerable, not sacred" — a
claim that is currently false, because the regenerator is broken.

Missing/renamed dependencies, found during sprint 021 ticket 001:

- `aprilcam.camera.camutil`
- `aprilcam.camera.camera_config`
- `aprilcam.camera.registry.dir_slug`
- `aprilcam.config.Config`
- `aprilcam.core.detector`

## Cause

The Stage 1 demolition and the v2 rebuild removed or restructured every
one of these; the dev tooling was moved across (from `archive/` to
`tests/testdev/` and then to repo-root `testdev/`) without being ported.

Not caused by any single sprint — it is fallout from the rebuild that
nothing has needed until now, because the existing 28 fixtures were
sufficient.

## Proposed fix

Port `capture_matrix.py` onto the v2 surface. The pieces it needs all
exist in new form: `camera/identity.py` + `camera/registry.py` for
identity and slugs, `camera/camera.py` + `camera/backends/` for capture
and settings, `config/directories.py` for roots, and
`vision/detector.py` if it wants detection annotations.

Worth deciding at the same time: should it drive the **daemon** over
RPC rather than opening cameras directly? It currently opens them
directly, which the design sanctions as an explicit exception for
offline test tooling (design.md §8 — it refuses to run when the daemon
holds a device). That exception is still defensible, but v2 now has a
daemon that can serve frames, so the alternative is worth a paragraph
of thought rather than an unexamined port.

## Verification

- `uv run python testdev/capture_matrix.py --help` runs.
- A capture run reproduces the `tests/images/<camera-slug>/` layout with
  a valid `manifest.json` recording effective controls, camera identity,
  and field-state id.
- Baseline controls are restored afterward, as the original did.

## Related

- `docs/design/design.md` §8 — the fixture matrix and its
  regenerability claim
- `tests/images/` — the 28 fixtures the tool produced
- `tests/images/field-state.json` — still `status: "unverified"` with
  empty tag lists; confirming what is physically on the field is
  related stakeholder work
- Sprint 021 ticket 001 — where the breakage was found and documented
