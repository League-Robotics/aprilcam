---
status: pending
---

# `camera probe` always reports uncalibrated/no-playfield, and renders skipped cameras as if probed

## Description

`aprilcam camera probe` and `aprilcam camera list` disagree about the
same cameras, in the same daemon, seconds apart. `probe` is wrong:

```
$ aprilcam camera probe
  [2]    hd-usb-camera               present  usable  playfield=-  uncalibrated
  [3]    arducam-ov9782-usb-camera   present  usable  playfield=-  uncalibrated
  [5]    arducam-ov9281-usb-camera   present  usable  playfield=-  uncalibrated
  skipped:
    arducam-ov9281-usb-camera: in use by a live subscriber

$ aprilcam camera list
  [2]    hd-usb-camera               present  usable  playfield=secondary-playfield  calibrated
  [3]    arducam-ov9782-usb-camera   present  usable  playfield=main-playfield       calibrated
  [5]    arducam-ov9281-usb-camera   present  usable  playfield=main-playfield       calibrated
```

All three cameras are genuinely calibrated with genuine playfield links —
`list` is right, `probe` is wrong. This matters because `probe` is the
command that reads as the authoritative "go look at the hardware and tell
me the truth" operation, so its answer is the one a person trusts.

## Cause — two independent defects

**1. `probe_cameras()` never overlays calibration/playfield state.**

`DaemonService.list_cameras()` overlays `playfield` and
`calibrated`/`calibration_stale` from `_calibration_flags()`
(`daemon/service.py`), and its docstring explicitly records that an
earlier version "previously hardcoded `calibrated=False`
unconditionally" and that this was fixed — calibration state lives in
`self._calibrations`, not on `Camera.state`, "per the module boundary
that keeps calibration a daemon-level concern."

`DaemonService.probe_cameras()` (`daemon/service.py:472`) is:

> "Pure delegation to `CameraDiscovery.probe()` — no business logic here
> (sprint architecture's module table)."

So it returns raw discovery records with those fields at their defaults.
**The exact bug that was found and fixed in `list_cameras` is still
present in its sibling.** Both commands render through the same
`print_cameras()`, so the divergence is purely in the data.

The "no business logic here" boundary is defensible for *discovery*, but
the overlay is not discovery — it is the same daemon-level state
`list_cameras` already reaches for. Either `probe_cameras` applies the
same overlay, or the probe path must not emit those columns at all (see
below).

**2. A skipped camera is still rendered as a full data row.**

`arducam-ov9281-usb-camera` appears both as row `[5]` with default values
*and* under `skipped: in use by a live subscriber`. `print_probe_report`
(`cli/_common.py:157`) documents its own contract as printing skipped
backends "explicitly, never silently dropped, so a partial probe can
never read as a complete one (`ProbeReport.skipped`'s contract,
UC-25.S-7)."

The skip notice is printed — but the row above it presents unprobed
defaults as fact, and the row is the more prominent of the two. The
contract is satisfied in letter and broken in effect.

## Proposed fix (stakeholder-specified, 2026-08-21)

**Every camera always appears in the table. There is no separate
`skipped:` block.** A camera the probe could not interrogate is still a
camera the operator wants to see — dropping it into a footnote below the
table buries the one row they are most likely looking for. Fold that
information into the row itself.

Concretely:

1. **Drop the `skipped:` section entirely.** Its content moves into the
   affected camera's own row.

2. **The availability column reports in-use state.** Today it prints
   `usable` for everything. It should distinguish:
   - `present` — camera is attached and free
   - `in use` — camera is attached but currently held (live subscriber,
     another daemon, another process)

   This applies to **`camera list` as well as `camera probe`** — a
   camera held by a live subscriber should read `present  in use` in
   both, not `present  usable`.

3. **`probe` says `not probed` where it did not probe.** When the probe
   could not interrogate a camera, the fields it would have measured
   read `not probed` rather than a default value. Never print a default
   where no measurement was taken.

4. **Same fields, same values.** If `probe` and `list` render the same
   column, that column must carry the same value in both for the same
   daemon state. Calibration and playfield are real daemon state, not
   probe results — a calibrated camera reads `calibrated` with its
   playfield in **both** commands. Apply the same
   `_calibration_flags()`/playfield overlay `list_cameras()` already
   uses, through a single shared helper rather than a second copy: the
   `list_cameras` docstring's own reasoning ("the single overlay point
   ... so a camera's reported calibration status can never drift between
   the ... code paths") applies with equal force across commands, not
   just across branches inside one method.

5. **Test that they agree.** Assert `probe` and `list` report identical
   `playfield`, `calibrated`, and `calibration_stale` for the same daemon
   state. This divergence was caught only by a human running two commands
   side by side and reading them against each other.

Note this supersedes the earlier draft's suggestion that a skipped
camera could simply be omitted from the table — the stakeholder's
direction is the opposite and is better: the camera is always shown, and
the row carries the truth about what is known and what was not measured.

## Related

- Discovered 2026-08-21 while bringing up a newly-connected
  Arducam OV9281.
- `camera probe` also does not pick up hot-plugged cameras despite being
  documented as "force a deep camera re-probe" — filed separately.
