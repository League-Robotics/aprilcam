---
date: 2026-08-20
tags: [camera, calibration, distortion, geometry, locate, daemon, contention, uvc-util, exposure]
related-tickets: [030-002, 030-003]
---

# Re-verifying camera 3's geometry against a distortion model: methodology, results, and what stayed blocked

Sprint 029 solved camera 3's (`arducam-ov9782-usb-camera`) optical
height at 128.230 cm against a physical sensor plane measured at
127 cm, and named unmodelled lens distortion as the most plausible
cause of the 1.23 cm gap. Sprint 030 built a distortion model
(tickets 001/002) specifically so ticket 003 could re-verify that
guess against real measurements instead of leaving it as a guess
forever. This entry records what that re-verification actually found,
including two environmental problems that had to be diagnosed and
fixed before any of it could be measured at all.

## The two problems that had to clear first (neither one is the distortion question)

**1. A live v1 daemon was contaminating every measurement.** A v1
AprilCam daemon from a sibling checkout ran all day holding the same
exclusive arducam and the same shared control socket
(`$TMPDIR/aprilcam-501/control.sock`) this project uses, applying its
own capture settings underneath v2's. A `-m camera` sweep with it live
gave 19 failed / 23 passed / 7 skipped; the same sweep with it stopped
gave 1 failed / 41 passed / 7 skipped. Full writeup and evidence:
`clasi/issues/camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`.
**Lesson**: before trusting any "the hardware is degenerate" conclusion
on a camera whose control socket might be shared, confirm nothing else
is holding the device. `ps aux | grep aprilcam` and checking whether a
stale `aprilcamd.pid` sits in *your own* socket directory is cheap;
re-deriving a physical-degeneracy theory from a contaminated
measurement is not.

**2. Once that was fixed, a stale exposure setting blew every frame
out.** The single physical fault left after de-contamination
(`matched_ids [6,7,9,10]`, half the field invisible) turned out to be
the stakeholder re-aiming the camera to recover full-field visibility
— not a code problem. But by the time this ticket ran its own live
checks (some time after that re-aim and after the `--with-intrinsics`
run that is this ticket's starting point), the stored
`settings.exposure=100` no longer matched the room's lighting:

```
$ aprilcam camera tags arducam-ov9782-usb-camera   # exposure=100, stored
  (no tags detected)
```

A captured frame confirmed it — the whole field was blown to solid
white, only the brightest edge markers faintly visible. This is the
exact "Gotcha: exposure is lighting-dependent" pattern already
documented in
`docs/knowledge/2026-08-12-v1-shaped-config-and-calibration-files-rejected-by-v2.md`,
recurring on the same camera. A fresh sweep (`uvc-util -I 1 -s
exposure-time-abs=$e`, camera index confirmed via `uvc-util -d`) found:

| exposure | tags detected (best of several polls) |
|---|---|
| 5-10 | 1 |
| 20 | 7 |
| 40 | 3 |
| 60-100 | 0-1 |
| **11-13** | **11 (full field)** |
| 14-17 | 8-10 |

`exposure=12` was pushed both live and to
`~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json` via
`aprilcam camera config <file>` (no daemon restart needed — config is
pushed live over RPC, unlike a raw file edit). Detection was stable at
11/11 (10 ArUco + AprilTag 1) afterward. **Lesson, restated because it
bears repeating**: re-sweep exposure whenever detection degrades, on
this camera specifically and probably on any exposed-to-daylight
camera generally — do not assume a stored value that worked once is
still right, and do not let a blown-out frame get misread as "the
field changed" or "the markers are gone."

## Falsifiable check 2 (optical height): stayed blocked, confirmed honestly

`aprilcam camera locate arducam-ov9782-usb-camera --reference
15:dot-northeast-orange:17.2 --reference 14:dot-southwest-orange:15.0
--dry-run` was attempted live. Result:

```
error: reference tag 15 (AprilTag) was not detected by
'arducam-ov9782-usb-camera' -- check it is staged, in view, and the
camera is calibrated, then retry
```

The elevated reference stands sprint 029 used are still removed (the
same precondition sprint 029 ticket 004 and sprint 030's own
`test_camera_geometry_hardware.py` already document and skip cleanly
on). This is the honest, reproduced "not detected" refusal, not a
stale success and not a fabricated number — the solved height could
not be re-derived or re-checked this ticket.

**A separate complication for this specific check, independent of the
stands being gone**: the camera was physically re-aimed between sprint
029's original solve and this re-verification, to recover full-field
visibility (see problem 1 above). Ground nadir and optical height are
properties of the camera's fixed *mounting point*, not its pointing
direction — they are unaffected by a pure pan/tilt adjustment on a
fixed bracket, but would be wrong after a genuine physical relocation.
This camera's lens is wide enough (see fx/width figures below, roughly
0.4-0.5x the 1280px image width, i.e. very roughly 90-100° horizontal
FOV) to see the whole ~134x89cm field from one fixed point without
needing much tilt at all, which favours "re-aimed in place" (rotation
only) over "relocated" as the more parsimonious explanation for going
from half the field invisible to all of it visible. **This is
inference, not a re-measurement** — nothing performed here confirms it.
Treat the currently-stored `camera_position` (height 128.230 cm, nadir
(3.057, -2.799)) as unverified until a fresh `camera locate` run
confirms or replaces it. That needs either the reference stands
re-staged, or (sprint 030's Open Question 7) permanent elevated survey
features added to `main-playfield` so `camera locate` no longer depends
on temporary hardware at all.

## Falsifiable check 1 (radial consistency): adapted to this camera, and what it found

The documented -0.96 apparent-size-vs-radius² correlation was measured
on **`hd-usb-camera`/`secondary-playfield`** using seven temporary
46.5mm AprilTags, not on camera 3. Reproducing it exactly on
`hd-usb-camera` is currently blocked for an unrelated reason:
`secondary-playfield` only surveys 4 ground-truth points, below the
`estimate_intrinsics` floor of 6 (`calibration failed: ... found 4`) —
a stakeholder surveying action, not a code fix.

**But the same check can be run on camera 3 using main-playfield's own
ten identical-size, spread-out ArUco markers as the "known-identical-
size" ground truth**, with no new survey and no new hardware. Method,
implemented as a standalone read-only script against the live daemon
(no writes; `GetTags`/`GetTag` and the pure `_estimate_intrinsics`/
`_solve_homography`/`undistort_point` functions only):

1. Pull one live detection burst's `pixel_corners` (4 per ArUco tag)
   and `pixel` (centre) for all 10 surveyed ArUco ids via
   `Discovery().connect(None).get_tags("arducam-ov9782-usb-camera")`.
2. Solve a homography **fresh, from that same burst's raw pixel
   centres** against `main-playfield`'s surveyed positions
   (`calibration.calibration._solve_homography`) — this is the "no
   lens model at all" condition, matching what v2 always did before
   sprint 030.
3. Map each tag's 4 raw pixel corners through that homography, average
   the resulting quadrilateral's 4 side lengths (cm) as the tag's
   "apparent size."
4. Repeat steps 2-3 but undistorting every point first
   (`calibration.geometry.undistort_point`) with a fitted lens model —
   both the currently-stored `calibration.json` model and a fresh
   `_estimate_intrinsics` re-fit **on that same burst** (so "raw" vs
   "corrected" never differ by which detection burst they came from,
   only by which lens model was applied — a burst-mismatched
   comparison was tried first and discarded specifically because it
   confounds detection-burst noise with the correction itself).
5. Correlate apparent size against pixel radius² from the principal
   point (`(639.5, 399.5)`), same statistic as the original -0.96
   measurement.

**Result, across 6 repeated live bursts**: correlation ranged **-0.52
to +0.40** (mean magnitude ≈ 0.15), regardless of whether a lens model
was applied and regardless of which lens model. None reached the n=10
two-tailed significance threshold (Pearson r ≈ 0.63 at df=8) — these
readings are consistent with pure sampling noise around zero, not a
detectable radial trend. This is **nowhere near** the documented -0.96
(which, at n=7, was comfortably significant). Conclusion: no evidence
of large real barrel distortion hiding in camera 3's raw pixel data —
this specific lens appears to have genuinely much less distortion than
`hd-usb-camera`'s, at least at the magnitude the original measurement
found.

**A second, independent finding from the same 6 runs (plus the CLI's
own `--with-intrinsics` runs) — the fitted `fx`/`k1` split itself is
not tightly determined by a single view, even now:**

| run | fx (px) | k1 | held_out_rms_px |
|---|---|---|---|
| stored (pre-ticket, ticket 002's own run) | 511.2 | 0.00688 | not recoverable (daemon stdout was `DEVNULL`'d — see below) |
| live re-fit A | 517.3 | 0.00780 | 3.001 |
| live re-fit 1 | 508.1 | 0.00775 | 3.167 |
| live re-fit 2 | 547.1 | 0.00910 | 2.900 |
| live re-fit 3 | 593.5 | 0.01063 | 2.799 |
| live re-fit 4 | 661.3 | 0.01353 | 2.843 |
| live re-fit 5 | 503.7 | 0.00692 | 2.700 |
| final `--with-intrinsics` CLI run (now stored) | 581.5 | 0.01005 | 0.188cm reprojection (not held-out) |

`fx` ranges ~504-661px (≈30% spread), `k1` ranges ~0.0069-0.0135
(≈2x spread), across otherwise-identical repeated live calibration
bursts of the same physical, stationary camera — yet `held_out_rms_px`
stays essentially flat at ~2.7-3.2px throughout. A held-out residual
that barely moves while `fx`/`k1` swing by 30%/2x is the signature of
a shallow, not-fully-resolved trade-off ridge between focal length and
distortion for a single coplanar(-ish) view — the same fx/depth
degeneracy ticket 002's own investigation documented for the
exactly-nadir case, evidently softened but not eliminated by this
camera's post-re-aim tilt. Every one of these fits passes
`_sanity_check_intrinsics` (all comfortably inside the 0.3-3.0x fx/width
band, `k1` far under the v1-derived bound) — the sanity bound catches
the *extreme* failure (v1's `[0.42, -2.43, ...]`, or the pre-re-aim
`fx=310.5px`/0.24x rejection), not this milder, still-real
underdetermination.

**Verdict on (a) "genuinely low distortion" vs (b) "fx absorbed it,
k1 is artificially small"**: the evidence favours (a) over the strong
form of (b) — there is no detected radial-size signature anywhere near
the magnitude that would indicate large hidden distortion. But neither
should the currently-stored `k1` be read as a precise, final constant:
it is one draw from a run-to-run range wide enough that a different
detection burst on the same physical setup would plausibly have stored
a `k1` roughly twice as large, or an `fx` roughly 30% different, and
fit the data almost as well by the held-out residual's own account.
**Practical recommendation**: do not treat any single
`--with-intrinsics` run's `camera_matrix`/`dist_coeffs` as ground
truth for tuning downstream thresholds; averaging several repeated
calibration bursts, or installing permanent elevated survey features
(sprint 030's Open Question 7) to structurally resolve the fx/depth
ridge, would each increase confidence in the specific numbers far more
than accepting one run's output at face value.

**A residual scale-gap question left genuinely unanswered.** The
source issue also names an unresolved ~1.9% apparent-size scale gap
(measured on `hd-usb-camera`'s 46.5mm tags against their known
physical size). Main-playfield's ArUco markers have no independently
documented physical size anywhere in this repo (checked `README.md`,
the playfield JSON, and `taggen`/`arucogen` defaults), so the
equivalent scale-gap comparison could not be attempted for camera 3 —
the mean measured size (~4.3cm) is internally consistent across
conditions here but has nothing surveyed to compare against.

## Why `held_out_rms_px` for the original stored run could not be recovered

`Calibrator.calibrate`'s intrinsics path logs `held_out_rms_px` at
`logger.info` (`calibration/calibration.py`), which should have
appeared on the daemon's stdout for the original `--with-intrinsics`
run this ticket started from. It didn't, because `aprilcam daemon
start` spawns the daemon subprocess with `stdout=subprocess.DEVNULL`
(`client/lifecycle.py`) — by design, so a CLI invocation doesn't hang
waiting on a pipe, but it also means there is currently no durable log
file anywhere for a long-running daemon's `INFO`-level output. This
ticket worked around it by re-deriving the same measurement directly
(`_estimate_intrinsics`, called from a standalone script against a
live-fetched point set) rather than trying to recover the original
run's log line. **Possible future improvement, not attempted here**: a
`--log-file`/`APRILCAM_LOG_FILE` option on `aprilcam daemon start`
would make this kind of after-the-fact diagnosis unnecessary next time.

## Related

- `docs/knowledge/2026-08-12-v1-shaped-config-and-calibration-files-rejected-by-v2.md`
  — the v1-format `calibration.json` this ticket's `--with-intrinsics`
  run finally replaced, and the exposure-gotcha pattern this entry's
  problem 2 recurs.
- `clasi/issues/camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`
  — full writeup of problem 1 above.
- `clasi/sprints/030-distortion-aware-calibration-and-the-locate-surface/tickets/done/002-opt-in-intrinsics-estimation-on-camera-calibrate.md`
  — the coplanar-constrained fit, sanity bound, and fx/depth-degeneracy
  investigation this entry's findings extend.
- `docs/design/use-cases/cli.md` UC-29 — updated fixture paragraph
  pointing back here.
