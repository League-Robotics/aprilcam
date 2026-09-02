---
id: "006"
title: "Hardware-marked capture test and knowledge-doc update"
status: open
use-cases: [SUC-001, SUC-002, SUC-003]
depends-on: ["005"]
github-issue: ""
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

- [ ] One `@pytest.mark.camera` (matching this project's existing
      marker convention — confirm the exact marker name already in use
      rather than inventing a new one) test performs a real
      `CaptureFlatField` against an attached camera and verifies the
      map is applied on a subsequent real frame; it skips cleanly (never
      fails) when no matching camera is present.
- [ ] `docs/knowledge/flat-field-gain-map.md`'s "Capturing a map"
      section documents the `aprilcam camera flatfield` workflow as the
      primary path; the manual-script recipe is kept only as historical
      context, clearly marked superseded.
- [ ] Every `sprint.md` Success Criteria item is verified true
      end-to-end.
- [ ] `tests/test_no_cv2_in_client_modules.py` passes.
- [ ] The offline test gate (`-m "not camera and not integration"`, the
      project's existing convention — see
      `docs/knowledge/project_sprint_close_test_gate.md`) passes across
      every module this sprint touched.

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
