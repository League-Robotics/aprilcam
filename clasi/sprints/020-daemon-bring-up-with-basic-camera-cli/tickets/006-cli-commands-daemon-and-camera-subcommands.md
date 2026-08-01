---
id: '006'
title: 'CLI commands: daemon and camera subcommands'
status: open
use-cases: [UC-01, UC-13, UC-14, UC-17, UC-25]
depends-on: ['005']
github-issue: ''
issue: daemon-bring-up-with-basic-camera-cli.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI commands: daemon and camera subcommands

## Description

Depends on ticket 005 (the CLI is a thin shell over the client library
— zero business logic per `docs/design/adapters.md` rule 4). Implements
`cli/daemon.py` and `cli/camera.py`, wired into the `main()` stub from
ticket 001:

- `aprilcam daemon start|stop|status` — `status` accepts `-d HOST` per
  `cli.md`'s drafted answer ("yes, `status` accepts `-d`, lifecycle does
  not"); `start`/`stop` refuse `-d` naming a remote host with a message
  pointing at SSH (UC-13.S-4) — process lifecycle is local-only by
  design, not a missing feature.
- `aprilcam camera list` — `-d HOST` / `--all` fan-out (read-only,
  allowed per `cli.md`'s fan-out table), one row per camera, one
  unreachable daemon during `--all` reported alongside working results
  rather than failing the whole command (UC-14.S-4).
- `aprilcam camera probe` — `-d HOST` / `--all`, reports the refreshed
  list and names any `ProbeReport.skipped` backends.
- `aprilcam camera image <camera>` — `-o FILE` / raw-to-stdout /
  `--base64`, `-d HOST`, `--encoding jpeg|png` (defaulting to jpeg,
  `png` selecting the lossless path for UC-17.S-1's fixture use case).
  `--all` is **not** offered on `camera image` as a single-file
  destination doesn't have a sensible multi-daemon meaning in this
  form — confirm against `cli.md`'s fan-out table before implementing;
  if the table's `camera image` row does sanction fan-out for the
  stdout/base64 modes specifically, implement it for those modes only
  and document the file-output exception.

All four commands use the typed errors from `aprilcam.errors` for their
user-facing messages — `AlreadyRunning`, `UnknownCamera`,
`ConnectionFailed`, `DiscoveryFailed` — formatted for a human reader,
never re-stringified generically.

**Reuse from archive**: `archive/src/aprilcam/cli/_daemon.py` (196
lines) and `archive/src/aprilcam/cli/daemon_cli.py` (517 lines) are
useful as reference for lifecycle-command edge cases and argument
shapes, but are tangled with v1's CLI framework — read for reference,
do not import or convert directly; this ticket's argparse/click
structure follows whatever convention ticket 001's `cli/__init__.py`
stub established.

## Acceptance Criteria

- [ ] UC-13.S-3 — `daemon status` (not running) → `daemon start` →
      `daemon status` (running, camera count) → `daemon stop` round
      trips correctly on this Mac.
- [ ] UC-13.S-4 — `aprilcam daemon start -d pi-field.local` (or any
      non-local host) refuses with a message pointing at SSH; nothing
      is started.
- [ ] UC-14.S-1 — `aprilcam camera list` on this Mac with both cameras
      attached lists `arducam-ov9782-usb-camera` and `hd-usb-camera`
      with their enumeration numbers.
- [ ] UC-14.S-3 / UC-14.S-4 — `camera list --all` fans out with
      per-row daemon labels; one unreachable daemon during fan-out is
      reported without failing the rows that did answer (test this with
      a second local daemon instance bound to an alternate socket
      path, standing in for a second host, plus one named `-d` target
      that is deliberately unreachable).
- [ ] UC-25.S-1 / UC-25.S-2 — `camera probe` against both real cameras
      reports each with its derived name and settings, independently.
- [ ] UC-17.S-1 — `camera image arducam-ov9782-usb-camera -o frame.png
      --encoding png` writes a lossless PNG.
- [ ] UC-17.S-2 — default invocation streams raw bytes to stdout,
      pipeable (`| file` or similar smoke check).
- [ ] UC-17.S-3 — a `--base64` (or equivalent) flag emits base64 to
      stdout.
- [ ] UC-17.S-4 — raw-bytes mode to an attached terminal (simulate via
      `isatty` mock) refuses rather than dumping binary, unless an
      explicit override flag is passed.
- [ ] Every error path above surfaces the specific `aprilcam.errors`
      type's message content (holder name, unknown camera + available
      list, etc.) — not a generic "command failed."

## Testing

- **Existing tests to run**: `tests/aprilcam/client/` (ticket 005) —
  CLI commands wrap the client calls tested there.
- **New tests to write**: `tests/aprilcam/cli/test_daemon_cli.py`,
  `tests/aprilcam/cli/test_camera_cli.py` — argument parsing and
  formatting against a mocked client (headless, fast); a smaller set of
  `@pytest.mark.camera` CLI-level integration tests invoking the actual
  `aprilcam` entry point via subprocess against a real local daemon
  with both cameras attached, covering the UC scenarios above.
- **Verification command**: `uv run pytest tests/aprilcam/cli/` then
  `uv run pytest -m camera tests/aprilcam/cli/` on this Mac.
