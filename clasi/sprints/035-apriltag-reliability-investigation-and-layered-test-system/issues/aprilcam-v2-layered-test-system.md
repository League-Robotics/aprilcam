---
status: in-progress
sprint: '035'
tickets:
- 035-003
- 035-004
- 035-005
- 035-006
- 035-007
- 035-008
- 035-009
- 035-010
- 035-011
- 035-012
- 035-013
- 035-014
- 035-015
- 035-016
- 035-017
- 035-018
---

# AprilCam v2 layered test system: offline image-matrix, live system tests, acceptance runs

## Description

The project has 29 use cases with 111 scenario IDs (`docs/design/use-cases/`), and
every traceability "Tests" column is empty — `docs/design/process.md` §5 declares
scenarios ARE the test plan. Hardware test sweeps cost ~9-12 minutes and one measured
run showed 18 of 19 failures were environmental (daemon contention), not code. Recent
incidents (dark camera, exposure drift, the detection-confirmation cliff) were all
diagnosed by ad-hoc scripts that a real test system would have caught instantly.

Stakeholder requirements (2026-08-23):

- Test all major use cases; work through the rest over time.
- Open a camera and calibrate it — **with and without raised blocks**. Blocks are not
  currently placed; the solved `camera_position` in
  `~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json` must NEVER be
  overwritten. Both modes must work.
- Capture playfield images and save them; run tests offline on saved images. Test
  different exposure levels — captured sweeps AND synthetic exposure/contrast
  modifications of a good image.
- Rigorously test: the MCP server; writing paths; setting tag heights — including
  registering a height on an OFF-CENTER tag and asserting the reported center moves
  per the parallax formula.
- Cover live-playfield testing and saved-image testing.
- Test the host package's public Python API — `aprilcam.client`
  (`Discovery().connect()` → typed `Daemon`) is the sanctioned robot-program surface
  (`src/aprilcam/client/interfaces.py` module docstring) — both driven directly and as
  an installed package (fresh-venv wheel install, cv2-free base install verified; the
  2026-08-23 "pipx daemon silently sees zero cameras" incident made into a permanent
  test).

Stakeholder decisions: capture cameras **2, 3, and 5**; colored rectangles/dots ARE
placed on the field (color family in scope); deliver as a **3-sprint arc,
offline-first**.

Field state at decision time: AprilTags 1, 10, 11, 15, 100; all 10 ArUco field
markers; surveyed colored rectangles/dots placed; no elevated features. Cameras:
2 (hd-usb), 3 (ov9782 color), 5 (ov9281 mono — exposure ~20 daylight / ~300 night).

**Existing assets to build on, not reinvent:**

- `docs/test-plans/camera-image-matrix.md` §6 — six offline assertion families;
  capture tool built (`testdev/capture_matrix.py` + `.yaml`); fixtures committed
  (`tests/images/`); the pytest suite was never written and
  `tests/images/field-state.json` is "unverified" with empty ID arrays. Tracked by
  `clasi/issues/later/image-matrix-fixture-test-suite.md` (this issue supersedes and
  closes it).
- Proven isolation: `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR`/`XDG_RUNTIME_DIR` +
  private daemon + skip-if-not-isolatable
  (`tests/aprilcam/daemon/test_camera_geometry_hardware.py:204-227`), `_short_tmp_dir`
  (103-char unix-socket limit), rig discovery (`tests/aprilcam/hardware_camera.py`),
  session-cached best-exposure sweep
  (`tests/aprilcam/daemon/conftest.py::hardware_test_camera_config`), detection
  priming (`CONFIRMATION_STREAK`, with sprint 034's `CONFIRMATION_GRACE=8`).
- The off-center mount_z hardware pattern:
  `test_camera_geometry_hardware.py:355` — `corrected = raw + (h/H)·(nadir − raw)`,
  target a tag ~70 cm off-nadir, hypot > 2 cm plus per-axis approx abs=2.0.
- Annotation assertion precedents: record round-trip/revision semantics
  (`tests/aprilcam/daemon/test_annotations_integration.py`); rendered
  polyline-midpoint pixel check (`tests/aprilcam/client/test_renderer.py:760`).
- v1's system tests died when their fixture corpus rotted: commit fixtures WITH
  manifests, and collection-time guards that skip cleanly when fixtures are absent.
  v1's hand-rolled MCP stdio JSON-RPC client is a fallback design
  (`git show 65510e4^:tests/system/test_mcp_subprocess.py` in the AprilTags repo).

## Proposed fix

Three layers:

| Layer | Markers | Runs | Touches |
|---|---|---|---|
| **L0 offline** — saved-image suite | none (joins the sprint-close gate automatically) | headless, anywhere | committed fixtures only |
| **L1 live-hardware** — isolated system tests | `camera`/`integration` | the rig | isolated roots + private daemon only |
| **L2 acceptance** — real-daemon field check | new `acceptance` (+`camera`+`integration` so the existing gate expression is unchanged) | the rig, REAL daemon | read-only + reversible ops; byte-level no-write guard |

### Sprint A — offline image-matrix suite on a confirmed field state

1. Capture-tool updates (`testdev/capture_matrix.py`, `capture_matrix.yaml`):
   `--field-state-id` flag; field-state.json always read/written at the fixture root;
   pin the active `calibration.json` snapshot beside each camera's fixtures with a
   sha256 in the manifest (§6.2 requires it); add an ov9281 yaml entry (baseline 20,
   sweep 5→300 spanning day/night, gain axis, no WB); fix the HD-cam control-ordering
   bug from the July run (auto modes to manual before pushing values) — camera 2 is in
   scope.
2. Capture session (operator-assisted, ~15 min): daemon stopped, views closed, run
   from Terminal (camera permission); capture cameras 2, 3, 5 into
   `tests/images/fs-20260823/<slug>/`; leave the July corpus in place (manifests carry
   `field_state_id`); populate `tests/images/field-state.json` with a confirmed
   `fs-20260823` entry: april tags [1,10,11,15,100], the 10 ArUco ids, the placed
   rectangles/dots (recorded during the session, stakeholder-confirmed); commit JPEGs
   + manifests + calibration snapshots.
3. `tests/image_matrix/` package (a package, not the single module the old plan named;
   update `docs/test-plans/camera-image-matrix.md` accordingly): conftest with
   `rglob("manifest.json")` discovery, FixtureEntry loader, skip-when-absent and
   skip-unless-confirmed guards; assertion families all via the daemon's own detection
   entry points (never a parallel pipeline):
   `test_recall.py` (per-config recall; baseline ⇒ full; cliff configs carry explicit
   expected-degraded constants — silent improvement or regression both fail),
   `test_world_accuracy.py` (≤ 1.5 cm vs surveyed positions via the pinned calibration
   snapshot), `test_geometry.py` (+x east / +y north / north-up),
   `test_calibration_robustness.py` (all 10 ArUcos across the sweep),
   `test_color_objects.py` (rectangles/dots within tolerance),
   `test_preprocessing.py` (highpass on/off; informational).
4. Synthetic degradation: `tests/image_matrix/transforms.py` (pure ndarray→ndarray:
   exposure_scale, gamma, contrast, brightness, gaussian_blur, gaussian_noise,
   jpeg_recompress) + `test_synthetic_degradation.py`: mild grid asserts full recall,
   severe grid asserts the documented boundary.
5. `scripts/test-gate.sh` (canonical close-sprint wrapper,
   `-m "not camera and not integration"`) + doc/traceability updates.

### Sprint B — MCP stdio system tests, public API, multi-camera coverage

1. `tests/aprilcam/mcp/test_stdio_offline.py` — spawn a REAL `aprilcam mcp` subprocess
   over stdio (`mcp.client.stdio`; fallback: the v1 hand-rolled client design),
   handshake, `tools/list` superset of the pinned 27-tool inventory. Verify no eager
   daemon connect; else mark `integration`.
2. `tests/aprilcam/mcp/test_stdio_hardware.py` — flagship: isolated roots + private
   daemon + real camera; push playfield + camera config (no `camera_position`);
   `camera calibrate` into the isolated state dir; then over the stdio session: golden
   path (`get_guide`→`list_cameras`→`get_tags`→`where`); paths (`draw_path` → record
   round-trip + revision → `get_frame` deskewed → rendered-pixel assertion at the
   polyline midpoint → `clear_layer` → gone); tag height off-center (`register_tag`
   mount_z=20 on a tag ~70 cm off-nadir → `mount_z_applied == False`, unmoved →
   `set_camera_config` with the known real geometry → `mount_z_applied == True`,
   center moves per formula, hypot > 2 cm, per-axis abs=2.0 → `unregister_tag` →
   restored). Displacement constants from a helper shared with the geometry hardware
   test so the two cannot drift.
3. Generalize `tests/aprilcam/hardware_camera.py` to enumerate ALL qualified cameras;
   parameterize the full-calibrate L1 flow over cameras 2, 3, 5 (each skips
   individually with remediation); assert `--with-intrinsics` on the flat field
   reports `"coplanar-constrained"`.
4. Public Python API system test —
   `tests/aprilcam/client/test_public_api_system.py`: drives `aprilcam.client`
   directly as a robot would: `Discovery().connect()` → `get_tags`/`get_tag`/
   `get_tag_history`, `get_frame` raw+deskewed, one `stream_images` subscription,
   `register_tag`/`unregister_tag`, `put_annotation` AND `replace_layer`
   (client-library-only, untested above unit level), `get_guide`. Camera-dependent
   parts `camera`; daemon-only parts `integration`.
5. Installed-package test — `tests/packaging/test_installed_package.py`
   (`integration`): `uv build` the wheel; fresh venv; pip install BASE install; assert
   `import aprilcam.client` works with NO cv2 importable in that venv; console script
   exists and `-V` matches; consumer script in the fresh venv connects to a private
   isolated daemon (run from the dev venv) and round-trips camera-free data
   (playfield push, annotations draw/list/clear, guide).
6. Traceability rows for every landed test.

### Sprint C — acceptance layer and the elevated procedure

1. `acceptance` marker + `tests/acceptance/` conftest: REQUIRE real roots (fail if
   isolation env vars are set); no-write guard — sha256 every file under the real
   config and state dirs before/after, hard-fail on any diff; terminal-summary hook
   renders a human-readable PASS/FAIL/SKIP report.
2. Acceptance inventory (read-only / reversible-in-memory only; `calibrate` and
   non-dry `locate` excluded via a node-id allowlist): daemon status; camera list;
   raw + deskewed frames from cameras 3 and 5; `get_tags` ⊇ confirmed field state;
   `where` on a surveyed tag; MCP stdio to the REAL daemon (guide, tags,
   draw_path/marker on a throwaway layer, list, clear, prove gone); mount_z
   register → moves-per-formula → unregister → restored.
3. Elevated ("with raised blocks") coverage: gated on `APRILCAM_TEST_ELEVATED=1` —
   `camera locate <cam> --dry-run --reference TAG:SLUG:HEIGHT_CM ...` (full solve
   report, zero writes), assert RMS ≤ 1.0 cm and condition ≤ 447; new operator doc
   `docs/test-plans/staged-elevated-locate.md` (blocks, tags, heights, commands,
   quality gates, and the rule that a real non-dry locate is a deliberate stakeholder
   action, never a test). Without blocks, coverage rests on the existing
   unit/real-fixture regressions (`tests/aprilcam/calibration/test_geometry.py:485-887`).
4. `scripts/test-hardware.sh` (`-m "(camera or integration) and not acceptance"` with
   a preflight checklist echo) and `scripts/test-acceptance.sh` (report tee'd to a
   timestamped file); README/docs runner section.
5. Final traceability fill (`docs/design/use-cases/{daemon,cli}.md` Tests columns,
   backticked pytest node IDs, updated by the ticket that lands each test) + full
   dress rehearsal.

Files created: `tests/image_matrix/`, `tests/images/fs-20260823/`,
`tests/aprilcam/mcp/test_stdio_offline.py`, `tests/aprilcam/mcp/test_stdio_hardware.py`,
`tests/aprilcam/client/test_public_api_system.py`, `tests/packaging/`,
`tests/acceptance/`, `scripts/test-*.sh`, `docs/test-plans/staged-elevated-locate.md`.
Modified: `testdev/capture_matrix.py` + `.yaml`, `tests/images/field-state.json`,
`tests/aprilcam/hardware_camera.py`, `pyproject.toml` (acceptance marker),
`docs/test-plans/camera-image-matrix.md`, `docs/design/use-cases/{daemon,cli}.md`.

Risks: field-state confirmation is a stakeholder step (sprint A); capture logistics
(daemon down ~15 min, v1 daemon off, Terminal camera permission, camera-5 lighting
variance recorded in the manifest); July baselines detected zero AprilTags — if
repeated with tags present, sprint A gains a debugging detour; `mcp.client.stdio`
under `asyncio_mode=auto` may need anyio bridging; the 103-char socket limit applies
to the MCP subprocess env; fixture growth ~5 MB/camera/generation (keep confirmed
generations, prune superseded unconfirmed ones, always skip-not-fail when absent).

## Verification

1. Mutation checks per family (falsify one expectation → readable failure → revert),
   written into ticket acceptance criteria.
2. Skip-cleanly: rename the fixture dir → all L0 image tests skip, gate stays green;
   unplug cameras → L1 skips with remediation.
3. Gate invariance: `scripts/test-gate.sh` selects old-suite ∪ new-L0 and zero
   hardware tests, before and after every sprint.
4. The no-write guard is itself unit-tested (touch a file in a fake tree → guard
   raises).
5. Sprint-close traceability audit (`get_use_case_coverage`); sprint C exit: every
   major UC (daemon 01,03,04,06,09,10,11,12,26,28; CLI 13,14,15,18,19,20,22,25,29)
   has ≥1 named test.
6. The packaging test proves the base install for real: fresh venv, wheel install,
   `import aprilcam.client` succeeds, importing cv2 in that venv FAILS, and data flows
   from a daemon.
7. Dress rehearsal at sprint C close: gate → hardware → acceptance back-to-back; the
   real `camera_position` file byte-identical before/after — proven, not assumed.

## Related

- Supersedes/closes: `clasi/issues/later/image-matrix-fixture-test-suite.md`
- `docs/test-plans/camera-image-matrix.md` (§6 is the L0 contract)
- `docs/knowledge/close-sprint-test-gate-hardware-flakes.md` (gate mechanics)
- `docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md` (the packaging
  incident this issue turns into a test)
- `clasi/issues/later/camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`
  (the 18/19-environmental-failures measurement motivating L0)
- `clasi/issues/later/sprint-030-unfinished-verification-distortion-benefit-unproven.md`
  (world-accuracy family bears on it)
- `clasi/issues/later/live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md`
  (future Pi-side acceptance, out of scope here)
