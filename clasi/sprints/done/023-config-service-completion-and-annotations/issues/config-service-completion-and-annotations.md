---
status: in-progress
sprint: '023'
split_into:
- annotation-store-for-shared-spatial-state.md
tickets:
- 023-001
- 023-002
- 023-003
- 023-004
- 023-005
- 023-006
- 023-007
---
## Description

The v2 vertical slice that makes the system **operationally solid
across restarts** and makes calibration trustworthy over time.
Development-plan stage 9, plus UC-04's acceptance-threshold and
staleness questions.

**Split 2026-08-01:** this issue originally also covered the shared
annotation store (UC-28, spec §9 decision 8) as "Part 2." That work is
split out to `annotation-store-for-shared-spatial-state.md` for its own
sprint (024) — sprint 023's planning found it independently valuable,
with no dependency on Parts 1 or 3 below, and splitting kept sprint
023's ticket count and architectural story coherent (Parts 1 and 3 are
both about *trustworthiness over time* on already-built subsystems; the
annotation store is a *new capability*). Part numbering below is kept
as-is (Part 1, Part 3) rather than renumbered, for continuity with
sprint 023's planning artifacts, which already reference these numbers
throughout.

### Part 1 — Configuration that survives a restart (the headline)

Sprint 022 exposed a real trap, hit live during verification:

> Stop the daemon, start it again, and the camera is still `calibrated`
> — but the playfield definition and the camera→playfield link are
> **gone**, because pushed config is in-memory by design. So
> `calibrated=True` alone does not mean a deskewed request will work,
> and `aprilcam camera calibrate 3` fails with "no playfield link"
> until you re-push two documents by hand.

This is UC-01's "disk is the source of record, RPC push is an ephemeral
override" rule working exactly as specified — the rule is right and
must not be weakened. **The missing half is that the daemon does not
yet read the source of record at startup.** `config_dir` holds
`cameras/<cam>/config.json` and `playfields/<slug>.json`; nothing loads
them. Fix that and the whole trap dissolves: install your documents
once, and every restart comes back ready.

Also in scope:

- **Hardware settings actually applied.** Sprint 022 shipped a minimal
  `SetCameraConfig` carrying only `name` + `playfield`; `settings`
  round-trips but is never pushed to the camera. Complete it —
  exposure, gain, white balance, resolution, UVC controls — with
  UC-03's contract: **unsupported settings are hard errors that fail
  the whole push** and leave the camera exactly as it was, never
  half-configured, never blind success. Control **ordering** matters:
  auto-exposure off before an exposure value sticks, auto-white-balance
  off before WB temperature. The OV9782-class cameras need manual low
  exposure this way; that is config, not code.
- **`ResetCameraConfig`** — re-apply the stored on-disk config,
  discarding live overrides. This is how an experiment ends without a
  daemon restart.
- **`camera config --dump` / `playfield config --dump`** — read back
  the live document verbatim, such that the dump re-loads identically
  (UC-24.S-1). This is what makes an experiment permanent: dump it,
  install the file on disk.
- **The self-describing template**: a fetched config reports **every
  settable key, blank when unset** (UC-03.S-1), so a client can
  discover what is adjustable without prior knowledge.

### Part 3 — Two open questions sprint 022 deliberately deferred

Both are now worth answering, and the second has teeth:

- **A reprojection-error acceptance threshold.** Calibration currently
  accepts any complete solve. Real solves run ~0.18 cm; at some error
  a "successful" calibration is junk. UC-04 already flags this.
- **Calibration staleness triggers beyond playfield replacement.**
  `Calibration.stale` is presently always `False`, so **a bumped
  camera is never noticed** — the system keeps serving plausible wrong
  coordinates, which the design calls the worst failure mode in the
  system. Candidate triggers: a low-rate drift check against the
  surveyed corner ArUcos, camera reopen, elapsed time. Decide, and make
  staleness actually reachable.

## Cause

Sprints 020–022 built cameras, detection, calibration, deskew, and the
views. What is missing is the operational layer that makes it survive a
restart and makes calibration trustworthy over time.

## Verification

- Install `main-playfield.json` and a camera config on disk, restart
  the daemon, and immediately run `aprilcam playfield image` — it works
  with no manual pushes.
- Push a bad setting; the whole push fails naming the key and the
  camera is unchanged.
- Dump a live config, install it, restart, dump again — identical.
- Bump the camera physically; the system reports the calibration stale
  rather than serving confident wrong positions.

## Related

- `docs/design/development-plan.md` stage 9
- `docs/design/configuration.md` — the two-directory model and cascade
- `docs/design/use-cases/daemon.md` UC-01, UC-02, UC-03, UC-04;
  `use-cases/cli.md` UC-23, UC-24
- Sprint 022 — where the restart trap was found, and whose two open
  questions Part 3 closes
- `annotation-store-for-shared-spatial-state.md` — the split-off
  annotation-store work (UC-28, spec §9 decision 8), planned for
  sprint 024
