---
id: '006'
title: Hardware-marked capture test and knowledge-doc update
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '005'
github-issue: ''
issue: flat-field-gain-map-capture-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware-marked capture test and knowledge-doc update

## Description

Close out the sprint: one real-hardware capture test, the
knowledge-doc update the issue itself calls for, and a full
Definition-of-Done pass against `sprint.md`'s Success Criteria.

Add one hardware-marked test performing a real `CaptureFlatField`
against an attached camera, following the same shape as this project's
existing `@pytest.mark.camera` hardware tests in
`tests/aprilcam/daemon/test_service.py` (the `real_daemon_service`/
`directories` fixtures, the `_real_identity`-style camera-presence
lookup, `pytest.skip(...)` — never a failure — when no matching camera
is attached). It should verify a captured map installs and is actually
applied on the next real frame read.

Rewrite `docs/knowledge/flat-field-gain-map.md`'s "Capturing a map"
section to describe this sprint's `aprilcam camera flatfield <cam>`
workflow as the primary instructions, keeping the old manual-script
recipe (`assets/build_flatfield_2026-09-02.py`) only as historical/
superseded context — the issue this sprint implements exists
specifically to retire that manual recipe as the documented path.

Finally, walk every item in `sprint.md`'s Success Criteria and confirm
each is true end-to-end (capture/inspect/clear through CLI+MCP alone, no
manual script or hard-coded polygon; quality-gate refusal behavior;
flat-field state visible next to calibration state; `cv2`-free
client/CLI/MCP; the three test categories all present).

## Acceptance Criteria

- [x] One `@pytest.mark.camera` (matching this project's existing
      marker convention — confirm the exact marker name already in use
      rather than inventing a new one) test performs a real
      `CaptureFlatField` against an attached camera and verifies the
      map is applied on a subsequent real frame; it skips cleanly (never
      fails) when no matching camera is present.
- [x] `docs/knowledge/flat-field-gain-map.md`'s "Capturing a map"
      section documents the `aprilcam camera flatfield` workflow as the
      primary path; the manual-script recipe is kept only as historical
      context, clearly marked superseded.
- [x] Every `sprint.md` Success Criteria item is verified true
      end-to-end.
- [x] `tests/test_no_cv2_in_client_modules.py` passes.
- [x] The offline test gate (`-m "not camera and not integration"`, the
      project's existing convention — see
      `docs/knowledge/project_sprint_close_test_gate.md`) passes across
      every module this sprint touched.

**Verification notes (this ticket's own pass):**

- New test: `tests/aprilcam/daemon/test_service.py::
  test_capture_flat_field_installs_and_applies_a_real_gain_map`, marked
  `@pytest.mark.camera` (the project's existing marker -- confirmed via
  `pyproject.toml`'s `[tool.pytest.ini_options] markers`; no `hardware`
  marker exists in this repo). Uses the `real_daemon_service`/
  `directories` fixtures and the `_real_identity`/`_open_or_skip` helpers
  already established in this file's real-hardware section. **This
  session has no attached camera and cannot start/stop/restart the real
  daemon that owns the cameras (see
  `docs/knowledge/daemon-camera-grant-and-detection-priming.md`), so the
  test was run once and observed to SKIP cleanly** (`no attached camera
  matching 'Arducam OV9782'`) -- confirmed skipping, not passing; it has
  not been run against real hardware.
- `docs/knowledge/flat-field-gain-map.md` rewritten: "Capturing a map"
  now documents `aprilcam camera flatfield <camera> [--exposure N]
  [--frames K] [--force] [--dry-run] [--clear]` (CLI/MCP/quality-gate/
  staleness) as the primary path; the old manual script
  (`assets/build_flatfield_2026-09-02.py`) is kept as a clearly-marked
  superseded historical reference, not the recipe.
- `sprint.md` Success Criteria walked end-to-end against the current
  source: CLI+MCP-only capture/inspect/clear with no manual script or
  hard-coded polygon (`cli/flatfield.py`, `mcp/tools/configuration.py`'s
  `capture_flatfield`/`get_flatfield`/`clear_flatfield`, mask from
  `DaemonService._field_mask_for`); quality-gate refusal behaviour
  (`calibration/flatfield.py`'s clipping/spread/disagreement checks,
  `--force` override, spread always reported); flat-field state next to
  calibration state (`cli/_common.py`'s `flatfield=present|absent` /
  `flatfield-stale`, `client/models.py`, `mcp/tools/perception.py`);
  `cv2`-free client/CLI/MCP (`tests/test_no_cv2_in_client_modules.py`
  passes); all three test categories present (`tests/aprilcam/
  calibration/test_flatfield.py` math unit tests,
  `tests/aprilcam/daemon/test_flatfield_rpc.py` +
  `tests/aprilcam/mcp/test_configuration.py` +
  `tests/aprilcam/cli/test_flatfield_cli.py` +
  `tests/aprilcam/client/test_daemon_client_flatfield.py` fake-backend/
  RPC round-trip tests, plus this ticket's new hardware-marked test).
- Offline gate run together, as this ticket's own testing note asks:
  `uv run pytest tests/aprilcam/calibration/ tests/aprilcam/daemon/
  tests/aprilcam/client/ tests/aprilcam/cli/ tests/aprilcam/mcp/
  tests/test_no_cv2_in_client_modules.py -q -m "not camera and not
  integration"` -> **1286 passed, 69 deselected in 146.00s**.
- Definition-of-done doc pass: `src/aprilcam/AGENT_GUIDE.md` already
  covers the flat-field workflow (§9, from ticket 005); added the
  missing `capture_flatfield`/`get_flatfield`/`clear_flatfield` rows and
  `flatfield_present`/`flatfield_stale` fields to
  `docs/wiki/mcp-server.md`'s tool tables, and a cross-reference from
  `docs/wiki/lighting-and-detection.md` (the existing "uneven
  illumination" doc) to the new per-camera software-side fix. Canonical
  `docs/design/design.md` / `src/aprilcam/daemon/DESIGN.md` and other
  per-subsystem `DESIGN.md` files were left untouched -- out of this
  ticket's declared design-doc scope (only `design.md` and
  `daemon/DESIGN.md`, both already current via this sprint's overlays);
  consolidating overlays into canonical docs is a separate, later step.

## Testing

- **Existing tests to run**: the full offline gate across
  `tests/aprilcam/calibration/`, `tests/aprilcam/daemon/`,
  `tests/aprilcam/client/`, `tests/aprilcam/cli/`, `tests/aprilcam/mcp/`,
  plus `tests/test_no_cv2_in_client_modules.py` — this ticket is the
  natural point to run the sprint's full offline surface together, ahead
  of `close_sprint`'s own full-suite gate.
- **New tests to write**: the one hardware-marked capture test described
  above.
- **Verification command**: `uv run pytest -m "not camera and not
  integration"` for the offline gate; the hardware test itself is
  verified manually against the real rig, per this project's existing
  convention (camera tests fail while the pipx daemon owns the cameras
  in an automated run).
