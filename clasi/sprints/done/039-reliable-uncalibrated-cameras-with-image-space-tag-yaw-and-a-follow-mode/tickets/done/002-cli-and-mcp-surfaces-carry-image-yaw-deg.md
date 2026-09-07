---
id: '002'
title: CLI and MCP surfaces carry image_yaw_deg
status: done
use-cases: []
depends-on:
- '001'
github-issue: ''
issue: uncalibrated-tag-records-should-report-image-space-yaw-in-degrees.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI and MCP surfaces carry image_yaw_deg

## Description

Surface `TagRecord.image_yaw_deg` (ticket 001) through the two
read-only surfaces that print/return `TagRecord`s today: the CLI's
one-shot `camera tags` and the three MCP perception tools.

**CLI**: `cli/camera.py`'s `_format_tag_row` currently prints, for the
uncalibrated branch, `UNCALIBRATED -- pixel=(x, y)` with nothing else.
Extend it to also print the image yaw, e.g. `UNCALIBRATED --
pixel=(x, y) yaw=NN.N°` (pick a format consistent with the calibrated
branch's `yaw=...` field already two lines above it in the same
function) — `"-"` when `image_yaw_deg is None`, matching the existing
placeholder convention the calibrated branch already uses for
`yaw`/`heading`/`speed`.

**MCP**: `mcp/tools/perception.py`'s `get_tags`/`get_tag`/
`get_tag_history` already return `TagRecord`-shaped data through
FastMCP's pydantic introspection with no `.to_dict()` shim (per
`mcp-DESIGN.md`'s note this sprint added — `TagRecord`'s wire shape has
not diverged from its dataclass shape). Confirm this is actually true
by writing a test that calls each of the three tools against a fake
daemon returning a `TagRecord` with `image_yaw_deg` set, and asserting
the field survives the MCP round-trip — do not assume the docstring's
claim is correct without a test proving it, since a future FastMCP/
pydantic version bump could silently change that.

## Acceptance Criteria

- [x] `_format_tag_row`'s uncalibrated branch prints `image_yaw_deg`
      next to the pixel position, with a `"-"` placeholder when it is
      `None`.
- [x] The calibrated branch is unchanged (it does not print
      `image_yaw_deg` — that branch already has `yaw_rad`; do not add a
      redundant/confusing second yaw to the calibrated row).
- [x] `get_tags`, `get_tag`, and `get_tag_history` (MCP) each return
      `image_yaw_deg` for a record that has it, verified by a test
      against a fake/stub daemon, not just read from the source code.
- [x] No RPC/proto change and no `mcp/tools/perception.py` code change
      is needed if the round-trip test in fact passes without one — if
      it does *not* pass (the field is silently dropped), add whatever
      minimal explicit serialization is needed and update
      `mcp-DESIGN.md`'s note accordingly (this would be new information
      contradicting what this sprint's architecture assumed from
      reading the existing code — flag it, don't just quietly fix it).

      **Resolved as the "does not pass" branch, but at a layer the
      ticket text didn't anticipate.** The MCP round-trip test (fake
      `Daemon`, `mcp/tools/perception.py`) passes with zero code
      change, exactly as `mcp-DESIGN.md` claimed — the pydantic
      boundary really does carry a new dataclass field for free.
      What does *not* survive for free is the separate real gRPC
      boundary between the daemon process and any CLI/MCP client: a
      genuine end-to-end round-trip test
      (`tests/aprilcam/client/test_daemon_client.py`) showed
      `image_yaw_deg` coming back `None` because `proto/
      aprilcam_v2.proto`'s `TagRecord` message had no field for it.
      Fixed by widening the message (`image_yaw_deg = 12`, additive
      per the proto file's own header rule) and adding the matching
      case to `daemon/rpc.py`'s `_tag_record_to_proto` and
      `client/models.py`'s `tag_record_from_proto`. `daemon-DESIGN.md`
      (not `mcp-DESIGN.md` — the wrong assumption was there) is
      corrected accordingly, since it had claimed "no proto change"
      for this exact field.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/test_camera.py tests/aprilcam/mcp -m "not camera and not integration"` (adjust to actual test paths).
- **New tests to write**: a `_format_tag_row` unit test asserting the
  new yaw suffix for an uncalibrated record with `image_yaw_deg` set
  and one with it `None`; an MCP round-trip test per tool
  (`get_tags`/`get_tag`/`get_tag_history`) against a fake `Daemon`
  returning a `TagRecord` with `image_yaw_deg` populated.
- **Verification command**: `uv run pytest tests/aprilcam/cli tests/aprilcam/mcp -m "not camera and not integration"`
