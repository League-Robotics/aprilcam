---
status: pending
---

# `camera config`/`playfield config` pushes are correctly ephemeral by design, but the CLI gives no warning — costly, repeated operator confusion

## This supersedes `camera-config-clearing-a-field-does-not-persist-to-disk.md`

That issue (four occurrences logged 2026-08-22/23) diagnosed this as a
**bug** — inconsistent, sometimes-persists-sometimes-doesn't disk
persistence. A full investigation (2026-08-23) found **there is no
bug**: RPC-pushed config (camera *and* playfield) is deliberately,
correctly, always in-memory-only. The apparent non-determinism was an
artifact of interleaving live pushes with manual disk-file edits across
one long debugging session, not real inconsistency in the code.

## Root cause of the *original* confusion (confirmed, not guessed)

`docs/design/configuration.md` §5 states the rule plainly: "configuration
pushed over RPC is in-memory only and discarded on restart... Does a
config push ever write to `/etc`? No — and it cannot, since the daemon
does not have write access there by design." A project-wide audit
(`grep -rn "write_text\|open(.*'w'\|\.dump(\|fh.write" src/aprilcam`)
found exactly 3 disk-write call sites in the whole package —
`camera/registry.py`, `daemon/service.py`'s `_persist_calibration`,
and one unrelated site in `cli/locate.py` — **none touch
`config_dir/cameras/<slug>/config.json` or `config_dir/playfields/`
at all.** `DaemonService.set_camera_config`
(`daemon/service.py:1018-1055`) does a whole-document in-memory dict
replace and nothing else; the `SetCameraConfig` RPC handler
(`daemon/rpc.py:927-934`) is a two-line synchronous pass-through with
no swallowed exception and nothing to race.

Live-reproduced (2026-08-23, no hardware touched, daemon left
unchanged): pushed a config for a synthetic, never-enumerated camera
name. CLI reported success, `--dump` showed the live value taking
effect — and zero bytes were ever written to disk, no directory even
created.

**Why it looked non-deterministic across four occurrences**: whichever
field happened to already hold the right value on disk (from an
earlier manual hand-edit, the documented workaround) looked "persisted
correctly"; whichever field hadn't been hand-edited yet looked "failed
to persist." No actual inconsistency in the code — the mental model of
"the push sometimes works" was wrong from the start.

## The real, remaining problem: a dangerous UX gap

`aprilcam camera config <file>` prints `pushed camera config 'X' --
playfield=Y` on success with **no indication this is a live-only
override**. An operator (this project's own maintainer, four times in
one night) reasonably reads "pushed" as "saved," configures a working
camera, and only discovers the setting is gone — silently, with no
warning — on the next daemon restart. This is exactly the kind of
"confusing failure" `docs/design/adapters.md`'s daemon-reliability
philosophy exists to prevent, just manifesting client-side instead of
daemon-side.

## Proposed fix

1. **Primary fix** — `src/aprilcam/cli/camera.py`'s `cmd_config`
   success message (~line 364-366): state explicitly that the push is
   a live override only, not persisted, and name the actual way to
   make it permanent: `aprilcam camera config <camera> --dump > FILE`,
   then copy `FILE` into `config_dir/cameras/<slug>/config.json`
   (UC-24). Tighten `cmd_config`'s docstring to lead with the ephemeral
   rule rather than bury it in prose the CLI never surfaces.
2. Apply the same fix to `playfield config`'s push path, if it has an
   equivalent success message (not yet checked — verify).
3. **Separate product decision, not a bug fix**: if auto-persisting a
   push is actually wanted, that changes the `/etc` read-only-by-daemon
   ownership model `configuration.md` §1 documents as deliberate — it
   would need to happen client-side (the CLI process, unlike the
   daemon, has no architectural reason to be locked out of a
   human-owned tree when run locally) and needs the stakeholder's
   explicit sign-off, not an implementation guess.

## Related

- Superseded: `camera-config-clearing-a-field-does-not-persist-to-disk.md`
  (renamed/replaced by this issue — kept for its four occurrences'
  evidence trail, now understood correctly per above).
- Also closed during this investigation as superseded by the same
  `configuration.md` §5 decision:
  `clasi/issues/done/daemon-camera-config-service-reset.md`,
  `clasi/issues/done/daemon-playfield-config-service.md` — both
  described an earlier design direction (daemon persists a pushed
  document to disk) that this later decision deliberately reversed.
- `docs/knowledge/2026-08-23-v1-known-good-arducam-ov9782-exposure-settings.md`
  — the practical workaround this issue's confusion led to (hand-edit
  the on-disk file directly) remains correct guidance regardless of
  this reframing.
