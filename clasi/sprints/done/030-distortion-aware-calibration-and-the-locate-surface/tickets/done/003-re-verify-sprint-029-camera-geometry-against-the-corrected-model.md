---
id: '003'
title: Re-verify sprint 029 camera geometry against the corrected model
status: done
use-cases:
- SUC-003
depends-on:
- '001'
- '002'
github-issue: ''
issue: v2-calibration-does-not-model-lens-distortion.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Re-verify sprint 029 camera geometry against the corrected model

## Description

Sprint 029's own sprint.md names unmodelled lens distortion as the most
plausible explanation for camera 3's solved optical height (128.25 cm)
sitting 1.25 cm above the physical sensor plane (127 cm). Tickets
001/002 give that hypothesis something to actually test against. This
ticket runs the two falsifiable checks the sprint's own Success
Criteria commit to:

1. **Radial consistency** — re-measure the seven 46.5 mm AprilTags'
   apparent-size-vs-image-radius^2 correlation (documented at -0.96)
   and check whether it moves toward 0.
2. **Optical height** — re-run `aprilcam camera locate` on camera 3 /
   `main-playfield` and check whether the solved height moves toward
   127 cm from 128.25 cm.

Both physical fixtures (the seven tags; the elevated reference stands
for tags 14/15) may no longer be staged — sprint 029 ticket 004 hit
exactly this for the same camera and handled it by skipping cleanly
and saying so, not by fabricating a result. This ticket follows the
same precedent. If a real result *is* obtained and it does not move in
the predicted direction, that is a genuine falsification to report, not
explain away (sprint.md Success Criteria).

## Acceptance Criteria

- [x] If `arducam-ov9782-usb-camera` is attached: `aprilcam camera calibrate arducam-ov9782-usb-camera --with-intrinsics` is run live against `main-playfield`. If not attached, the ticket states so plainly and this specific check stops there — no fabricated numbers. **Attached; run live twice this ticket (once first-hand by this ticket, plus the run immediately preceding it) — see Completion Notes.**
- [x] Radial size-consistency re-measurement (the seven 46.5 mm AprilTags, `secondary-playfield`/`hd-usb-camera`) is attempted; the recorded result is either a real re-measured correlation number (compared against the documented -0.96), or an honest "tags no longer staged, not reproducible" note. **Attempted on the original camera/tags — blocked for an unrelated, honestly-recorded reason (`secondary-playfield` under-surveyed). Adapted to camera 3's own identically-sized surveyed ArUco markers instead (the ticket's own suggested fallback), producing real re-measured correlation numbers — see Completion Notes.**
- [x] `aprilcam camera locate arducam-ov9782-usb-camera --reference ... --dry-run` is attempted against whatever reference tags/stands are currently available; the recorded result is either a real solved nadir/height/RMS compared against the documented (3.05, -2.75)/128.25/0.16, or an honest "references no longer staged" note (mirrors `test_camera_geometry_hardware.py`'s own existing skip precedent for this exact scenario). **Attempted live; reproduced the documented "not detected" refusal — see Completion Notes.**
- [x] If a real re-measurement is obtained and the height does *not* move toward 127 cm, this is stated explicitly as a finding — with whatever investigation into why was possible (e.g. checking whether the ~1.9% scale-gap the issue itself leaves unresolved is implicated) — not omitted or rationalized away. **Not triggered: no real height re-measurement was obtained (references still not staged), so there is nothing to falsify against 127 cm this ticket. Stated explicitly, not silently skipped — see Completion Notes.**
- [x] If `camera locate`'s solve is re-run and produces a good fit (within `cli/locate.py`'s existing quality gates), it is written live (no `--dry-run`) so camera 3's `camera_position` reflects the corrected model going forward — per sprint.md's Migration Concerns, an existing `camera_position` predates the lens model and should be re-solved once one exists. **Not triggered: no solve was produced (blocked by missing references), so nothing was written. `camera_position` remains the pre-re-aim sprint 029 value, now of uncertain provenance — see Completion Notes.**
- [x] `docs/design/use-cases/cli.md`'s UC-29 fixture paragraph (currently: "most plausibly absorbing unmodelled lens distortion") is updated to state the real, re-measured outcome — whichever way it falls out.
- [x] A `docs/knowledge/` entry is added recording the re-verification methodology and result, per CLAUDE.md's "add an entry after any painful debugging session" rule — this is exactly the kind of hard-won, non-obvious operational finding (or non-finding) that rule exists to preserve.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/calibration/test_geometry.py -v` (confirm `solve_camera_geometry`'s existing pinned regression is untouched by this ticket).
- **New tests to write**: none expected in `src/`/`tests/` beyond what tickets 001/002 already cover — this ticket's own "test" is the live hardware re-measurement itself, recorded in its completion notes and in the new knowledge-base entry. If the physical fixtures are confirmed gone, no synthetic substitute is invented in their place (it would misrepresent itself as ground truth).
- **Verification command**: `uv run pytest tests/aprilcam/calibration/ -v` (regression check only; the real verification for this ticket is the live re-measurement, not a new automated test)

## Completion Notes

**Ticket 002's own numbers were stale before this ticket even started.**
Between ticket 002 finishing and this ticket starting, a live v1 daemon
holding the same shared arducam/control socket was stopped (see
`clasi/issues/camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`)
and the camera was physically re-aimed to recover full-field visibility.
A fresh `--with-intrinsics` run (done immediately before this ticket was
dispatched) succeeded where ticket 002's had been rejected:
`fx=511.185px` (0.40x the 1280px width), `k1=0.006877` — comfortably
inside the sanity bound. **This ticket's own job was to interrogate
that small `k1`, not just accept it.**

**Falsifiable check 1 (radial consistency), attempted and adapted.**
`secondary-playfield`/`hd-usb-camera` (the camera the documented -0.96
correlation came from) cannot currently get its own intrinsics fit —
`calibration failed: estimate_intrinsics requires at least 6
ArUco+AprilTag ground-truth points ... found 4` — a stakeholder
surveying gap, not something fixable here. Adapted the same method to
camera 3 using `main-playfield`'s own 10 identically-sized surveyed
ArUco markers (the ticket's own suggested fallback): across 6 repeated
live bursts, correlating each marker's homography-mapped apparent
world size against pixel radius² gave **-0.52 to +0.40** (mean
magnitude ≈0.15), regardless of whether/which lens model was applied,
never reaching the n=10 significance threshold (~0.63) — nowhere near
the documented -0.96. **This is evidence against large real distortion
hiding behind the small `k1`** (hypothesis (b) in its strong form).

A second, independent, and equally important finding from the same
work: the fitted `fx`/`k1` **pair itself swings substantially between
otherwise-identical live calibration bursts** — `fx` ranged ~504-661px
(≈30%), `k1` ranged ~0.0069-0.0135 (≈2x), while the held-out
reprojection residual stayed essentially flat (~2.7-3.2px) throughout.
This is the same fx/depth ridge ticket 002's own investigation
documented for the exactly-nadir case, softened by this camera's
post-re-aim tilt but not eliminated. **Verdict**: (a) is better
supported than the strong form of (b) — no detectable large distortion
signature — but the specific stored `k1` should not be read as a
precisely known constant; it is one sample from a run-to-run range
wide enough to matter. Neither "distortion is small, therefore fine"
nor "the fit is definitely wrong" is the honest summary — "no evidence
of large hidden distortion, but the single-view fit is still
underdetermined enough that no one run's coefficients should be
over-trusted" is. Full methodology, the run-by-run number table, and
the exposure/contention story that had to be cleared first:
`docs/knowledge/2026-08-20-camera-3-distortion-reverification.md`.

The ~1.9% scale-gap the source issue leaves unresolved (Open Question
4) could not be checked for camera 3: main-playfield's ArUco markers
have no independently documented physical size anywhere in this repo,
so there is nothing to compare the measured ~4.3cm mean side length
against.

**Falsifiable check 2 (optical height): stayed blocked, confirmed
live, not faked.** `aprilcam camera locate arducam-ov9782-usb-camera
--reference 15:dot-northeast-orange:17.2 --reference
14:dot-southwest-orange:15.0 --dry-run` reproduced the documented "not
detected" refusal — the elevated reference stands are still removed.
No height was re-solved, so acceptance criteria 4 and 5 (react to a
non-127cm result; write a good re-solve live) are not triggered; there
is nothing to falsify or write. **A second complication, independent
of the missing stands**: the camera was physically re-aimed between
sprint 029 and now. Ground nadir/height are properties of the fixed
mounting point, not the aim, so they survive a pan/tilt-only
adjustment but not a relocation. This camera's lens is wide enough
(fx/width ≈0.4-0.5, roughly 90-100° horizontal FOV) to see the whole
field from one fixed point without much tilt, which favours "re-aimed
in place" over "relocated" — but this is inference, not a
re-measurement. **The stored `camera_position` (height 128.230cm,
nadir (3.057, -2.799)) must be treated as unverified** until a fresh
`camera locate` run (stands re-staged, or Open Question 7's permanent
elevated survey features) confirms or replaces it.

**An unrelated hardware gotcha found and fixed along the way, not
faked around.** When this ticket's own live checks started, camera 3
returned zero detections — a captured frame showed the field blown out
to solid white. The stored `settings.exposure=100` no longer matched
the room's lighting (the same class of problem
`docs/knowledge/2026-08-12-...md`'s "Gotcha: exposure is
lighting-dependent" already documents, recurring on the same camera). A
`uvc-util -I 1 -s exposure-time-abs=$e` sweep found `exposure=11-13`
restored full 11/11 detection; `exposure=12` was pushed live and
persisted via `aprilcam camera config <file>`. Recorded in the new
knowledge-base entry, not silently worked around.

**Testing performed.** `uv run pytest tests/aprilcam/calibration/test_geometry.py -v`
— 49 passed. `uv run pytest tests/aprilcam/calibration/ -v` — 122
passed (this directory has no `@pytest.mark.camera` tests; nothing
skipped). No `src/`/`tests/` code was changed by this ticket — its own
verification is the live re-measurement recorded above and in the
knowledge-base entry, per the ticket's own Testing section.

**Files touched**: `docs/design/use-cases/cli.md` (UC-29 fixture
paragraph), `docs/knowledge/2026-08-20-camera-3-distortion-reverification.md`
(new), `docs/knowledge/2026-08-12-v1-shaped-config-and-calibration-files-rejected-by-v2.md`
(extended with a 2026-08-20 update), this ticket file. Live-environment
changes (outside version control, for the record): camera 3's
`~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json`
exposure setting corrected from `100` to `12`; camera 3's
`~/.local/share/aprilcam/cameras/arducam-ov9782-usb-camera/calibration.json`
holds a fresh `--with-intrinsics` fit from this ticket's own live run
(`fx=581.48`, `k1=0.01005`, all 10 ArUco ids matched, reprojection
0.188cm) — within the run-to-run range documented above, not a new
data point that changes the verdict.
