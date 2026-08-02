---
status: pending
split_from: config-service-completion-and-annotations.md
---

# The annotation store

## Description

**Split 2026-08-01 from `config-service-completion-and-annotations.md`**
— that issue's original "Part 2." Sprint 023 (config-service completion
+ calibration trust) planned that issue's Parts 1 and 3 only; this part
is independently valuable and has no dependency on that sprint's work,
so it is carved out for its own sprint (024) rather than inflating 023
to roughly 10-11 tickets spanning two structurally unrelated stories.

The daemon holds world-coordinate annotations; it never renders them.
Full design is already written — spec §9 decision 8, UC-28 in
`use-cases/daemon.md`, and the `Annotation`/`AnnotationSet` types in
`types.py` (already implemented — sprint 024 needs no rework there, only
the daemon-side store, RPCs, client rendering, and CLI). In short: keyed
`(playfield, layer, id)`, in-memory only (gone on restart, like the
mobile-tag registry), named layers with no ownership enforcement,
last-write-wins. `put_annotation`, `remove_annotation`, `replace_layer`
(atomic), `clear_annotations`, `get_annotations`. Deskewed frames embed
the current set with a monotonic revision; **raw frames carry none**
(annotations are world-only and a raw frame has no world→pixel mapping).

`client/renderer.py` already draws tags and has an annotation no-op
waiting to be filled in; `playfield view` should then show them live.
CLI: `playfield annotations` (list) and `--clear [--layer NAME]`.

## Cause

Sprints 020–022 built cameras, detection, calibration, deskew, and the
views — all single-client concerns. What is missing is the shared-state
layer that makes the daemon a rendezvous point for multiple services
communicating spatially (`docs/design/adapters.md` §2's worked example
for "held once and shared").

## Verification

- Draw a marker from one client; a second client sees it in
  `list_annotations` and in `playfield view`'s rendering.
- Restart the daemon; annotations are gone and the client re-draws.

## Related

- `docs/design/use-cases/daemon.md` UC-28
- `docs/design/specification.md` §9 decision 8 (daemon-held annotations)
- `docs/design/adapters.md` §2 — the "held once and shared" worked
  example this capability satisfies
- `config-service-completion-and-annotations.md` — the issue this was
  split from (sprint 023, Parts 1+3)
- Sprint 023's `sprint.md` Architecture section, "Sizing decision and
  recommended split" — the reasoning for carving this out
