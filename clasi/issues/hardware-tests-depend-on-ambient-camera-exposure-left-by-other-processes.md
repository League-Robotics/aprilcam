---
status: pending
---

# Hardware tests inherit whatever exposure the last process left on the camera, so they pass or fail by accident

## Description

`tests/aprilcam/daemon/test_calibration_hardware.py` has a hidden
dependency on **ambient UVC device state**. It configures the camera as:

```python
CameraConfig(name=ARDUCAM_SLUG, playfield="main-playfield", settings={})
```

— empty `settings` — and runs against a temp `config_dir`/`state_dir`
(`tmp_path / "config"`, `tmp_path / "state"`). So it pushes no exposure
of its own **and** cannot inherit the real config's exposure. It runs the
camera at whatever the previous process happened to leave on the device.

UVC control values persist on the device across processes. When the last
writer left a bad exposure, the frame blows out, markers drop, and the
test fails with a message blaming the physical field.

## Demonstrated, not inferred (2026-08-20)

Three runs, same commit, no code changes between them:

| # | Action | Result |
|---|---|---|
| 1 | `pytest tests/aprilcam/daemon/test_calibration_hardware.py` | **7 failed**, 3 passed — `missing_ids (1, 4)` |
| 2 | `aprilcam camera calibrate arducam-ov9782-usb-camera` (live, real config, `exposure: 12`) | **succeeded, 10/10 ArUco matched**, reprojection 0.186 cm |
| 3 | same command as (1), immediately after (2) | **7 passed**, 3 skipped |

Run (2) is the only thing that changed, and all it did was leave the
device at the exposure the real config specifies. The field was never the
problem.

## Why this matters more than a flaky test

This single mechanism accounts for a lot of noise this project has
already paid for:

- Sprint 030 ticket 004 reported "the physical arducam currently can't
  see the full playfield."
- Ticket 005's `-m camera` audit attributed **17 failures** to that same
  supposed precondition and, reasonably, scoped them out.
- Ticket 003 independently found that camera 3's stored `exposure: 100`
  "blew every frame to white" and swept `uvc-util` to land on 12.
- The pre-close full-suite run showed 15 failures, all `missing_ids (1, 4)`.

Each was read as a physical or environmental fact about the playfield.
At least some of them were the test suite failing to state a precondition
it depends on. The failure message actively misleads — it tells the
reader to go check the camera's aim, which sends people to inspect
hardware that is working.

Note this is a *different* mechanism from the v1-daemon contention issue
(`camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`),
which was confirmed separately and accounted for 18 of 19 failures in its
own controlled comparison. Both are real; this one is what remained.

## Proposed fix

1. **Push an explicit exposure** in the hardware fixtures rather than
   `settings={}` — the tests should establish the camera state they need
   instead of hoping for it. `exposure: 12` is the value sprint 030
   ticket 003 measured against current room lighting, but a fixed
   constant will drift with the room; see (3).
2. **Fail with the right diagnosis.** When markers are missing, the
   message should say the frame may be over- or under-exposed and name
   the exposure actually in force, rather than asserting the camera is
   mis-aimed. The current text costs real debugging time — it did today.
3. **Consider whether a fixed exposure is even right.** Lighting changed
   materially over a single afternoon (100 worked in the morning, blew
   out by mid-afternoon). A short auto-exposure sweep at fixture setup —
   pick the exposure that detects the most markers — would make these
   tests robust to the room instead of to a constant someone has to
   maintain. That is closer to what ticket 003 did by hand.

## Verification

- Deliberately leave the device at a known-bad exposure (e.g. 100 under
  current lighting), then run the hardware tests: they must either pass
  by setting their own exposure, or skip with a message naming exposure
  as the unmet precondition — never fail claiming the field is wrong.
- Run the file twice in a row and after an unrelated camera-using
  process; results must not depend on run order.

## Related

- `camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`
  — the other, separately-confirmed cause of camera-test noise.
- `hardware-tests-fail-instead-of-skipping-and-a-suspect-uvc-exposure-push.md`
  — sprint 030 ticket 005 closed this for the daemon-reachability guard;
  this issue is the same principle applied to the exposure precondition,
  which 005 deliberately scoped out.
- Sprint 030 ticket 003's completion notes and
  `docs/knowledge/2026-08-20-camera-3-distortion-reverification.md`.
