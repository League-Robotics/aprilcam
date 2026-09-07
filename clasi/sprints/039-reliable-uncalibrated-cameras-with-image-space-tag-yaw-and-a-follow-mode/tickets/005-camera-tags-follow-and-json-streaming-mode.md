---
id: '005'
title: camera tags --follow and --json streaming mode
status: open
use-cases: []
depends-on:
- '002'
github-issue: ''
issue: camera-tags-needs-a-follow-mode-that-streams-detections.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# camera tags --follow and --json streaming mode

## Description

Add `aprilcam camera tags <camera> --follow` (and `--json`) to
`cli/camera.py`'s `cmd_tags`, subscribing to the daemon's existing tag
stream instead of a one-shot burst. This is the mode
`_confirm_detections`'s 3-frame confirmation streak was designed for —
today's one-shot `camera tags` reports `(no tags detected)` on the
first call after any gap even with tags plainly visible, because the
confirmation streak has to rebuild from zero each isolated call.

Depends on ticket 002: `--follow`'s per-row output reuses (an extended)
`_format_tag_row`, so that ticket's `image_yaw_deg` display must land
first.

No daemon-side or client-library change is needed — `Daemon.stream_tags`
(`client/daemon_client.py`) and `client.streams.SocketTagStream`
already exist from the streaming sprint (027). This ticket is purely
`cli/camera.py` becoming the first caller of that existing client
method. Read this sprint's `design/cli-DESIGN.md` overlay's `--follow`/
`--json` sections before implementing.

**Implementation approach**:
1. `cli/camera.py`: add `--follow` and `--json` flags to the `camera
   tags` subparser (`add_subparser`). `--follow` refuses `--all` up
   front, before any connection — a single blocking stream loop cannot
   fan out the way a one-shot RPC does (mirror the existing refusal
   shape other `--all`-excluded commands use, e.g. `cmd_view`'s).
2. `cmd_tags`: when `--follow` is given, call
   `discovery.connect(args.daemon).stream_tags(args.camera)` and, for
   each yielded `TagFrame`, print one row per tag with a timestamp
   prefix (`_format_tag_row`'s extended output from ticket 002, or
   `--json`'s one-`json.dumps(...)`-per-line sibling) — loop until
   `KeyboardInterrupt` (Ctrl-C), then call the stream's `.close()` and
   return `EXIT_OK`. Ctrl-C ending a `--follow` session is success, not
   an error — mirror `camera view`'s "window closing is a normal exit"
   convention, not a crash/traceback.
3. `--json` fields per tag: at minimum `tag` (family + number),
   `timestamp`, `pixel` (x, y), `image_yaw_deg` — one JSON object per
   line (JSON Lines), not a JSON array (so a consumer can process the
   stream incrementally without waiting for it to end).
4. `AprilCamError` during connect/stream-open still goes through the
   existing `report_error` path — only the interrupt loop itself treats
   Ctrl-C specially.

## Acceptance Criteria

- [ ] `aprilcam camera tags <camera> --follow` streams rows
      continuously (one per tag per frame) until Ctrl-C, printing tag
      id, timestamp, pixel position, and `image_yaw_deg`, using no
      manual repeated invocation.
- [ ] `--json` emits one machine-parseable JSON object per line with
      the same fields — verified by a test that parses each printed
      line with `json.loads` and checks the expected keys.
- [ ] Ctrl-C (`KeyboardInterrupt`) during `--follow` closes the stream
      cleanly and exits `EXIT_OK`, not a traceback.
- [ ] `--follow --all` (and `--json --all`) is refused before any
      connection is attempted, with a message naming the restriction —
      argparse-level or an explicit client-side check, either is fine
      as long as it fires before `discovery.connect(...)`.
- [ ] One-shot `camera tags` (no `--follow`) is completely unchanged in
      behavior — this ticket adds a new code path, it does not modify
      the existing one.
- [ ] A fake-daemon/fake-stream test drives `cmd_tags` with `--follow`
      through a few synthetic `TagFrame`s and a simulated
      `KeyboardInterrupt`, asserting the printed output and clean exit
      — this cannot be a real-hardware test (no `@pytest.mark.camera`),
      it must run against a fake `TagStream`.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/test_camera.py -m "not camera and not integration"`.
- **New tests to write**: a fake `TagStream` (yields a few synthetic
  `TagFrame`s then raises `KeyboardInterrupt`, or is driven by a test
  harness that injects the interrupt after N frames) driving `cmd_tags`
  with `--follow` and separately with `--follow --json`, asserting
  printed row format / JSON-Lines format and `EXIT_OK`; a test asserting
  `--follow --all` is refused with no connection attempted (use a
  discovery factory that raises if `find_all`/`connect` is ever called).
- **Verification command**: `uv run pytest tests/aprilcam/cli -m "not camera and not integration"`
