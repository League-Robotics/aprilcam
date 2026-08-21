---
status: done
sprint: '030'
tickets:
- 030-005
---

# Hardware tests fail instead of skipping, and one may be hiding a real UVC exposure bug

## Description

A full-suite run on 2026-08-20 (`uv run pytest -q`) gave **2 failed,
1385 passed, 4 skipped in 717s**. Neither failure is a regression in
merged v2 code — both are `@pytest.mark.camera` tests that **fail hard
where they should skip**, because their environment preconditions are
only partly guarded. One of the two may additionally be a real backend
bug wearing a test failure as a disguise.

Any suite run on a machine that has the cameras attached but is not in
the exact state the test assumes now reports red. That trains people to
ignore two failures, which is how the third one gets missed.

## 1. Geometry hardware test does not guard on a reachable daemon

`tests/aprilcam/daemon/test_camera_geometry_hardware.py::
test_locate_reports_missing_references_or_reproduces_the_documented_fixture`
fails at line 250:

```
assert result.returncode == 0, stderr
AssertionError: advisory: only 2 references given -- 3+ is recommended
  for redundancy (not blocking; add more --reference args to improve
  the fit's conditioning)
error: daemon at 'localhost' did not answer: no local daemon answered
assert 1 == 0
```

The module guards two preconditions and misses a third:

- **Camera attached** — `_skip_if_arducam_not_attached()`
  (`test_camera_geometry_hardware.py:111`) skips when
  `arducam-ov9782-usb-camera` is absent. Present and working.
- **Reference tags still on the field** — sprint 029 ticket 004 built
  this test to skip cleanly when `camera locate` answers "not
  detected", because the reference stands had already been removed.
  Present and working.
- **A daemon is running** — *not guarded*. `camera locate` drives
  detection through the daemon's `GetTag` RPC by design (the daemon is
  the sole vision authority), so with no daemon up the command exits 1
  before it can ever reach the "not detected" path the skip depends on.

The fix is a third guard of the same shape as the other two: skip when
no local daemon answers. Worth deciding at the same time whether the
module should start its own daemon on a temp `socket_dir` — every other
`@pytest.mark.camera` module in the tree already has the
`_short_tmp_dir` machinery for exactly that, and a self-contained test
is better than one that skips on most machines.

## 2. `exposure` push rejected by uvc-util — possibly a real bug

`tests/aprilcam/daemon/test_service.py::
test_set_camera_config_two_real_cameras_configured_independently`
fails:

```
aprilcam.errors.UnsupportedSetting: unsupported or rejected setting: 'exposure'
  src/aprilcam/camera/camera.py:476
WARNING aprilcam.camera.backends.uvc_util:uvc_util.py:197
  uvc-util -s exposure-time-abs=300 exited 13:
  ERROR: unable to write new value to control exposure-time-abs
```

`Camera.apply_config` behaved correctly here — `set_property` returned
false, so it rolled the whole push back atomically and raised, which is
the documented contract. The question is why the control refused the
write.

This has the shape of the **UVC control-ordering quirk** CLAUDE.md
warns about, where absolute exposure is not writable until auto-exposure
is turned off. v2 already knows this: `_GATE_KEYS = ("auto_exposure",
"auto_white_balance")` (`camera/camera.py:122`) exists precisely to push
the gates first, and `apply_config` orders them ahead of everything else
(`camera.py:459-460`). So the gate mechanism is present and the failure
is one of:

- the test's config does not set `auto_exposure` at all, so nothing
  ungates the control (a test-fixture bug);
- `auto_exposure` is pushed but the value written does not actually put
  *this* camera in manual mode (a value/semantics bug — v1 had per-model
  quirks here);
- `300` is outside this camera's supported `exposure-time-abs` range (a
  range-validation gap — the CLI should say so, not surface `exited 13`);
- the second of the two cameras genuinely lacks the control, in which
  case the test's premise about which settings are portable is wrong.

**Investigate which before treating this as a skip-guard fix.** If it is
a real ordering or range bug it affects `camera config` pushes against
real hardware generally, not just this test, and the current failure
mode — a raw `exited 13` behind a generic `UnsupportedSetting` — is a
poor diagnostic either way.

## Verification

- Full suite green on a machine with the cameras attached and no daemon
  running.
- Full suite green on a machine with no cameras attached
  (`uv run pytest -m "not camera"` unaffected, as the module docstring
  requires).
- For item 2: a stated, tested conclusion about *why* the write was
  refused, and an error message that names the real cause (unsupported
  control vs. out-of-range value vs. still in auto mode) instead of
  `unsupported or rejected setting`.

## Related

- Sprint 029 ticket 004 built item 1's existing skip behaviour; this
  extends it rather than contradicting it.
- `hardening-cleanup-and-the-design-doc-set.md` — item 2's diagnostic
  quality belongs to the same "confusing failure" family.
