---
id: "004"
title: "Client wrapper and camera flatfield CLI command"
status: open
use-cases: [SUC-001, SUC-002, SUC-003]
depends-on: ["003"]
github-issue: ""
issue: flat-field-gain-map-capture-as-a-daemon-workflow.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Client wrapper and camera flatfield CLI command

## Description

Add the `client/` wrapper and the `aprilcam camera flatfield` CLI
command over ticket 003's RPCs. In `client/daemon_client.py`'s
`GrpcDaemon`, add `capture_flat_field`/`get_flatfield`/`clear_flat_field`
— each builds its own request and hands the response straight to a
`client/models.py` converter, never reading a response field itself
(this module's existing "requests here, responses only via `models.py`"
boundary). Add `flatfield_result_from_proto`/`flatfield_info_from_proto`
to `client/models.py` with uniform `HasField()` discipline for every
optional field (never coerced to a fabricated default), and extend
`camera_state_from_proto` to read the two new `flatfield_present`/
`flatfield_stale` fields. Extend `_reconstruct_error` to map
`NoFieldMask`'s trailing metadata back to the typed exception, alongside
the existing `NoPlayfieldLinked` mapping.

Add `cli/flatfield.py`, mirroring `cli/calibrate.py`'s shape exactly (a
pure RPC orchestrator, grouped under `camera` structurally but kept in
its own file): `aprilcam camera flatfield <cam> [--exposure N] [--frames
K] [--clear] [--dry-run] [--force] [-d HOST]`. `--all` is refused up
front, before any connection is attempted, with the same message shape
`calibrate.py`'s refusal uses (calibration/flat-field capture are both
single-daemon operations). `--clear` combined with any capture-only flag
(`--exposure`/`--frames`/`--dry-run`/`--force`) is refused client-side,
before any RPC — a usage error, not a daemon round trip. Wire
`add_subparser` into `cli/camera.py`'s own `sub` subparsers action, the
same "one thin module, hooked in by its parent" shape `calibrate.py`
already uses.

Output: a successful capture reports the measured p98/p2 spread before
and after correction, per channel; a refusal reports the reason plus the
same spread numbers (never omitted just because the gate failed);
`--dry-run`'s output is clearly labelled as evaluated-but-not-persisted.

## Acceptance Criteria

- [ ] `GrpcDaemon.capture_flat_field`/`get_flatfield`/`clear_flat_field`
      added, each request-build-then-converter-decode, matching
      `calibrate`/`get_calibration`'s existing shape.
- [ ] `client/models.py`'s `flatfield_result_from_proto`/
      `flatfield_info_from_proto` follow uniform `HasField()` discipline.
- [ ] `camera_state_from_proto` reads the new `flatfield_present`/
      `flatfield_stale` fields.
- [ ] `_reconstruct_error` maps `NoFieldMask`'s trailing metadata to the
      typed exception client-side.
- [ ] `cli/flatfield.py`'s `aprilcam camera flatfield <cam>` supports
      `--exposure`/`--frames`/`--clear`/`--dry-run`/`--force`/`-d`,
      refuses `--all` up front (mirroring `calibrate.py`'s refusal
      message), and refuses `--clear` combined with any capture-only
      flag before any RPC.
- [ ] `camera flatfield` is wired into `cli/camera.py`'s subparser tree
      and appears in `aprilcam camera --help`.
- [ ] Successful, refused, and `--dry-run` output all report per-channel
      spread before/after; `--dry-run` output is clearly labelled as not
      persisted.

## Testing

- **Existing tests to run**: `tests/aprilcam/client/`,
  `tests/aprilcam/cli/` (no regressions, in particular `calibrate.py`'s
  own tests since this ticket copies its shape).
- **New tests to write**: `client/daemon_client.py`/`models.py` wrapper
  unit tests against a fake gRPC stub; `cli/flatfield.py` tests
  (argparse shape and output formatting) using a fake
  `Discovery`/`Daemon`, covering success, refusal, `--force`,
  `--dry-run`, `--clear`, and the `--all`/`--clear`-conflict refusals.
- **Verification command**: `uv run pytest tests/aprilcam/client/
  tests/aprilcam/cli/`
