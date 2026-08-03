---
status: pending
---

# Hardening, cleanup, and the design-doc set

## Description

The accumulated debt sprint, and the one that decides whether this is a
system someone else can pick up. Each item below is already recorded
somewhere; this collects them so they get done rather than re-noticed.

### Reliability — things that will produce a confusing failure

- **v1/v2 control-socket collision.** A running v1 daemon from a
  sibling checkout shares the conventional socket path, so a v2 client
  can reach a v1 daemon and get `Method not found!`. Observed live in
  sprint 020. The honest fix is a protocol-version handshake: a v2
  client talking to a v1 daemon should *say so*, not fail obscurely.
- **Stale pidfile and orphaned daemons.** Observed 2026-08-03:
  `daemon.lock` held the live pid while `aprilcamd.pid` named a
  long-dead one, and an orphaned `aprilcam.daemon` process from hours
  earlier was still alive, not holding the lock, not serving. Anything
  trusting the pidfile targets the wrong process. Reconcile the two, and
  decide what start/stop do about an orphan.
- **`BrokenPipeError` on piped output.** `camera image --base64 | head`
  prints a traceback; standard Unix behaviour is to exit quietly.
  Needs SIGPIPE handling at the CLI boundary.
- **Deferred libcamera capture backend.** `camera/backends/libcamera.py`
  does `/sys` enumeration only; the gst-mode capture path was deferred
  for want of Pi hardware. **Required before any Pi-over-the-field
  deployment**, which is the deployment the architecture is shaped
  around.

### Usability — already cost real debugging time

- **`camera view` vs `playfield view` signposting.** `camera view` is
  raw by design (UC-16's "raw means raw" is a genuine diagnostic tool —
  it is how you tell a camera problem from a geometry problem). But the
  stakeholder hit this in practice, having expected deskew. Keep the
  behaviours; make `camera view` announce itself as RAW in its window
  title and startup line and point at `playfield view`. Recommended
  over auto-deskewing, which would destroy the diagnostic guarantee.

### Tooling and documentation

- **`testdev/capture_matrix.py` is broken** — imports five modules
  deleted in the v2 rebuild. `docs/design/design.md` §8 calls the image
  fixtures "regenerable, not sacred"; that claim is currently false.
  Also worth deciding whether it should drive the daemon over RPC now
  that one exists, rather than opening cameras directly.
- **`tests/images/field-state.json` is unverified** — `status:
  "unverified"` with empty tag lists since Stage 0. Nobody has recorded
  what is physically on the field.
- **Per-subsystem `DESIGN.md` bootstrap.** `clasi design validate`
  **fails**: every subsystem under `src/aprilcam/` is missing its
  co-located `DESIGN.md`, and `docs/design/design.md`'s subsystem table
  links to files that do not exist. The project has `design_docs:
  enabled`, so this is a standing failure, not a preference. Six
  subsystems now hold real implementations worth documenting.
- **Hardware-test contention.** Running the full suite together
  produces failures that vanish when hardware tests run alone — tests
  competing for the same cameras. Needs serialization or a fixture that
  arbitrates.
- **Stage 12 package restructure** — the dead-code sweep and directory
  moves the development plan deliberately deferred to last, "when the
  code that moves is final." It now is. Includes deciding the fate of
  `archive/`, which has served its purpose as an ideas source
  (`CLAUDE.md`'s archive rule) and is a large tree to carry forever.

### Open questions worth closing

- **Camera slug never renamed on reconnect** (sprint 021 decision) —
  confirm or specify the alternative; UC-25 left it open.
- **The robot "reference point" convention** for mount offsets (UC-12)
  — geometric centre, drive-axle centre, or client-defined. Offsets are
  meaningless without it. (May be resolved earlier by sprint 025.)
- **Annotation count/point limits** — unenforced by sprint 024's
  recorded decision; unbounded memory and per-frame wire cost if a
  client misbehaves.

## Cause

Normal accumulation across five sprints, most of it recorded honestly
at the time as deliberate deferral rather than discovered later. The
design-doc failure and the socket collision are the two that most
affect someone *other than* the original author picking this up.

## Verification

- `clasi design validate` passes.
- `uv run pytest` green in a single full run, hardware included.
- A v2 client meeting a v1 daemon reports a version mismatch clearly.
- `camera image --base64 | head` exits cleanly.
- `testdev/capture_matrix.py` regenerates the fixture set.
- `camera view` makes its raw-ness obvious on sight.

## Related

- `docs/design/development-plan.md` stage 12 and sprint 029
- Absorbs `sprint-020-followups-socket-collision-and-cli-polish.md`,
  `capture-matrix-tool-imports-deleted-modules.md`, and
  `bootstrap-per-subsystem-design-docs-for-v2-tree.md` — those three
  pending issues are this sprint's contents and should be linked to it
  rather than duplicated
