---
id: '005'
title: "Annotation store \u2014 integration and acceptance verification"
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
- '002'
- '003'
- '004'
github-issue: ''
issue: annotation-store-for-shared-spatial-state.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Annotation store — integration and acceptance verification

## Description

Close out the annotation-store issue with the end-to-end checks its
own Verification section names: multi-client visibility, restart
wipes state, and rendering. This is the last ticket against
`annotation-store-for-shared-spatial-state.md` (`completes_issue:
true` — the issue archives once this ticket is done).

**Headless — no real camera needed.** Nothing about annotation
correctness depends on real optics: the store is playfield-addressed,
not vision-derived. Use the same `daemon.__main__.build()` +
`FakeBackend`-camera + synthetic-calibration pattern
`test_lifecycle_integration.py` and other headless integration tests
in this suite already use to get a real deskewed frame without
hardware. Do **not** mark this `@pytest.mark.camera` — that mark is
reserved for tests that genuinely need the physical camera 3 /
`main-playfield` (tickets 006/007's phantom-detection story).

**Depends on 001, 002, 003, 004** — this ticket exercises the full
vertical slice (server store, client wire, renderer, CLI) together;
it is not testable meaningfully until all four exist.

Verifies (issue's own Verification section, restated as concrete
tests):

- Draw a marker from one client (`GrpcDaemon` instance A); a second,
  independently-connected client (`GrpcDaemon` instance B) sees it in
  `list_annotations` and in a rendered deskewed frame.
- Restart the daemon (tear down and rebuild the `DaemonService`/gRPC
  server, matching `test_lifecycle_integration.py`'s existing restart
  pattern if one exists, or construct fresh); annotations are gone,
  `get_annotations` returns an empty set at revision 0, and the
  original client's re-put on "reconnect" is visible again.
- `aprilcam playfield annotations` (subprocess, matching
  `test_cli_integration.py`'s pattern) round-trips against the same
  daemon a `GrpcDaemon`-instance put wrote to.
- A `client/renderer.py` `Renderer.render()` call against a real
  fetched deskewed frame (not a synthetic fixture — this is the one
  place in the annotation-store story that exercises the full stack
  end to end) shows drawn pixels for a put annotation.

## Acceptance Criteria

- [x] Two independent `GrpcDaemon` connections to the same daemon
      agree on annotation state — a put on one is visible via
      `get_annotations` on the other with no daemon-side action beyond
      the put itself (UC-28.S-1, S-3).
- [x] A deskewed frame fetched after the put embeds the annotation,
      and `Renderer.render()` on that frame draws it (assert on pixel
      output, not just presence in `frame.annotations`).
- [x] Restarting the daemon process (or its `DaemonService`+gRPC
      server stack, in-test) leaves `get_annotations` reporting an
      empty set at revision 0 for a playfield that previously held
      annotations (UC-28.S-4).
- [x] `aprilcam playfield annotations <name>` (subprocess) prints an
      annotation put via the Python client on the same daemon; `aprilcam
      playfield annotations <name> --clear` (subprocess) removes it,
      confirmed by a follow-up `get_annotations` call.
- [x] Full suite green, including this ticket's new tests: `uv run
      pytest -m "not camera"`. Baseline is 717 headless tests passing
      before this sprint; report the new total in this ticket's
      completion notes.
- [x] `uv run mypy` clean.

## Completion Notes

- **Baseline was already 808, not 717.** Tickets 001–004 (already
  `status: done` before this ticket started) had grown the headless
  suite from the sprint's originally-stated 717 to 808 by the time
  this ticket began (`uv run pytest -m "not camera" --collect-only -q`
  confirmed 808/842 collected before any change here). This ticket
  added 3 new tests; the suite now reports **811 passed, 34 deselected**
  (`uv run pytest -m "not camera" -q`), re-run three times with no
  flakiness. `uv run mypy` reports "Success: no issues found in 55
  source files".
- **No integration gap surfaced.** Tickets 001–004 compose exactly as
  designed — no production code changed in this ticket, only new
  tests. `client/daemon_client.py`'s five annotation methods,
  `daemon/service.py`'s store, `client/renderer.py`'s draw routines,
  and `cli/playfield.py`'s `cmd_annotations` all worked together on
  the first attempt.
- **Substantial coverage already existed from 001–004** and was not
  duplicated: `test_service.py` already had
  `test_two_writers_on_one_key_last_write_wins_no_merge_no_error`
  (UC-28.S-6) and every `UnknownPlayfield`-on-annotation-RPC rejection
  (UC-28.S-5); `test_daemon_client.py` already had a two-independent
  -clients wire-level test and a deskewed-embed test (a synthetic-
  fixture-free, real-ArUco-calibrated frame, but no `Renderer` call);
  `test_renderer.py` already had per-shape pixel-level draw tests
  against synthetic `ImageFrame` fixtures; `test_playfield_cli.py`
  already had `cmd_annotations` unit tests against a fake `Daemon`.
  This ticket's own new tests build on top of that rather than
  re-proving it.
- **New tests added** (`tests/aprilcam/daemon/test_annotations_integration.py`,
  2 tests; one addition to `tests/aprilcam/cli/test_cli_integration.py`):
  1. `test_put_by_one_client_is_visible_to_a_second_client_and_renders_on_a_real_deskewed_frame`
     — the one full-stack round trip the story still lacked: put on
     client A, `get_annotations` agreement on independently-connected
     client B (UC-28.S-1/S-3), fetch B's next real deskewed frame
     (`FakeBackend` + `render_playfield_frame`'s real `cv2.aruco`
     markers + a real homography solve via `Calibrator` — not a
     synthetic `ImageFrame`), confirm the embedded `AnnotationSet`
     carries it, then `Renderer().render(frame)` and assert the drawn
     pixel patch at the world-predicted location differs from an
     undrawn control patch elsewhere in the same frame (measured
     Euclidean color distance > 30, mirroring `test_renderer.py`'s own
     threshold). Observed real output: marker drawn at world (0, 0),
     predicted pixel matched `world_to_deskewed_pixel`'s own formula,
     drawn/control patches differed well past the threshold on every
     run.
  2. `test_daemon_restart_wipes_annotations_while_the_disk_backed_playfield_and_a_re_put_survive`
     — a genuine restart via `daemon.__main__.build()` (`start_watch=
     False`, headless, no camera subsystem touched), with
     `main-playfield` installed directly in `config_dir` (ticket
     023-003's boot-time load) rather than pushed over RPC, so it is
     the *playfield* — not the annotation — that survives. Observed:
     before restart, `get_annotations` returns 1 annotation at
     revision 1; after rebuilding `DaemonService` fresh against the
     same `config_dir`/`state_dir`, `get_annotations` returns an empty
     set at revision 0 (UC-28.S-4), and a re-put on the same
     `(playfield, layer, id)` after "reconnect" is visible again.
  3. `test_playfield_annotations_subprocess_round_trips_against_a_python_client_put`
     (`tests/aprilcam/cli/test_cli_integration.py`, `@pytest.mark.
     integration`, no camera) — the real, installed `aprilcam` console
     script via `subprocess`, against a real daemon started the same
     way (`daemon start`/`daemon stop` subprocess calls, `isolated_env`
     fixture). A Python `GrpcDaemon` (`Discovery().connect()`, same
     env-resolved socket) pushes a playfield and puts an annotation;
     `aprilcam playfield annotations main-playfield` (subprocess)
     printed `revision 1` and the `planner`/`goal` row; `--clear`
     (subprocess) printed a "cleared" confirmation, and a follow-up
     Python `get_annotations` call on the same daemon confirmed
     `annotations == ()`.
- **All six UC-28 scenarios exercised, across old and new tests**:
  S-1 and S-3 by this ticket's new render round trip (plus 002's
  existing wire-level test); S-2 (atomic replace) by 002's existing
  `test_replace_layer_round_trip_swaps_a_layers_contents`, backed by
  `replace_layer`'s single lock-guarded dict assignment in
  `daemon/service.py`; S-4 by this ticket's new restart test; S-5 by
  002's existing `UnknownPlayfield` tests; S-6 by 001's existing
  `test_two_writers_on_one_key_last_write_wins_no_merge_no_error`.
- **For the stakeholder to verify by hand**: this ticket's tests are
  all headless/synthetic per the ticket's own instruction ("Headless —
  no real camera needed"). Nobody has watched annotations actually
  render inside a live `playfield view` window against camera 3 /
  `main-playfield` in this ticket — `view/viewer.py` needed no code
  change (ticket 003/sprint.md already established this), so this is
  expected to just work, but seeing it with your own eyes
  (`aprilcam playfield view`, with a marker pushed from a second
  terminal via the Python client) is the one thing this ticket's test
  suite cannot itself confirm.

## Implementation Plan

**Approach**: this ticket is almost entirely test-writing, not new
production code — 001-004 already build every piece; this ticket
proves they compose. If a genuine integration gap surfaces (e.g. a
mismatch between what 001 embeds and what 002/003 expect), fix it here
rather than reopening the ticket it belongs to, and note the fix in
this ticket's completion notes.

**Files**:
- New integration test module, e.g.
  `tests/aprilcam/daemon/test_annotations_integration.py`, following
  `test_lifecycle_integration.py`'s fixture pattern.
- CLI subprocess tests, e.g. extending
  `tests/aprilcam/cli/test_cli_integration.py`.

**Testing plan**:
- **Existing tests to run**: full headless suite — this ticket is a
  net-new test module, so "no regression" means the count only grows.
- **New tests to write**: exactly the four bullets in Description
  above, as real pytest tests.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none required — this ticket verifies
already-documented behaviour, it does not change the design.
