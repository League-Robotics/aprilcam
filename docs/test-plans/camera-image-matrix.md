# Test Plan: Camera Image Matrix — captured fixtures for pipeline testing

**Status:** IMPLEMENTED — capture tool built and first fixtures captured 2026-07-29
(`testdev/capture_matrix.py` + `testdev/capture_matrix.yaml`; fixtures in
`tests/images/` — 19 Arducam OV9782 images, 9 HD USB CAMERA images, manifests +
field-state stub). **Updated 2026-08-23 (sprint 035 ticket 003):** capture
tool gained `--field-state-id` (§4), calibration-snapshot pinning (§6.2), a
dedicated `ov9281` matrix entry, and the HD USB control-ordering fix (§4,
§8's first open item — answered: the exposure/WB values were rejected
because the live baseline itself had `auto-exposure-mode`/
`auto-white-balance-temp` set to "auto"; fixed by forcing each variation's
gated value control's gate to manual before the value, ordered gate-first).
Remaining work tracked in `clasi/issues/image-matrix-fixture-test-suite.md`:
the pytest suite (§6, superseded by sprint 035's `tests/image_matrix/`
package) and the operator-assisted `fs-20260823` capture session (sprint 035
ticket 004) that exercises this ticket's changes against real hardware.
**Date:** 2026-07-29 (capture tool); 2026-08-23 (ticket 003 updates)
**Stakeholder intent:** capture real playfield images from several cameras
under systematically varied camera configurations, save them as regenerable
test fixtures, and run the detection/calibration pipeline against them as
an offline test suite. Regeneration must be one command per camera because
the field contents will change (new tags placed, etc.).

---

## 1. The two deliverables

1. **A capture program** (`testdev/capture_matrix.py`) that, for a given
   camera: identifies it, reads its current config as the baseline,
   applies a matrix of configuration variations, captures image(s) per
   variation, saves them with a full manifest, and **always restores the
   baseline config** afterwards (try/finally).
2. **A pytest suite** (`tests/test_image_matrix.py`) that runs the vision
   pipeline over the saved fixtures and asserts detection quality,
   world-coordinate accuracy, and degradation boundaries.

## 2. Baseline: the Arducam over the main playfield

Live config at `~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json`:

```json
{
  "device_name": "Arducam OV9782 USB Camera",
  "playfield": "main-playfield",
  "resolution": [1280, 800],
  "camera_position": {"height": 127.0, "x_offset": 0.0, "y_offset": 0.0},
  "settings": {
    "program": "uvc-util",
    "controls": {
      "auto-exposure-mode": "1",
      "auto-exposure-priority": "0",
      "auto-white-balance-temp": "false",
      "exposure-time-abs": "6",
      "gain": "2",
      "white-balance-temp": "4600"
    }
  }
}
```

Known from prior tuning (see memory / docs/knowledge): this camera needs
MANUAL low exposure; `auto-exposure-mode` must be `"1"` (manual), and the
washout cliff sits around exposure 12–18. The matrix deliberately crosses
that cliff so the tests document it.

## 3. Variation matrix

Per camera, starting from its own baseline config (never a hardcoded
one — other cameras have different controls and baselines):

| Dimension | Values (OV9782 example) | Purpose |
|---|---|---|
| `exposure-time-abs` | 3, 4, 6*, 8, 10, 12, 16, 24, 40 | washout cliff, dark-floor |
| `gain` | 1, 2*, 4, 8, 16 (at baseline exposure) | noise vs. sensitivity |
| `auto-exposure-mode` | manual* vs. auto (aperture-priority) | does auto break detection? |
| `white-balance-temp` | 3000, 4600*, 6500; plus auto-WB on | color classifier robustness |
| combined stress | low-exp+high-gain, high-exp+low-gain | realistic drift states |

`*` = baseline value. Full cross-product is unnecessary; vary one
dimension at a time from baseline, plus the small combined-stress set.
Target ≈ 20–25 images per camera. The matrix lives in a checked-in spec
file (`testdev/capture_matrix.yaml`) keyed by device-name pattern, so new
cameras get a matrix by adding an entry, and unknown cameras fall back to
an exposure+gain sweep of whatever controls `uvc-util -c` reports.

## 4. Capture program design

`python testdev/capture_matrix.py <camera>` (camera = enum number, name
pattern, or alpha code — same resolution as other CLI tools).

1. **Open the camera directly**, reusing the daemon's own camera code
   (`aprilcam.camera`: identity resolution, registry lookup, the
   VideoCapture/v4l2 backends, and the uvc-util settings machinery).
   This tool deliberately predates the finished v2 daemon, so direct
   access is a sanctioned exception to the daemon-sole-owner rule —
   it is offline test tooling, not a runtime client. It must first
   verify the daemon is not holding the device
   (`camutil.diagnose_camera_failure` reports the owning PID) and
   refuse with a clear "stop the daemon / close the camera" message on
   conflict.
2. **Identify**: resolve the camera identity and registry slug
   (`camera/identity.py`, `camera/registry.py`), read the camera's
   `config.json` from the config dir as the baseline document, note the
   linked playfield.
3. **Select matrix** from `capture_matrix.yaml` by device-name pattern.
4. Per variation:
   a. Build the variation control set = baseline controls with the
      varied values. **Gate-first ordering (sprint 035 ticket 003).**
      Any auto-mode control that gates a value control (`auto-exposure-mode`
      before `exposure-time-abs`; `auto-white-balance-temp` before
      `white-balance-temp`) is always pushed first — both because a
      variation that touches the value but not the gate has the gate
      forced to manual (`_ensure_gate_overrides`), and because the whole
      push, baseline or variation, is reordered gate-first regardless of
      dict iteration order (`_gate_first_order`, mirroring
      `camera.camera.Camera.apply_config`'s own `_GATE_KEYS`-first
      convention). Without this, a `set`-derived baseline dict's
      hash-randomized iteration order could push a value before its gate
      on some runs and not others — exactly what happened to HD USB
      CAMERA (camera 2) in the July run: its live baseline itself had
      both auto modes active, so every `exposure-time-abs`/
      `white-balance-temp` variation (and the two combined stress
      states) was silently rejected — recorded in
      `tests/images/hd-usb-camera/manifest.json`'s
      `skipped_variations.apply_failed`.
   b. Apply directly to the hardware via the same uvc-util path the
      daemon uses (`_apply_camera_settings`). `config.json` on disk is
      **never modified** — variations exist only on the hardware.
   c. Wait for settle: discard frames for ~1.5 s (auto modes need time
      to converge; manual modes settle in 2–3 frames).
   d. Capture a frame → save JPEG.
5. **Restore** by re-applying the baseline controls in a `finally:`
   block. A crashed run must not leave the playfield camera
   misconfigured; since disk config is untouched, a daemon restart also
   fully recovers.
6. Write/refresh the **manifest**, including a pinned `calibration.json`
   snapshot (§6.2) and, when `--field-state-id` was given, that id.

### `--field-state-id` (sprint 035 ticket 003)

`python testdev/capture_matrix.py <camera> --field-state-id fs-20260823`
tags every manifest this run produces with the given id (instead of the
default auto-generated `unverified-<date>` stub) and nests the run's
fixtures under `tests/images/<field-state-id>/<slug>/` instead of the flat
`tests/images/<slug>/` layout — so a new field-state generation coexists
with an older one rather than overwriting it in place. Omitting the flag
keeps the default unnested layout and legacy field-state.json behavior,
so existing camera entries and their existing captures are unaffected.
`tests/images/field-state.json` is always read/written at the fixture
root (`tests/images/field-state.json`), never per-camera and never
per-generation, so every camera's manifest in one run can reference the
SAME field-state id; an explicit id's `--field-state-id` run only ever
adds/sets `"current"` and fills a stub entry when one doesn't already
exist — it never clobbers a stakeholder-confirmed entry for that id.

### Fixture layout (committed to git — regenerable but CI-usable)

```
tests/images/
  field-state.json         # fixture root -- never per-camera
  <camera-slug>/            # default (no --field-state-id) layout
    manifest.json          # per-camera capture session record
    calibration.json        # pinned calibration snapshot (§6.2)
    baseline.jpg
    exp-03.jpg exp-04.jpg ... exp-40.jpg
    gain-01.jpg ... gain-16.jpg
    auto-exposure.jpg
    wb-3000.jpg wb-6500.jpg wb-auto.jpg
    stress-lowexp-higain.jpg ...
  <field-state-id>/<camera-slug>/   # with --field-state-id fs-...
    manifest.json
    calibration.json
    baseline.jpg ...
```

`manifest.json` per image records: full effective config document,
camera identity (unique_id, device_name), resolution, playfield slug,
capture timestamp, daemon + package version, and the **field state id**
(see §5). ~25 images × ~150–250 KB ≈ 5 MB/camera: acceptable in git.

## 5. Ground truth

Two sources, combined:

- **Playfield definition** (`main-playfield.json`): surveyed world
  positions of every ArUco, AprilTag, rectangle, and dot — the absolute
  reference for world-coordinate accuracy tests.
- **Field-state file** (`tests/images/field-state.json`): what was
  physically on the field at capture time — the set of visible tag IDs
  (some surveyed tags may be temporarily covered; extra tags may be
  placed). The capture program snapshots the current field-state id into
  each manifest. When the stakeholder adds tags and regenerates, they
  update field-state.json (or the program proposes it from a
  baseline-image detection run that the operator confirms).

## 6. Test suite

Parameterized over every manifest entry (skip cleanly if fixtures are
absent on a machine). All detection runs use the same code path the
daemon uses (`TagDetector` / `AprilCam.process_frame` internals — not a
parallel reimplementation).

1. **Tag recall per configuration.** For each image: detected tag IDs ⊇
   expected visible IDs (from field state). Baseline and near-baseline
   configs must be 100%; for cliff-crossing configs the assertion
   inverts — the test *documents* the failure boundary (e.g. exposure
   ≥ 16 ⇒ washout, recall may drop) so a regression that silently
   *improves or worsens* the boundary is visible.
2. **World-coordinate accuracy.** For surveyed static tags detected in
   calibrated images: |world_detected − world_surveyed| ≤ tolerance
   (start: 1.5 cm; tighten empirically). Uses the camera's committed
   calibration snapshot (the manifest pins which calibration.json was
   active; a copy is stored beside the fixtures so tests don't depend
   on live machine state). **Implemented (sprint 035 ticket 003):**
   `testdev/capture_matrix.py`'s `pin_calibration_snapshot` copies
   `<state_dir>/cameras/<slug>/calibration.json` (the same path
   `daemon/service.py` writes to — see `docs/design/design.md`
   §6/`calibration/DESIGN.md`) to `<fixture-dir>/calibration.json`
   **read-only** — it only ever reads the source, never writes it, so a
   capture run can never disturb a real camera's live calibration — and
   records its sha256 in the manifest's `calibration_snapshot_sha256`
   field. When a camera has no calibration yet, the field is `null` and
   the run still proceeds (logged, not fatal) — that camera's
   world-accuracy tests skip until it is calibrated and recaptured.
3. **Geometry conventions.** Using two tags with known east/north
   relationships from the survey: assert +x east / +y north / north-up
   on the deskewed image — the axis-mirror regression tests required by
   the world-coordinates-first issue.
4. **Corner/calibration robustness.** The 4 corner ArUcos (and all
   surveyed ArUcos) detected across the variation range; document which
   configs calibration would survive.
5. **Color/object detection** (where the field state includes the
   rectangles/dots): classifier finds each surveyed colored object
   within position tolerance; white-balance variations are the stress
   axis here.
6. **Preprocessing effectiveness** (informational, non-blocking):
   detection with/without the highpass illumination flattening across
   the exposure sweep, so preprocessing changes show their real effect.

## 7. Regeneration workflow (stakeholder loop)

```bash
# after changing what's on the field:
python testdev/capture_matrix.py arducam          # regenerate that camera
python testdev/capture_matrix.py --all            # every configured camera
# update tests/images/field-state.json if tag population changed
uv run pytest tests/test_image_matrix.py
```

More cameras will be attached soon; each new camera needs only (a) a
`capture_matrix.yaml` entry (or falls back to the generic sweep) and
(b) a capture run.

## 8. Open items to resolve during implementation

- Whether uvc-util control changes take effect on a live capture handle
  for every backend, or whether some controls require reopening the
  device per variation.
- Exact daemon-contention behavior: detect-and-refuse (preferred) vs.
  an optional flag that stops/restarts the daemon around the run.
- Settle-time calibration per control (auto-exposure convergence time on
  the OV9782 vs. the C920).
- Whether to also capture a short burst (3–5 frames) per variation for
  future tracking/velocity tests, or single frames only (start: single).
- Tolerances: start loose, ratchet down after the first full matrix run.
